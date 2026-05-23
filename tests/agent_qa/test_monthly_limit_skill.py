"""QA-8: NurseMonthlyLimit skill bridge — tools + skill + query_schedule scope."""

from __future__ import annotations

import pytest

from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded
from agents_v2.tools import nurse_monthly_limit_tools
from agents_v2.middleware import _check_permission
from agents_v2.schemas.session_context import SessionContext

from tests.agent_qa.harness import AgentTestSession, ScriptedClient, ScriptedResponse


_ensure_loaded()


# ── registry / structural ───────────────────────────────


def test_update_monthly_limit_registered():
    """skill 이 registry 에 등록되었는지."""
    assert "update-monthly-limit" in SKILL_REGISTRY or "update_monthly_limit" in SKILL_REGISTRY


def test_limit_fields_complete():
    """LIMIT_FIELDS 가 d/e/n/o × min/max/exact = 12 개."""
    assert len(nurse_monthly_limit_tools.LIMIT_FIELDS) == 12
    for shift in ("d", "e", "n", "o"):
        for bound in ("min", "max", "exact"):
            assert f"{shift}_{bound}" in nurse_monthly_limit_tools.LIMIT_FIELDS


# ── tools: get / list ───────────────────────────────────


def test_get_monthly_limit_missing_returns_none(db):
    """미설정 nurse → None."""
    result = nurse_monthly_limit_tools.get_monthly_limit(
        db, "N001", "GRP001", 2026, 5
    )
    assert result is None


def test_upsert_create_new_then_get(db):
    """신규 row 생성 → get 으로 다시 확인 가능."""
    res = nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 5,
        {"n_exact": 4},
        preview_only=False,
    )
    assert res["preview"] is False
    assert "changes" in res and "n_exact" in res["changes"]

    got = nurse_monthly_limit_tools.get_monthly_limit(db, "N001", "GRP001", 2026, 5)
    assert got is not None
    assert got["n_exact"] == 4


def test_upsert_preview_only_does_not_persist(db):
    """preview_only=True 면 DB 미적용."""
    res = nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N002", "GRP001", 2026, 5,
        {"d_min": 8},
        preview_only=True,
    )
    assert res["preview"] is True
    assert "changes" in res
    # DB 에 row 가 없어야 함
    got = nurse_monthly_limit_tools.get_monthly_limit(db, "N002", "GRP001", 2026, 5)
    assert got is None


def test_upsert_invalid_field_rejected(db):
    res = nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 5,
        {"x_invalid": 1, "n_exact": 4},
        preview_only=True,
    )
    assert "error" in res
    assert "x_invalid" in str(res["error"])


def test_list_monthly_limits_returns_all(db):
    """upsert 후 list 가 해당 row 반환."""
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 6, {"n_max": 5}, preview_only=False,
    )
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N002", "GRP001", 2026, 6, {"d_exact": 10}, preview_only=False,
    )
    rows = nurse_monthly_limit_tools.list_monthly_limits(db, "GRP001", 2026, 6)
    nurse_ids = [r["nurse_id"] for r in rows]
    assert "N001" in nurse_ids
    assert "N002" in nurse_ids


# ── permission: update_monthly_limit ─────────────────────


def test_permission_nurse_role_blocked():
    err = _check_permission(
        "update_monthly_limit",
        {"nurse_ids": ["N002"], "n_exact": 4},
        SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=5,
                       nurse_id="N001", nurse_name="김민지", user_role="nurse"),
    )
    assert err is not None
    assert "수간호사" in err or "ADM" in err


def test_permission_hn_role_allowed():
    err = _check_permission(
        "update_monthly_limit",
        {"nurse_ids": ["N002"], "n_exact": 4},
        SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=5,
                       nurse_id="N001", nurse_name="김민지", user_role="HN"),
    )
    assert err is None


# ── query_schedule scope=monthly_limit ─────────────────


def test_query_schedule_monthly_limit_scope_via_skill(db):
    """미설정 상태 + 단일 nurse 조회 → limit_set=False 반환."""
    from agents_v2.skills.query_schedule import query_schedule
    res = query_schedule(db, {
        "scope": "monthly_limit",
        "group_id": "GRP001",
        "year": 2026,
        "month": 7,
        "nurse_ids": ["N001"],
    })
    assert res["limit_set"] is False


def test_query_schedule_monthly_limit_scope_with_data(db):
    """upsert 후 같은 nurse 조회 → 값 반환."""
    from agents_v2.skills.query_schedule import query_schedule
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 8, {"n_exact": 3}, preview_only=False,
    )
    res = query_schedule(db, {
        "scope": "monthly_limit",
        "group_id": "GRP001",
        "year": 2026,
        "month": 8,
        "nurse_ids": ["N001"],
    })
    assert res["limit_set"] is True
    assert res["n_exact"] == 3


# ── end-to-end via agent dispatch ──────────────────────


def test_agent_dispatches_update_monthly_limit_via_scripted(db):
    """LLM 이 update_monthly_limit 을 호출하면 agent 가 정확히 dispatch."""
    cli = ScriptedClient([
        ScriptedResponse(tool_calls=[{
            "name": "update_monthly_limit",
            "args": {
                "nurse_ids": ["N001"],
                "year": 2026,
                "month": 5,
                "n_exact": 4,
                "preview_only": True,
            },
        }])
    ])
    sess = AgentTestSession(db, client=cli, user_role="HN")
    sess.send("김민지 5월 N 4번으로 맞춰줘")
    sess.assert_tool_called("update_monthly_limit", {"preview_only": True})
