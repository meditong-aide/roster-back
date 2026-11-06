"""스킬 레지스트리 — 이름 → 실행 함수(AIDE `agents_v2/skills/registry.py` 와 같은 모양 · 사용자 지시 11-06).

  · `register(name)` 데코레이터로 `SKILL_REGISTRY[name] = func(turn, params)` — AIDE 는 func(db, params) 다. 수정 도우미는
    한 대화 안에서 판정 재료·요청·동의를 이어 가야 해 db 대신 그것들을 든 `Turn`(db 포함)을 넘긴다.
  · `run_skill` 은 이름을 정규화한다(그대로 → 하이픈 → 밑줄 · AIDE 와 같다).
  · 도구 정의(설명·인자·읽기/미리보기/쓰기)는 `descriptions.py` 한 곳에 있다 — 등록 이름과 정의 이름이 어긋나면
    `definition_gaps()` 가 알린다(시험).
이 레지스트리는 수정 도우미 전용이다 — 자기 함수 그대로는 AIDE `SKILL_REGISTRY` 에 넣지 않고, AIDE 목록에는
`aide_skills` 가 (db, params) 로 감싼 함수만 올린다(S4). 거기 있는 AIDE 스킬은 여기서 부르지 않는다.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

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


def resolve(skill_name: Optional[str]) -> Optional[str]:
    """등록된 이름으로 — 그대로, 하이픈, 밑줄 순(AIDE `run_skill` 과 같은 정규화). 없으면 None."""
    _ensure_loaded()
    name = str(skill_name or "")
    for candidate in (name, name.replace("_", "-"), name.replace("-", "_")):
        if candidate in SKILL_REGISTRY:
            return candidate
    return None


def kind_of(skill_name: Optional[str]) -> Optional[str]:
    """'tool'(LLM 이 고른다) · 'button'(카드 버튼) · None(없는 이름)."""
    name = resolve(skill_name)
    if name in TOOL_NAMES:
        return "tool"
    if name in BUTTON_NAMES:
        return "button"
    return None


def mode_of(skill_name: str) -> Optional[str]:
    return SKILL_MODES.get(resolve(skill_name) or "")


def run_skill(turn: Any, skill_name: str, params: Dict[str, Any]) -> Any:
    """이름으로 실행. 없는 이름이면 KeyError(AIDE 와 같다)."""
    name = resolve(skill_name)
    if name is None:
        raise KeyError(f"Unknown skill: {skill_name}")
    return SKILL_REGISTRY[name](turn, params)


def definition_gaps() -> List[str]:
    """정의는 있는데 실행 함수가 없거나, 실행 함수는 있는데 정의(모드 포함)가 없는 이름."""
    _ensure_loaded()
    defined = TOOL_NAMES | BUTTON_NAMES
    return sorted((defined ^ set(SKILL_REGISTRY)) | (defined ^ set(SKILL_MODES)))


_loaded = False


def _ensure_loaded() -> None:
    """스킬 모듈을 불러 등록을 끝낸다(AIDE 와 같다)."""
    global _loaded
    if _loaded:
        return
    import schedule_edit_agent.skills  # noqa: F401 — 등록
    _loaded = True
