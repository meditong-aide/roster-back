"""스킬 한 번 실행 — 권한 → 문맥 주입 → 이름 확정 → 실행 → 기록(AIDE `agents_v2/middleware.py` 와 같은 단계 · 사용자 지시 11-06).

  ① permission        : 수정 도우미에 등록된 도구인가 · 부른 곳(LLM·예시·버튼)이 그 도구를 부를 수 있나 · 읽기/미리보기/쓰기.
                        쓰기(적용)는 버튼으로만이고 새 버전(draft)만 만든다. 사람·병동 권한(수간호사·그룹관리자·ADM + 그
                        병동 접근)은 경로가 턴마다 먼저 확인한다(`spec.authorize_schedule`).
  ② context_injection : 세션이 아는 값(schedule_id·오늘·근무표 달·고칠 수 있는 첫날)은 LLM 인자로 받지 않는다 — 스킬은
                        `Turn` 에서 읽는다. LLM 인자는 도구 정의에 있는 칸만 남긴다. 사용자가 고칠 첫날을 말했으면
                        (edit_from_day) 여기서 먼저 정한다 — 뒤 단계의 날짜 확정이 새 첫날을 쓴다.
  ③ grounding         : 말한 이름·날짜·근무·버전을 근무표 안에서 확정한다(`skills.GROUNDERS`). 모호하면 되묻고 끝낸다.
  ④ execution         : `registry.run_skill`.
  ⑤ trace             : 단계 기록(`MiddlewareStep`) — 이 턴의 첫 결과 기록(schedule_edit_log payload 의 steps)에 남긴다.
AIDE 의 `MiddlewareStep`·`SkillResult` 는 import 하지 않고 같은 칸으로 따로 둔다 — AIDE 미들웨어 모듈은 AIDE 스킬
레지스트리·이름 확정 모듈을 함께 불러오고 자주 바뀐다(모양이 어긋나면 시험이 알린다 · tests/edit_agent/test_middleware.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from schedule_edit_agent import registry
from schedule_edit_agent.descriptions import SKILL_TOOLS
from schedule_edit_agent.skills import CONTEXT_FIRST, GROUNDERS, Outcome, SkillError, Turn, _clarify

#: 부른 곳 — llm=LLM 이 고른 도구 · example=처음 화면 예시(미리 정한 도구 호출 · LLM 없음) · button=카드 버튼
SOURCES = ("llm", "example", "button")
#: 부른 곳마다 부를 수 있는 도구 종류(`registry.kind_of`)
_ALLOWED_KIND = {"llm": "tool", "example": "tool", "button": "button"}
#: LLM 이 없는 도구를 고르거나 버튼을 고르면(기존 문장 그대로)
UNKNOWN_TOOL = "무엇을 바꿀지 다시 말씀해 주세요."
UNKNOWN_ACTION = "알 수 없는 동작입니다."
#: 늘 미들웨어가 채우는 칸 — LLM·화면이 보낸 값은 버린다
GROUNDED = "grounded"
_PROPERTIES = {t["name"]: set((t.get("parameters") or {}).get("properties") or {}) for t in SKILL_TOOLS}


@dataclass
class MiddlewareStep:
    """단계 하나의 기록 — AIDE `agents_v2.middleware.MiddlewareStep` 과 같은 칸·같은 to_dict."""

    name: str  # "permission", "context_injection", "grounding", "execution"
    status: str  # "pass", "block", "injected", "skip", "grounded", "clarification_needed", "ok", "error"
    detail: str = ""
    before: dict | None = None
    after: dict | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"name": self.name, "status": self.status}
        if self.detail:
            d["detail"] = self.detail
        if self.before is not None:
            d["before"] = self.before
        if self.after is not None:
            d["after"] = self.after
        return d


@dataclass
class SkillResult:
    """실행 결과 — AIDE `SkillResult` 와 같은 칸. data 는 스킬 결과(`skills.Outcome`)다."""

    data: Any
    duration_ms: float
    skill_name: str
    grounded_params: dict
    blocked: bool = False
    block_reason: str | None = None
    middleware_steps: List[MiddlewareStep] = field(default_factory=list)


def execute_skill(turn: Turn, skill_name: str, args: Dict[str, Any], *, source: str) -> SkillResult:
    """단계를 차례로 돌고 스킬을 실행한다. 버튼이 지금 상태와 안 맞으면(SkillError) 기록하고 failed 결과로,
    그 밖의 예외는 기록하고 위로 올린다(500) — 앞의 `EditAgent._run` 과 같다."""
    t0 = time.monotonic()
    steps: List[MiddlewareStep] = []
    turn.trace, turn.traced = steps, False
    original = dict(args or {})
    name = registry.resolve(skill_name) or str(skill_name or "")

    # ── ① Permission ──
    block = _check_permission(name, source)
    if block:
        steps.append(MiddlewareStep("permission", "block", detail=block))
        answer = UNKNOWN_TOOL if source in ("llm", "example") else UNKNOWN_ACTION
        return _result(Outcome(answer=answer, failed=source == "button"), t0, name, original, steps, block=block)
    steps.append(MiddlewareStep("permission", "pass", detail=f"source={source}, mode={registry.mode_of(name)}, "
                                                             f"skill={name}, 사람·병동=경로에서 확인"))
    params: Dict[str, Any] = dict(original)
    try:
        # ── ② Context Injection ──
        params, dropped = _inject_context(name, params, source)
        ask = CONTEXT_FIRST[name](turn, params) if name in CONTEXT_FIRST else None
        detail = _context_detail(turn) + (f", 버린 인자={','.join(sorted(dropped))}" if dropped else "")
        if ask:
            steps.append(MiddlewareStep("context_injection", "clarification_needed", detail=f"{detail}, 되물음={ask}"))
            return _result(_clarify(turn, [ask]), t0, name, params, steps)
        steps.append(MiddlewareStep("context_injection", "injected", detail=detail))

        # ── ③ Internal Grounding ──
        grounder = GROUNDERS.get(name)
        if grounder is None:
            steps.append(MiddlewareStep("grounding", "skip", detail="no grounding needed"))
        else:
            grounded, asks = grounder(turn, params)
            if asks:
                steps.append(MiddlewareStep("grounding", "clarification_needed", detail=" / ".join(asks)))
                return _result(_clarify(turn, asks), t0, name, params, steps)
            if grounded is None:
                steps.append(MiddlewareStep("grounding", "skip", detail="실행 단계가 먼저 답한다"))
            else:
                params[GROUNDED] = grounded
                steps.append(MiddlewareStep("grounding", "grounded", detail=_brief(grounded)))

        # ── ④ Skill Execution ──
        running = MiddlewareStep("execution", "ok", detail=name)
        steps.append(running)
        try:
            outcome = registry.run_skill(turn, name, params)
        except Exception:
            running.status = "error"
            raise
    except SkillError as exc:
        # 스킬이 지금 상태와 안 맞는 인자를 거절했다(없는 후보 번호 등) — 부른 곳(llm·example·button)과 함께 남긴다(검토 L2)
        turn.record("error", {"stage": source, "params": original, "message": str(exc)})
        return _result(Outcome(answer=str(exc), failed=True), t0, name, params, steps)
    except Exception as exc:   # noqa: BLE001 — 기록을 남기고 위로(500)
        turn.record("error", {"stage": "skill", "error": f"{type(exc).__name__}: {exc}"[:500]})
        raise
    return _result(outcome, t0, name, params, steps)


# ── Permission Check ────────────────────────────────────────


def _check_permission(name: str, source: str) -> Optional[str]:
    """부른 곳이 그 도구를 부를 수 있나 · 쓰기는 버튼으로만(LLM·예시는 읽기·미리보기만)."""
    if source not in SOURCES:
        return f"알 수 없는 호출 경로: {source}"
    kind = registry.kind_of(name)
    if kind is None:
        return f"수정 도우미에 없는 도구: {name}"
    if kind != _ALLOWED_KIND[source]:
        return f"{source} 에서 부를 수 없는 도구: {name}"
    if registry.mode_of(name) == "write" and source != "button":
        return f"쓰기는 버튼으로만: {name}"
    return None


# ── Context Injection ───────────────────────────────────────


def _inject_context(name: str, params: Dict[str, Any], source: str):
    """(인자, 버린 칸). 확정 칸은 늘 미들웨어가 채운다. LLM·예시 인자는 도구 정의에 있는 칸만 — 세션이 아는 값
    (schedule_id·오늘·달)은 정의에 없어 받지 않는다. 버튼 인자는 서버가 만든 것이라 그대로 둔다."""
    dropped = [GROUNDED] if GROUNDED in params else []
    params = {k: v for k, v in params.items() if k != GROUNDED}
    if source != "button":
        allowed = _PROPERTIES.get(name, set())
        dropped += [k for k in params if k not in allowed]
        params = {k: v for k, v in params.items() if k in allowed}
    return params, dropped


def _context_detail(turn: Turn) -> str:
    ctx = turn.session.ctx
    return (f"schedule_id={turn.session.schedule_id}, today={turn.today.isoformat()}, "
            f"month={ctx.year}-{ctx.month:02d}, 고칠 수 있는 첫날={turn.open_from() + 1}일")


# ── Trace ───────────────────────────────────────────────────


def _brief(grounded: Dict[str, Any]) -> str:
    """확정한 값 한 줄 — 기록에 남길 만큼만(요청·칸은 개수, 버전은 VER 번호)."""
    parts = []
    for key, value in grounded.items():
        if hasattr(value, "schedule_id"):
            parts.append(f"{key}=VER{getattr(value, 'version', '?')}")
        elif isinstance(value, (list, tuple, set, frozenset)):
            parts.append(f"{key}={len(value)}개")
        elif value is not None:
            parts.append(f"{key}={value}")
    return ", ".join(parts) or "확정할 값 없음"


def _result(outcome, t0: float, name: str, params: Dict[str, Any], steps: List[MiddlewareStep], *,
            block: Optional[str] = None) -> SkillResult:
    return SkillResult(data=outcome, duration_ms=(time.monotonic() - t0) * 1000, skill_name=name,
                       grounded_params=params, blocked=block is not None, block_reason=block, middleware_steps=steps)
