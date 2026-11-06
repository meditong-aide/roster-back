"""수정 도우미 스킬 20개를 AIDE 매니페스트에 올린다(S4 · 11-06) — AIDE 실행 경로(`agents_v2.middleware.execute_skill`)로도
같은 스킬을 같은 결과로 돌리기 위한 얇은 감싸기. 수정 도우미 자기 경로(`middleware.py`·`registry.py`)는 그대로다(S5 에서 갈아탄다).

  · 등록은 이 모듈이 매니페스트를 import 해서 한다 — `agents_v2/*` 는 `schedule_edit_agent` 를 import 하지 않는다(층 분리).
    AIDE 쪽은 `manifest._MANIFEST_MODULES` 에 이 모듈 경로 문자열만 두고 `load_manifest_skills()` 가 불러 준다.
  · 실행 함수는 AIDE 서명 `(db, params)` 이고 턴은 `agents_v2.turn_context.current_turn()` 으로 받는다(턴 밖이면 RuntimeError).
    기존 핸들러 본문 `(turn, params) → Outcome` 은 그대로 부르고 결과를 `Outcome.to_data()` 로 돌려준다.
  · 훅(스펙의 context_first·grounder)은 AIDE 일반 코드가 부르는 계약을 따른다 — 되물음 결과 dict 를 직접 돌려준다:
      context_first(turn, params) → 끝낼 결과 dict 또는 None
      grounder(turn, params)      → (확정한 값 또는 None, 끝낼 결과 dict 또는 None)
    되물음 문장·기록(`skills._clarify`)은 여기서 만든다.
  · 예외 처리는 수정 도우미 미들웨어와 같다: SkillError → 'error' 기록 + failed 결과 · 예상 밖 예외 → 'error' 기록 뒤 다시 올림
    (AIDE 가 error 결과로 받는다).
권한 칸: mutation·hn_only 는 끈다 — AIDE ①(`_check_permission`)은 hn_only 면 user_role 이 HN·ADM 이 아닌 사람을 막고 mutation 이면
다른 간호사 이름 인자를 막는데, 수정 도우미를 쓰는 사람(수간호사·그룹관리자·ADM)은 경로가 `authorize_schedule` 로 먼저 가르며 그룹관리자의
user_role 은 HN 이 아닐 수 있다. 읽기/쓰기는 mode 로 정한다(쓰기 = apply_edit 하나 · ⓪ 범위 단계가 버튼 출처만 받는다).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from agents_v2.skills.manifest import SKILL_SPECS, skill
from agents_v2.skills.registry import SKILL_REGISTRY
from agents_v2.turn_context import current_call, current_turn
from schedule_edit_agent import registry
from schedule_edit_agent.descriptions import BUTTON_TOOLS, SKILL_MODES, SKILL_TOOLS
from schedule_edit_agent.skills import CONTEXT_FIRST, GROUNDERS, Outcome, SkillError, Turn, _clarify

AGENT = "schedule-edit"
#: 말로 고르는 도구는 LLM·예시에서만, 버튼은 버튼에서만(`middleware._ALLOWED_KIND` 와 같다)
TOOL_SOURCES = ("llm", "example")
BUTTON_SOURCES = ("button",)

#: 권한 거절(⓪)의 문장 — 수정 도우미 미들웨어와 같은 글자로 되돌린다(`from_aide_result`)
UNKNOWN_TOOL = "무엇을 바꿀지 다시 말씀해 주세요."
UNKNOWN_ACTION = "알 수 없는 동작입니다."


def _norm(name: str) -> str:
    return name.replace("-", "_")


def _need_turn() -> Turn:
    turn = current_turn()
    if turn is None:
        raise RuntimeError("수정 도우미 스킬은 턴 안에서만 실행할 수 있습니다(agents_v2.turn_context.bind_turn).")
    return turn


def _guarded(thunk: Callable[[Turn], Any]) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """(값, None) 또는 (None, failed 결과 dict). 예상 밖 예외는 기록하고 다시 올린다."""
    turn = _need_turn()
    try:
        return thunk(turn), None
    except SkillError as exc:
        source, original = current_call()
        turn.record("error", {"stage": source, "params": original, "message": str(exc)})
        return None, Outcome(answer=str(exc), failed=True).to_data()
    except Exception as exc:   # noqa: BLE001 — 기록을 남기고 위로
        turn.record("error", {"stage": "skill", "error": f"{type(exc).__name__}: {exc}"[:500]})
        raise


def _handler(name: str) -> Callable[[Any, Dict[str, Any]], Dict[str, Any]]:
    func = registry.SKILL_REGISTRY[name]

    def run(db: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        out, failed = _guarded(lambda turn: func(turn, params).to_data())
        return failed or out

    run.__name__ = f"aide_{name}"
    return run


def _context_first(name: str) -> Optional[Callable[[Any, Dict[str, Any]], Optional[Dict[str, Any]]]]:
    hook = CONTEXT_FIRST.get(name)
    if hook is None:
        return None

    def first(turn: Any, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ask, failed = _guarded(lambda t: hook(t, params))
        if failed:
            return failed
        return _clarify(turn, [ask]).to_data(clarify=True) if ask else None

    return first


def _grounder(name: str) -> Optional[Callable[[Any, Dict[str, Any]], Tuple[Any, Optional[Dict[str, Any]]]]]:
    hook = GROUNDERS.get(name)
    if hook is None:
        return None

    def ground(turn: Any, params: Dict[str, Any]) -> Tuple[Any, Optional[Dict[str, Any]]]:
        found, failed = _guarded(lambda t: hook(t, params))
        if failed:
            return None, failed
        grounded, asks = found
        if asks:
            return None, _clarify(turn, asks).to_data(clarify=True)
        return grounded, None

    return ground


def from_aide_result(data: Dict[str, Any], source: str) -> Outcome:
    """AIDE 실행 경로의 결과 dict → Outcome. 권한 거절(⓪·①)은 수정 도우미 미들웨어와 같은 문장·failed 로 되돌린다."""
    if data.get("permission_denied"):
        return Outcome(answer=UNKNOWN_TOOL if source in ("llm", "example") else UNKNOWN_ACTION, failed=source == "button")
    if "answer" not in data:       # 예상 밖 예외로 끝난 결과({'error': …}) — 엉뚱한 KeyError 대신 원래 오류를 올린다
        raise RuntimeError(str(data.get("error") or "수정 도우미 스킬 실행 오류"))
    return Outcome.from_data(data)


def register_all() -> None:
    """수정 도우미 도구 정의 20개를 매니페스트에 올린다. 같은 이름(하이픈·밑줄 구분 없이)이 AIDE 에 이미 올라와 있으면 바로 예외.
    옛 @register 스킬은 지연 적재라 여기서 부르지 않는다 — 그쪽과의 겹침은 시험이 지킨다."""
    taken = {_norm(n) for n in SKILL_SPECS} | {_norm(n) for n in SKILL_REGISTRY}
    for tools, sources in ((SKILL_TOOLS, TOOL_SOURCES), (BUTTON_TOOLS, BUTTON_SOURCES)):
        for schema in tools:
            name = schema["name"]
            if _norm(name) in taken:
                raise RuntimeError(f"수정 도우미 스킬 이름 '{name}' 이 AIDE 에 이미 등록돼 있다 — 이름을 바꾸거나 충돌을 풀어 달라")
            mode = SKILL_MODES[name]
            skill(name, schema, agents=(AGENT,), mode=mode, sources=sources, llm_visible=tools is SKILL_TOOLS,
                  grounder=_grounder(name), context_first=_context_first(name),
                  commit="button")(_handler(name))


register_all()
