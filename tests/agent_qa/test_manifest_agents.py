"""S2(11-06) — 매니페스트의 파생 에이전트 칸과 AIDE 목록 거르기.

한 매니페스트에 AIDE 와 파생 에이전트(근무표 수정 도우미 등) 스킬이 함께 살아도, AIDE 의 LLM 도구 목록·
라우터 카테고리·라우터 힌트는 "aide" 가 든 스킬만 받는다. 병합 직후 AIDE 도구 표면은 스냅샷과 같아야 한다.
실 LLM·실 DB 없음.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from agents_v2.skills.manifest import (
    SKILL_SPECS,
    find_spec,
    load_manifest_skills,
    manifest_category_tools,
    manifest_hn_only_skills,
    manifest_mutation_skills,
    manifest_router_hints,
    manifest_specs,
    manifest_tools,
    skill,
    skill_mode,
)
from agents_v2.skills.registry import SKILL_REGISTRY, run_skill

_SNAPSHOT = Path(__file__).parent / "fixtures" / "aide_tool_surface_snapshot.json"
_EDIT = "schedule-edit"


def _schema(name: str) -> dict:
    return {"name": name, "parameters": {"type": "object", "properties": {}}}


@pytest.fixture
def derived_skills():
    """수정 도우미 소속 가짜 스킬 2개(말로 고르는 것·버튼 전용) — 끝나면 지운다."""
    load_manifest_skills()
    names = ("se_fake_propose", "se_fake_apply")

    @skill(names[0], _schema(names[0]), categories=("analyze",), agents=(_EDIT,), mode="preview",
           trigger_hint="근무표 칸을 바꿔 달라는 말", commit="button")
    def _propose(db, params):
        return {"ok": True}

    @skill(names[1], _schema(names[1]), categories=("analyze",), agents=(_EDIT,), mode="write",
           mutation=True, hn_only=True, sources=("button",), llm_visible=False, commit="button")
    def _apply(db, params):
        return {"ok": True}

    try:
        yield names
    finally:
        for name in names:
            SKILL_SPECS.pop(name, None)
            SKILL_REGISTRY.pop(name, None)


def test_aide_tool_surface_matches_post_join_snapshot():
    from agents_v2.router import CATEGORY_TOOLS
    from agents_v2.skills.descriptions import SKILL_TOOLS

    snap = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    assert [t["name"] for t in SKILL_TOOLS] == snap["skill_tools"]
    # 이름만이 아니라 스키마(설명·인자)까지 — 병합 직후(HEAD 2c406e4) 사본에서 뜬 값(검토 L5)
    assert {t["name"]: _schema_hash(t) for t in SKILL_TOOLS} == snap["schema_hashes"]
    assert {k: list(v) for k, v in sorted(CATEGORY_TOOLS.items())} == snap["category_tools"]
    assert manifest_router_hints() == snap["router_hints"]


def _schema_hash(schema: dict) -> str:
    return hashlib.sha1(json.dumps(schema, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def test_untagged_specs_belong_to_aide():
    load_manifest_skills()
    assert SKILL_SPECS, "매니페스트 스킬이 하나도 없다"
    for spec in SKILL_SPECS.values():
        assert spec.agents == ("aide",), spec.name
        assert spec.commit == "rerun" and spec.sources == () and spec.llm_visible


def test_derived_skills_stay_out_of_aide_lists(derived_skills):
    propose, apply = derived_skills
    aide_tools = {s["name"] for s in manifest_tools()}
    assert propose not in aide_tools and apply not in aide_tools
    assert all(propose not in names and apply not in names for names in manifest_category_tools().values())
    assert "근무표 칸을 바꿔 달라는 말" not in manifest_router_hints()


def test_derived_skills_listed_for_their_agent_only_when_llm_visible(derived_skills):
    propose, apply = derived_skills
    assert [s.name for s in manifest_specs(_EDIT)] == [propose, apply]
    assert [s["name"] for s in manifest_tools(_EDIT)] == [propose]           # 버튼 전용은 LLM 에 안 보임
    assert manifest_category_tools(_EDIT) == {"analyze": [propose]}
    assert "근무표 칸을 바꿔 달라는 말" in manifest_router_hints(_EDIT)


def test_permission_gates_still_cover_derived_skills(derived_skills):
    _propose, apply = derived_skills
    assert apply in manifest_mutation_skills()
    assert apply in manifest_hn_only_skills()


def test_remerging_into_aide_lists_does_not_pull_derived_skills(derived_skills):
    """descriptions·router 는 import 때 한 번 병합한다 — 나중에 등록된 파생 스킬로 다시 병합해도 AIDE 목록은 그대로."""
    from agents_v2 import router
    from agents_v2.skills import descriptions

    before_tools = [t["name"] for t in descriptions.SKILL_TOOLS]
    before_cats = {k: list(v) for k, v in router.CATEGORY_TOOLS.items()}
    descriptions._merge_manifest_tools()
    router._merge_manifest_categories()
    assert [t["name"] for t in descriptions.SKILL_TOOLS] == before_tools
    assert {k: list(v) for k, v in router.CATEGORY_TOOLS.items()} == before_cats


def test_mode_comes_from_spec_not_llm(derived_skills):
    propose, apply = derived_skills
    assert skill_mode(propose) == "preview"
    assert skill_mode(apply.replace("_", "-")) == "write"                     # 하이픈 이름도 같은 스펙
    load_manifest_skills()
    assert skill_mode("manage_assignment") in ("read", "write")               # 비우면 mutation 에서
    assert skill_mode("no_such_skill") is None


def test_aide_execution_path_refuses_derived_skills(derived_skills):
    """파생 스킬도 같은 SKILL_REGISTRY 에 있지만, AIDE 계획이 그 이름을 적어도(환각·옛 기록) 실행하지 못한다(검토 M1)."""
    propose, apply = derived_skills
    for name in (propose, apply, apply.replace("_", "-")):
        with pytest.raises(KeyError):
            run_skill(None, name, {})                        # 기본 agent = aide
    assert run_skill(None, propose, {}, agent=_EDIT) == {"ok": True}


def test_untagged_registry_skills_stay_aide_only():
    """태그 없는 옛 @register 스킬은 aide 소속 — 파생 에이전트 이름으로는 못 부른다."""
    calls = []
    SKILL_REGISTRY["legacy_fake_skill"] = lambda db, params: calls.append(params) or {"ok": True}
    try:
        assert run_skill(None, "legacy-fake-skill", {"a": 1}) == {"ok": True}
        with pytest.raises(KeyError):
            run_skill(None, "legacy_fake_skill", {}, agent=_EDIT)
        assert calls == [{"a": 1}]
    finally:
        SKILL_REGISTRY.pop("legacy_fake_skill", None)


def test_find_spec_matches_any_spelling():
    """하이픈으로 등록한 스킬도 밑줄 이름으로, 그 반대도 찾는다(검토 L4)."""
    name = "se-fake-hyphen"

    @skill(name, _schema(name), agents=(_EDIT,), mode="read")
    def _h(db, params):
        return {}

    try:
        assert find_spec("se_fake_hyphen") is SKILL_SPECS[name]
        assert skill_mode("se_fake_hyphen") == "read"
    finally:
        SKILL_SPECS.pop(name, None)
        SKILL_REGISTRY.pop(name, None)


@pytest.mark.parametrize("kwargs", [
    {"mode": "delete"},
    {"commit": "auto"},
    {"agents": ()},
])
def test_bad_declarations_are_refused(kwargs):
    name = "se_fake_bad"
    with pytest.raises(ValueError):
        skill(name, _schema(name), **kwargs)
    assert name not in SKILL_SPECS
