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


# ── as-of 이월 조회 (생성 로더와 정합) ─────────────────────


def test_get_monthly_limit_carries_forward_from_past(db):
    """7월 설정 후 8월 미설정 → 8월 조회 시 7월값 이월(as-of). applied_from=7월."""
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 7, {"n_max": 4}, preview_only=False,
    )
    got = nurse_monthly_limit_tools.get_monthly_limit(db, "N001", "GRP001", 2026, 8)
    assert got is not None
    assert got["n_max"] == 4
    assert got["year"] == 2026 and got["month"] == 8         # 표시월=대상월
    assert got["applied_from_year"] == 2026 and got["applied_from_month"] == 7
    assert got["carried_over"] is True


def test_get_monthly_limit_asof_false_is_exact(db):
    """as_of=False 는 정확히 그 달 행만 — 8월엔 없음."""
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 7, {"n_max": 4}, preview_only=False,
    )
    assert nurse_monthly_limit_tools.get_monthly_limit(
        db, "N001", "GRP001", 2026, 8, as_of=False
    ) is None


def test_get_monthly_limit_same_month_not_carried(db):
    """대상월 본인 설정이 있으면 carried_over=False."""
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 8, {"n_max": 3}, preview_only=False,
    )
    got = nurse_monthly_limit_tools.get_monthly_limit(db, "N001", "GRP001", 2026, 8)
    assert got["carried_over"] is False
    assert got["applied_from_month"] == 8


def test_list_monthly_limits_asof_dedupes_latest(db):
    """list as-of: 7월·미래 없음 → 8월 조회 시 nurse별 최근 1건, 8월 표시."""
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 7, {"n_max": 4}, preview_only=False,
    )
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 8, {"n_max": 2}, preview_only=False,
    )
    rows = nurse_monthly_limit_tools.list_monthly_limits(db, "GRP001", 2026, 8)
    n001 = [r for r in rows if r["nurse_id"] == "N001"]
    assert len(n001) == 1                    # 7월 행은 8월 행에 가려짐
    assert n001[0]["n_max"] == 2
    assert n001[0]["carried_over"] is False


def test_query_schedule_carryover_message(db):
    """query_schedule 단일 조회가 이월 시 안내 메시지를 담는다."""
    from agents_v2.skills.query_schedule import query_schedule
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 7, {"n_max": 4}, preview_only=False,
    )
    res = query_schedule(db, {
        "scope": "monthly_limit", "group_id": "GRP001",
        "year": 2026, "month": 9, "nurse_ids": ["N001"],
    })
    assert res["limit_set"] is True and res["n_max"] == 4
    assert "이월" in res.get("message", "")
    assert res["applied_from_month"] == 7


# ── 해제(unset) = tombstone ──────────────────────────────


def test_unset_all_writes_tombstone_and_stops_inheritance(db):
    """전체 해제 → all-null 묘비. 이후 as-of 가 과거값을 재상속하지 않음."""
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 7, {"n_max": 4}, preview_only=False,
    )
    res = nurse_monthly_limit_tools.unset_monthly_limit(
        db, "N001", "GRP001", 2026, 8, preview_only=False,
    )
    assert res["preview"] is False and res["unset"] is True
    # 8월 묘비 행이 존재하고 모든 한도가 NULL
    row8 = nurse_monthly_limit_tools.get_monthly_limit(
        db, "N001", "GRP001", 2026, 8, as_of=False
    )
    assert row8 is not None
    assert all(row8[f] is None for f in nurse_monthly_limit_tools.LIMIT_FIELDS)
    # 9월 as-of 는 8월 묘비에서 멈춰 과거(7월 n_max=4) 재상속 안 함
    got9 = nurse_monthly_limit_tools.get_monthly_limit(db, "N001", "GRP001", 2026, 9)
    assert got9 is not None
    assert got9["n_max"] is None
    assert got9["applied_from_month"] == 8


def test_unset_single_shift_keeps_others(db):
    """야간만 해제 → n_* NULL, d_* 유지."""
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 8, {"n_max": 4, "d_min": 6}, preview_only=False,
    )
    nurse_monthly_limit_tools.unset_monthly_limit(
        db, "N001", "GRP001", 2026, 8, shifts=["n"], preview_only=False,
    )
    got = nurse_monthly_limit_tools.get_monthly_limit(
        db, "N001", "GRP001", 2026, 8, as_of=False
    )
    assert got["n_max"] is None
    assert got["d_min"] == 6


def test_unset_preview_does_not_persist(db):
    """해제 preview_only → DB 미기록."""
    res = nurse_monthly_limit_tools.unset_monthly_limit(
        db, "N002", "GRP001", 2026, 8, preview_only=True,
    )
    assert res["preview"] is True and res["unset"] is True
    assert nurse_monthly_limit_tools.get_monthly_limit(
        db, "N002", "GRP001", 2026, 8, as_of=False
    ) is None


def test_unset_unknown_shift_rejected(db):
    res = nurse_monthly_limit_tools.unset_monthly_limit(
        db, "N001", "GRP001", 2026, 8, shifts=["z"], preview_only=True,
    )
    assert "error" in res


def test_skill_unset_dispatch(db):
    """skill 진입점: unset 파라미터가 해제 경로로 라우팅."""
    from agents_v2.skills.registry import SKILL_REGISTRY
    fn = SKILL_REGISTRY.get("update-monthly-limit") or SKILL_REGISTRY["update_monthly_limit"]
    nurse_monthly_limit_tools.upsert_monthly_limit(
        db, "N001", "GRP001", 2026, 7, {"n_max": 4}, preview_only=False,
    )
    res = fn(db, {
        "nurse_ids": ["N001"], "group_id": "GRP001",
        "year": 2026, "month": 8, "unset": ["all"], "preview_only": False,
    })
    assert res.get("unset") is True
    got9 = nurse_monthly_limit_tools.get_monthly_limit(db, "N001", "GRP001", 2026, 9)
    assert got9["n_max"] is None


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
