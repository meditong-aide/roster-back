"""S3(11-06) — 에이전트 프로필(허용 목록)과 ⓪ 범위 단계.

근무표 수정 도우미(schedule-edit) 프로필로는 근무표 수정 밖의 AIDE 도구(조회·일괄 변경·화면 이동·생성)를 부를 수 없고,
모르는 에이전트는 거절되며, 쓰기 스킬은 버튼에서 온 호출만 받는다. aide 프로필은 지금 동작 그대로다.
실 LLM·실 DB 없음(db=None — 감사 기록은 조용히 건너뛴다).
"""
from __future__ import annotations

import pytest
from agents_v2 import middleware
from agents_v2.middleware import (
    SCOPE_OUT_OF_PROFILE,
    SCOPE_UNKNOWN_AGENT,
    SCOPE_WRITE_SOURCE,
    execute_skill,
)
from agents_v2.profiles import AIDE, PROFILES, SCHEDULE_EDIT, in_scope, profile_of
from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.manifest import SKILL_SPECS, load_manifest_skills, skill
from agents_v2.skills.registry import SKILL_REGISTRY


def _ctx(agent_id: str = AIDE, source: str = "llm", role: str = "HN") -> SessionContext:
    return SessionContext(office_id="O", group_id="G", year=2026, month=11, nurse_id="N1",
                          nurse_name="김민지", user_role=role, agent_id=agent_id, source=source)


def _schema(name: str) -> dict:
    return {"name": name, "parameters": {"type": "object", "properties": {}}}


@pytest.fixture
def edit_skills():
    """수정 도우미 소속 가짜 스킬 — 읽기 하나·쓰기(버튼 전용) 하나."""
    load_manifest_skills()
    calls = []
    read, write = "se_scope_read", "se_scope_apply"

    @skill(read, _schema(read), agents=(SCHEDULE_EDIT,), mode="read")
    def _read(db, params):
        calls.append(read)
        return {"ok": True}

    @skill(write, _schema(write), agents=(SCHEDULE_EDIT,), mode="write", sources=("button",),
           llm_visible=False, commit="button")
    def _write(db, params):
        calls.append(write)
        return {"ok": True}

    try:
        yield read, write, calls
    finally:
        for name in (read, write):
            SKILL_SPECS.pop(name, None)
            SKILL_REGISTRY.pop(name, None)


@pytest.fixture
def audits(monkeypatch):
    rows = []
    monkeypatch.setattr(middleware, "_write_skill_audit",
                        lambda db, ctx, name, args, status, err, dt: rows.append((name, status, err)))
    return rows


@pytest.mark.parametrize("name", ["query_schedule", "bulk_mutation", "navigate", "generate_schedule",
                                  "query-schedule"])
def test_schedule_edit_profile_refuses_other_aide_tools(name, audits):
    res = execute_skill(None, name, {}, _ctx(SCHEDULE_EDIT))
    assert res.blocked and res.block_reason == SCOPE_OUT_OF_PROFILE
    assert res.data == {"error": SCOPE_OUT_OF_PROFILE, "permission_denied": True}
    assert [s.name for s in res.middleware_steps] == ["scope"]           # ① 이후 단계로 가지 않는다
    assert audits == [(name, "DENIED", SCOPE_OUT_OF_PROFILE)]


def test_unknown_agent_is_refused(audits):
    res = execute_skill(None, "query_schedule", {}, _ctx("no-such-agent"))
    assert res.blocked and res.block_reason == SCOPE_UNKNOWN_AGENT
    assert audits[-1][1] == "DENIED"


def test_schedule_edit_reads_its_own_skill(edit_skills, audits):
    read, _write, calls = edit_skills
    res = execute_skill(None, read, {}, _ctx(SCHEDULE_EDIT))
    assert not res.blocked and res.data == {"ok": True}
    assert calls == [read]
    assert res.middleware_steps[0].name == "scope" and res.middleware_steps[0].status == "pass"


