"""수정 도우미 핸들러 표 + 매니페스트 조회(S5 · 11-06).

정본은 AIDE 매니페스트 스펙(`agents_v2.skills.manifest.SKILL_SPECS` — agents·mode·sources·llm_visible)이다. 어떤 스킬이 있고
도구/버튼인지 · 읽기/미리보기/쓰기인지 · 어디서 부를 수 있는지는 거기서 읽는다 — 이 모듈에 따로 판정을 두지 않는다.
  · `register(name)` 데코레이터가 `SKILL_REGISTRY[name] = func(turn, params)` 만 채운다 — `aide_skills` 가 이 표로 (db, params) 감싸기를
    만들어 매니페스트에 올린다. 핸들러 본문은 `(turn, params)` 그대로 — 한 대화 안에서 판정 재료·요청·동의를 이어 가야 해
    db 대신 그것들을 든 `Turn` 을 받는다. 실행은 AIDE `execute_skill` 이 하므로 여기에 실행 분기는 없다.
  · `resolve`·`kind_of` 는 매니페스트를 읽는 조회다(하이픈·밑줄 정규화는 AIDE `find_spec` 과 같다).
  · 도구 정의(설명·인자·읽기/미리보기/쓰기)는 `descriptions.py` 한 곳에 있다 — 정의와 핸들러 표와 매니페스트가 어긋나면
    `definition_gaps()` 가 알린다(시험).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from agents_v2.profiles import SCHEDULE_EDIT
from agents_v2.skills.manifest import SKILL_SPECS, SkillSpec, find_spec, load_manifest_skills
from schedule_edit_agent.descriptions import BUTTON_TOOLS, SKILL_MODES, SKILL_TOOLS

#: (turn, params) → Outcome — 타입은 `skills.Turn`·`skills.Outcome`(순환 import 를 피해 Any 로 둔다)
SkillFunc = Callable[[Any, Dict[str, Any]], Any]

SKILL_REGISTRY: Dict[str, SkillFunc] = {}

TOOL_NAMES = frozenset(t["name"] for t in SKILL_TOOLS)
BUTTON_NAMES = frozenset(b["name"] for b in BUTTON_TOOLS)


def register(name: str):
    """Decorator — 이름은 `descriptions.py` 의 정의 이름과 같아야 한다."""
    def decorator(func: SkillFunc) -> SkillFunc:
        SKILL_REGISTRY[name] = func
        return func
    return decorator


def _spec(skill_name: Optional[str]) -> Optional[SkillSpec]:
    """수정 도우미 스펙(agents 에 schedule-edit 가 든 것) — AIDE 스킬 이름이면 None."""
    load_manifest_skills()
    spec = find_spec(str(skill_name or ""))
    return spec if spec is not None and SCHEDULE_EDIT in spec.agents else None


def resolve(skill_name: Optional[str]) -> Optional[str]:
    """정의된 이름으로 — 그대로, 하이픈, 밑줄 순(AIDE 와 같은 정규화). 수정 도우미 스킬이 아니면 None."""
    spec = _spec(skill_name)
    return spec.name if spec is not None else None


def kind_of(skill_name: Optional[str]) -> Optional[str]:
    """'tool'(LLM 이 고른다 · llm_visible) · 'button'(카드 버튼) · None(수정 도우미에 없는 이름)."""
    spec = _spec(skill_name)
    if spec is None:
        return None
    return "tool" if spec.llm_visible else "button"


def definition_gaps() -> List[str]:
    """정의는 있는데 핸들러·모드·매니페스트가 없거나, 핸들러·모드·매니페스트는 있는데 정의가 없는 이름, AIDE 표의 함수가 매니페스트 핸들러와 다른(덮어쓴) 이름."""
    _ensure_loaded()
    load_manifest_skills()
    defined = TOOL_NAMES | BUTTON_NAMES
    listed = {n for n, s in SKILL_SPECS.items() if SCHEDULE_EDIT in s.agents}
    from agents_v2.skills import registry as aide_registry
    aide_registry._ensure_loaded()     # 지연 적재되는 옛 @register 가 같은 이름을 덮어쓰지 않았는지도 본다
    overwritten = {n for n in defined & listed if aide_registry.SKILL_REGISTRY.get(n) is not SKILL_SPECS[n].handler}
    return sorted((defined ^ set(SKILL_REGISTRY)) | (defined ^ set(SKILL_MODES)) | (defined ^ listed) | overwritten)


_loaded = False


def _ensure_loaded() -> None:
    """스킬 모듈을 불러 핸들러 등록을 끝낸다."""
    global _loaded
    if _loaded:
        return
    import schedule_edit_agent.skills  # noqa: F401 — 등록
    _loaded = True
