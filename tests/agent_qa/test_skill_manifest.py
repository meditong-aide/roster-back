"""Skill manifest — 단일 소스 선언 + 파생(derive) 검증.

@skill 데코레이터 하나로 스키마·카테고리·mutation·hn_only 를 선언하면 흩어진
소비 지점(descriptions/router/middleware)이 파생하는지 확인한다. 마지막 테스트는
SKILL_TOOLS(스키마) ↔ SKILL_REGISTRY(핸들러) 정합을 잠가 드리프트를 차단한다.
"""

from __future__ import annotations

import pytest

from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.manifest import (
    SKILL_SPECS,
    manifest_category_tools,
    manifest_hn_only_skills,
    manifest_mutation_skills,
    manifest_tools,
    skill,
)
from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded, run_skill

_NAME = "manifest_sample_skill"
_SCHEMA = {
    "name": _NAME,
    "description": "테스트용 샘플 스킬",
    "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
}


@pytest.fixture()
def sample_skill():
    @skill(
        _NAME, _SCHEMA,
        categories=["analyze"], mutation=True, hn_only=True, grounds=["nurse_name"],
    )
    def _handler(db, params):
        return {"ok": True, "echo": params.get("x")}

    yield _handler
    SKILL_SPECS.pop(_NAME, None)
    SKILL_REGISTRY.pop(_NAME, None)


def _ctx(role: str) -> SessionContext:
    return SessionContext(
        office_id="O", group_id="G", year=2026, month=5,
        nurse_id="N1", nurse_name="김민지", user_role=role,
    )


# ── 등록 ──────────────────────────────────────────────────────


def test_skill_registers_spec_and_handler(sample_skill):
    assert _NAME in SKILL_SPECS
    spec = SKILL_SPECS[_NAME]
    assert spec.mutation is True and spec.hn_only is True
    assert spec.categories == ("analyze",)
    assert spec.grounds == ("nurse_name",)
    # 핸들러가 기존 dispatch(SKILL_REGISTRY)에 그대로 등록됨
    assert SKILL_REGISTRY[_NAME] is sample_skill


def test_schema_name_mismatch_raises():
    with pytest.raises(ValueError):
        @skill("aaa", {"name": "bbb", "parameters": {}})
        def _h(db, p):
            return None


# ── 파생(derive) ──────────────────────────────────────────────


def test_manifest_derivation(sample_skill):
    assert any(s["name"] == _NAME for s in manifest_tools())
    assert _NAME in manifest_category_tools().get("analyze", [])
    assert _NAME in manifest_mutation_skills()
    assert _NAME in manifest_hn_only_skills()


def test_run_skill_dispatches_manifest_handler(sample_skill):
    out = run_skill(None, _NAME, {"x": 7})
    assert out == {"ok": True, "echo": 7}


# ── 권한 파생 (middleware) ────────────────────────────────────


def test_permission_derived_from_spec(sample_skill):
    from agents_v2.middleware import _check_permission

    # hn_only=True → 일반 간호사 차단, HN/ADM 통과
    assert _check_permission(_NAME, {}, _ctx("nurse")) is not None
    assert _check_permission(_NAME, {}, _ctx("HN")) is None
    assert _check_permission(_NAME, {}, _ctx("ADM")) is None


def test_permission_mutation_other_nurse_block():
    # hn_only=False, mutation=True 인 스킬: 본인 외 대상 차단 규칙이 파생되는지
    name = "manifest_self_only_skill"
    schema = {"name": name, "parameters": {"type": "object", "properties": {}}}

    @skill(name, schema, mutation=True, hn_only=False)
    def _h(db, params):
        return {"ok": True}

    try:
        from agents_v2.middleware import _check_permission

        # 본인 대상 → 통과
        assert _check_permission(name, {"nurse_name": "김민지"}, _ctx("nurse")) is None
        # 다른 간호사 대상 → 차단
        err = _check_permission(name, {"nurse_name": "이영희"}, _ctx("nurse"))
        assert err is not None and "권한이 없습니다" in err
    finally:
        SKILL_SPECS.pop(name, None)
        SKILL_REGISTRY.pop(name, None)