@pytest.mark.parametrize("source", ["llm", "example"])
def test_schedule_edit_write_only_from_button(edit_skills, audits, source):
    _read, write, calls = edit_skills
    res = execute_skill(None, write, {}, _ctx(SCHEDULE_EDIT, source=source))
    assert res.blocked and res.block_reason == SCOPE_WRITE_SOURCE
    assert calls == []


def test_schedule_edit_write_from_button_runs(edit_skills, audits):
    _read, write, calls = edit_skills
    res = execute_skill(None, write, {}, _ctx(SCHEDULE_EDIT, source="button"))
    assert not res.blocked and res.data == {"ok": True}
    assert calls == [write]


def test_mutation_flag_without_mode_counts_as_write(audits):
    """mode 를 비우고 mutation=True 만 둔 파생 스킬도 쓰기다 — 말(llm)로 오면 막는다(검토 L5-a)."""
    name = "se_scope_mutation_only"
    calls = []

    @skill(name, _schema(name), agents=(SCHEDULE_EDIT,), mutation=True)
    def _h(db, params):
        calls.append(name)
        return {"ok": True}

    try:
        res = execute_skill(None, name, {}, _ctx(SCHEDULE_EDIT, source="llm"))
        assert res.blocked and res.block_reason == SCOPE_WRITE_SOURCE
        assert calls == []
    finally:
        SKILL_SPECS.pop(name, None)
        SKILL_REGISTRY.pop(name, None)


def test_hyphen_registered_skill_is_in_scope_by_any_spelling(audits):
    """하이픈 이름으로 등록한 파생 스킬을 밑줄 이름으로 불러도 같은 스킬로 보고 범위 판정한다(검토 L5-b)."""
    name = "se-scope-hyphen"

    @skill(name, _schema(name), agents=(SCHEDULE_EDIT,), mode="read")
    def _h(db, params):
        return {"ok": True}

    try:
        assert in_scope(SCHEDULE_EDIT, "se_scope_hyphen") and not in_scope(AIDE, "se_scope_hyphen")
        res = execute_skill(None, "se_scope_hyphen", {}, _ctx(SCHEDULE_EDIT))
        assert not res.blocked and res.data == {"ok": True}
    finally:
        SKILL_SPECS.pop(name, None)
        SKILL_REGISTRY.pop(name, None)


def test_aide_cannot_reach_schedule_edit_skills(edit_skills, audits):
    read, write, calls = edit_skills
    for name in (read, write):
        res = execute_skill(None, name, {}, _ctx(AIDE))
        assert res.blocked and res.block_reason == SCOPE_OUT_OF_PROFILE
    assert calls == []


def test_aide_profile_keeps_current_behaviour(audits):
    """aide 는 ⓪ 을 통과하고 지금처럼 다음 단계로 간다 — 모르는 이름도 ⓪ 이 아니라 실행 단계가 '모르는 스킬'로 답한다."""
    res = execute_skill(None, "no_such_skill_xyz", {}, _ctx(AIDE))
    assert not res.blocked
    assert res.middleware_steps[0].name == "scope" and res.middleware_steps[0].status == "pass"
    assert "Unknown skill" in res.data["error"]
    assert in_scope(AIDE, "query_schedule") and in_scope(AIDE, "navigate")


def test_default_context_is_aide_from_llm():
    ctx = SessionContext(office_id="O", group_id="G", year=2026, month=11)
    assert ctx.agent_id == AIDE and ctx.source == "llm" and ctx.turn is None
    assert "turn" not in repr(ctx)


def test_profiles_declare_the_schedule_edit_limits():
    edit = profile_of(SCHEDULE_EDIT)
    assert edit.write_sources == ("button",) and edit.max_llm_calls == 1 and edit.tool_choice == "required"
    assert not (edit.router or edit.memory or edit.l2 or edit.preview_summary or edit.parallel_reads)
    assert edit.planner == "code" and edit.approval == "button"
    aide = PROFILES[AIDE]
    assert aide.write_sources == () and aide.max_llm_calls is None and aide.approval == "text"
    assert profile_of(None) is None and profile_of("") is None
