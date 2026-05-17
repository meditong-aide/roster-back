"""QA-6: 권한 게이팅 — router/auth.py 의 is_head_nurse / is_master_admin 와 1:1 매칭.

검증 매트릭스:
  | skill                       | role=nurse (일반) | role=HN | role=ADM |
  |---                          |---                |---      |---       |
  | update_constraint           | BLOCK             | PASS    | PASS     |
  | generate_schedule           | BLOCK             | PASS    | PASS     |
  | bulk_mutation deadline      | BLOCK             | PASS    | PASS     |
  | bulk_mutation 본인 wanted    | PASS              | PASS    | PASS     |
  | update_person_attr 본인     | PASS              | PASS    | PASS     |
  | update_person_attr 타인     | BLOCK             | PASS    | PASS     |
  | update_monthly_limit (타인) | BLOCK             | PASS    | PASS     |
  | query_schedule (read)       | PASS              | PASS    | PASS     |
"""

from __future__ import annotations

import pytest

from agents_v2.middleware import _check_permission
from agents_v2.schemas.session_context import SessionContext


def _ctx(role: str, *, nurse_id: str = "N001", nurse_name: str = "김민지") -> SessionContext:
    return SessionContext(
        office_id="OFF001",
        group_id="GRP001",
        year=2026,
        month=5,
        nurse_id=nurse_id,
        nurse_name=nurse_name,
        user_role=role,
    )


# ── update_constraint — 병동 전체 정책 ───────────────────


def test_update_constraint_blocked_for_nurse():
    err = _check_permission(
        "update_constraint",
        {"field": "max_nig_per_month", "value": 7},
        _ctx("nurse"),
    )
    assert err is not None
    assert "수간호사" in err or "ADM" in err or "권한" in err


def test_update_constraint_allowed_for_hn():
    err = _check_permission(
        "update_constraint",
        {"field": "max_nig_per_month", "value": 7},
        _ctx("HN"),
    )
    assert err is None


def test_update_constraint_allowed_for_adm():
    err = _check_permission(
        "update_constraint",
        {"field": "max_nig_per_month", "value": 7},
        _ctx("ADM"),
    )
    assert err is None


# ── generate_schedule — 병동 영향 ──────────────────────


def test_generate_schedule_blocked_for_nurse():
    err = _check_permission("generate_schedule", {}, _ctx("nurse"))
    assert err is not None


def test_generate_schedule_allowed_for_hn():
    err = _check_permission("generate_schedule", {}, _ctx("HN"))
    assert err is None


# ── bulk_mutation — ward-wide action vs self ──────────


def test_bulk_mutation_deadline_blocked_for_nurse():
    err = _check_permission(
        "bulk_mutation",
        {"scope": "wanted_submissions", "action": "update_deadline", "new_deadline": "2026-05-20"},
        _ctx("nurse"),
    )
    assert err is not None
    assert "마감일" in err or "수간호사" in err


def test_bulk_mutation_self_wanted_cancel_allowed_for_nurse():
    err = _check_permission(
        "bulk_mutation",
        {"scope": "wanted_submissions", "action": "cancel", "nurse_name": "나"},
        _ctx("nurse"),
    )
    assert err is None


def test_bulk_mutation_other_wanted_cancel_blocked_for_nurse():
    err = _check_permission(
        "bulk_mutation",
        {"scope": "wanted_submissions", "action": "cancel", "nurse_name": "박혜미"},
        _ctx("nurse"),
    )
    assert err is not None


def test_bulk_mutation_wanted_adjustment_self_allowed_for_nurse():
    err = _check_permission(
        "bulk_mutation",
        {"scope": "wanted_adjustment", "nurse_name": "나"},
        _ctx("nurse"),
    )
    assert err is None


def test_bulk_mutation_wanted_adjustment_other_blocked_for_nurse():
    err = _check_permission(
        "bulk_mutation",
        {"scope": "wanted_adjustment", "nurse_name": "박혜미"},
        _ctx("nurse"),
    )
    assert err is not None


# ── update_person_attr — self vs other ────────────────


def test_update_person_attr_self_allowed_for_nurse():
    err = _check_permission(
        "update_person_attr",
        {"nurse_name": "나", "field": "wanted_max_requests", "value": 3},
        _ctx("nurse"),
    )
    assert err is None


def test_update_person_attr_other_blocked_for_nurse():
    err = _check_permission(
        "update_person_attr",
        {"nurse_name": "박혜미", "field": "grade", "value": 3},
        _ctx("nurse"),
    )
    assert err is not None


def test_update_person_attr_other_allowed_for_hn():
    err = _check_permission(
        "update_person_attr",
        {"nurse_name": "박혜미", "field": "grade", "value": 3},
        _ctx("HN"),
    )
    assert err is None


# ── update_monthly_limit ──────────────────────────────


def test_update_monthly_limit_blocked_for_nurse():
    err = _check_permission(
        "update_monthly_limit",
        {"nurse_ids": ["N002"], "n_exact": 4},
        _ctx("nurse"),
    )
    assert err is not None


def test_update_monthly_limit_allowed_for_hn():
    err = _check_permission(
        "update_monthly_limit",
        {"nurse_ids": ["N002"], "n_exact": 4},
        _ctx("HN"),
    )
    assert err is None


# ── read skills — anyone can ────────────────────────────


@pytest.mark.parametrize("role", ["nurse", "HN", "ADM"])
@pytest.mark.parametrize("skill", [
    "query_schedule",
    "validate_schedule",
    "analyze_report",
    "recommend_candidates",
    "repair_schedule",
])
def test_read_skills_allowed_for_all_roles(role, skill):
    err = _check_permission(skill, {}, _ctx(role))
    assert err is None