# ── 드리프트 차단: 스키마 ↔ 핸들러 정합 ───────────────────────


def test_every_tool_schema_has_handler_or_is_client_action():
    """SKILL_TOOLS 의 non-client-action tool 은 반드시 핸들러가 있어야 한다.

    스키마만 추가하고 핸들러 등록을 잊으면 런타임 KeyError → 이 테스트가 사전 차단.
    (기존엔 SKILL_TOOLS ↔ SKILL_REGISTRY 정합 테스트가 없었다.)
    """
    from agents_v2.skills.client_actions import _CLIENT_ACTION_NAMES
    from agents_v2.skills.descriptions import SKILL_TOOLS

    _ensure_loaded()
    for tool in SKILL_TOOLS:
        name = tool["name"]
        if name in _CLIENT_ACTION_NAMES:
            continue  # 프론트 위임 — run_skill 이 아니라 build_ui_action 이 처리
        variants = (name, name.replace("_", "-"), name.replace("-", "_"))
        assert any(v in SKILL_REGISTRY for v in variants), (
            f"{name}: 스키마는 SKILL_TOOLS 에 있는데 핸들러가 SKILL_REGISTRY 에 없음(드리프트)"
        )


def test_manifest_specs_schema_name_consistency():
    """모든 SkillSpec 의 schema['name'] 이 spec.name 과 일치 — 구조 불변식."""
    for name, spec in SKILL_SPECS.items():
        assert spec.schema.get("name") == name == spec.name


# ── postcondition 검증 게이트 (5요소의 '검증') ────────────────

_PC_NAME = "manifest_postcond_skill"
_PC_SCHEMA = {"name": _PC_NAME, "parameters": {"type": "object", "properties": {}}}


def test_postcondition_failure_becomes_verification_failed():
    from agents_v2.errors import ErrorType, classify
    from agents_v2.middleware import execute_skill

    @skill(_PC_NAME, _PC_SCHEMA, postcondition=lambda d: d.get("count", 0) > 0)
    def _h(db, params):
        return {"count": 0}  # error 없지만 성공조건(count>0) 미충족

    try:
        res = execute_skill(None, _PC_NAME, {}, _ctx("HN"))
        assert res.data.get("verification_failed") is True
        assert classify(res.data) is ErrorType.VERIFICATION_FAILED
        # 미들웨어 trace 에 verification 스텝 기록
        assert any(s.name == "verification" for s in res.middleware_steps)
    finally:
        SKILL_SPECS.pop(_PC_NAME, None)
        SKILL_REGISTRY.pop(_PC_NAME, None)


def test_postcondition_pass_returns_result():
    from agents_v2.errors import ErrorType, classify
    from agents_v2.middleware import execute_skill

    @skill(_PC_NAME, _PC_SCHEMA, postcondition=lambda d: d.get("count", 0) > 0)
    def _h(db, params):
        return {"count": 5}

    try:
        res = execute_skill(None, _PC_NAME, {}, _ctx("HN"))
        assert res.data == {"count": 5}
        assert classify(res.data) is ErrorType.OK
    finally:
        SKILL_SPECS.pop(_PC_NAME, None)
        SKILL_REGISTRY.pop(_PC_NAME, None)


def test_no_postcondition_skips_gate():
    from agents_v2.middleware import execute_skill

    @skill(_PC_NAME, _PC_SCHEMA)  # postcondition 없음
    def _h(db, params):
        return {"count": 0}

    try:
        res = execute_skill(None, _PC_NAME, {}, _ctx("HN"))
        assert res.data == {"count": 0}  # 검증 스킵 → 그대로 통과
    finally:
        SKILL_SPECS.pop(_PC_NAME, None)
        SKILL_REGISTRY.pop(_PC_NAME, None)
