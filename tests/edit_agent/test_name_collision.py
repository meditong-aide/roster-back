"""S5(11-06) — 수정 도우미 스킬 20개 이름이 AIDE 의 매니페스트 스펙·옛 @register 스킬(지연 적재까지)과
하이픈·밑줄 구분 없이 겹치지 않고, 일부러 겹치는 이름을 등록하면 바로 예외가 난다."""

from __future__ import annotations

import pytest

from agents_v2.skills import registry as aide_registry
from agents_v2.skills.manifest import SKILL_SPECS, SkillSpec, load_manifest_skills
from schedule_edit_agent import aide_skills
from schedule_edit_agent.descriptions import BUTTON_TOOLS, SKILL_TOOLS

MINE = [t["name"] for t in SKILL_TOOLS + BUTTON_TOOLS]


def _norm(name: str) -> str:
    return name.replace("-", "_")


def test_the_twenty_names_are_unique_after_normalizing():
    assert len(MINE) == 20 and len({_norm(n) for n in MINE}) == 20


def test_no_overlap_with_aide_specs_or_lazily_loaded_old_skills():
    load_manifest_skills()
    aide_registry._ensure_loaded()          # 옛 @register 스킬은 지연 적재 — 여기서 전부 불러 견준다
    others = {_norm(n) for n, spec in SKILL_SPECS.items() if "schedule-edit" not in spec.agents}
    others |= {_norm(n) for n in aide_registry.SKILL_REGISTRY if n not in SKILL_SPECS}
    assert others, "견줄 AIDE 이름이 없다 — 적재가 안 됐다"
    assert not {_norm(n) for n in MINE} & others


@pytest.mark.parametrize("clash", ["lookup", "check-rules", "propose_edit", "apply-edit"])
@pytest.mark.parametrize("where", ["spec", "old_register"])
def test_a_deliberate_clash_raises_at_once(monkeypatch, clash, where):
    load_manifest_skills()
    for name in MINE:                         # 내 20개를 두 표에서 빼 둔 뒤 겹치는 이름만 심는다
        monkeypatch.delitem(SKILL_SPECS, name)
        monkeypatch.delitem(aide_registry.SKILL_REGISTRY, name)
    if where == "spec":                       # 매니페스트 스펙만 겹치게(옛 표에는 없다)
        schema = {"name": clash, "description": "x", "parameters": {"type": "object", "properties": {}}}
        monkeypatch.setitem(SKILL_SPECS, clash, SkillSpec(name=clash, schema=schema, handler=lambda db, params: {}))
    else:
        monkeypatch.setitem(aide_registry.SKILL_REGISTRY, clash, lambda db, params: {})
    with pytest.raises(RuntimeError, match="이미 등록"):
        aide_skills.register_all()


def test_every_handler_in_the_aide_table_is_ours_after_lazy_loading():
    from schedule_edit_agent import registry
    load_manifest_skills()
    assert registry.definition_gaps() == []
    aide_registry._ensure_loaded()
    assert all(aide_registry.SKILL_REGISTRY[n] is SKILL_SPECS[n].handler for n in MINE)


@pytest.mark.parametrize("clash", ["lookup", "propose_edit"])
def test_a_late_old_register_with_the_same_name_is_reported(monkeypatch, clash):
    # 우리 등록 뒤에 같은 이름의 옛 @register 가 늦게 들어와 감싼 함수를 덮어쓰는 순서
    from schedule_edit_agent import registry
    load_manifest_skills()
    aide_registry._ensure_loaded()
    monkeypatch.setitem(aide_registry.SKILL_REGISTRY, clash, lambda db, params: {"old": True})
    assert clash in registry.definition_gaps()
