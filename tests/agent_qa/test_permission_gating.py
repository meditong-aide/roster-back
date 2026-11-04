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


def test_bulk_mutation_clear_deadline_blocked_for_nurse():
    """clear_deadline 도 update_deadline 과 동일하게 HN/ADM 권한 필요."""
    err = _check_permission(
        "bulk_mutation",
        {"scope": "wanted_submissions", "action": "clear_deadline"},
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


def test_bulk_mutation_other_wanted_by_nurse_ids_blocked_for_nurse():
    """이름 없이 nurse_ids 로 다른 간호사를 가리켜도 막는다 — 예전엔 이름만 봐서 통과했다(11-04 검토)."""
    err = _check_permission(
        "bulk_mutation",
        {"scope": "wanted_submissions", "action": "add_shift", "nurse_ids": ["N999"]},
        _ctx("nurse"),
    )
    assert err is not None


def test_bulk_mutation_own_nurse_ids_allowed_for_nurse_and_others_for_hn():
    own = {"scope": "wanted_submissions", "action": "add_shift", "nurse_ids": ["N001"]}
    assert _check_permission("bulk_mutation", own, _ctx("nurse")) is None
    other = {"scope": "wanted_submissions", "action": "add_shift", "nurse_ids": ["N999"]}
    assert _check_permission("bulk_mutation", other, _ctx("HN")) is None


def test_inject_context_overrides_caller_role_flag():
    """스킬이 간호사 본인 쓰기 규칙을 걸지 정하는 역할 표시는 세션 역할로 덮어쓴다(LLM 값 무시)."""
    from agents_v2.middleware import _inject_context

    spoofed = _inject_context({"_caller_is_manager": True}, _ctx("nurse"))
    assert spoofed["_caller_is_manager"] is False
    assert _inject_context({}, _ctx("HN"))["_caller_is_manager"] is True


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


# ── 그라운딩 뒤 본인 대상 재확인(간호사 쓰기) ───────────


def test_self_target_fills_missing_target_with_self_for_nurse():
    """대상을 비운 간호사 쓰기는 본인으로 채운다 — 비우면 근무표 일괄 변경이 병동 전원에 쓴다(11-04 리뷰)."""
    from agents_v2.middleware import _check_self_target

    args = {"scope": "schedule", "action": "change_shift", "group_id": "GRP001", "office_id": "OFF001"}
    assert _check_self_target("bulk_mutation", args, _ctx("nurse")) is None
    assert args["nurse_ids"] == ["N001"]


def test_self_target_blocks_other_nurse_or_other_ward_for_nurse():
    from agents_v2.middleware import _check_self_target

    grounded_other = {"nurse_ids": ["N077"], "group_id": "GRP001", "office_id": "OFF001"}  # 이름 해석이 남을 가리킴
    assert "다른 간호사" in _check_self_target("bulk_mutation", grounded_other, _ctx("nurse"))
    other_ward = {"nurse_ids": ["N001"], "group_id": "W9", "office_id": "OFF001"}          # LLM 이 다른 병동을 넣음
    assert "다른 병동" in _check_self_target("bulk_mutation", other_ward, _ctx("nurse"))


@pytest.mark.parametrize("role", ["HN", "ADM"])
def test_self_target_leaves_manager_writes(role):
    from agents_v2.middleware import _check_self_target

    args = {"scope": "schedule", "group_id": "GRP001", "office_id": "OFF001"}
    assert _check_self_target("bulk_mutation", args, _ctx(role)) is None
    assert "nurse_ids" not in args


def test_execute_skill_rechecks_target_after_grounding(monkeypatch):
    """이름 해석(③)이 남을 가리키면 실행 전에 막는다 — ① 권한 검사는 원본 인자(본인 이름)만 본다."""
    import agents_v2.middleware as mw

    monkeypatch.setattr(mw, "_ground_params", lambda db, gid, params: params.update(nurse_ids=["N077"]))
    monkeypatch.setattr(mw, "_write_skill_audit", lambda *a, **k: None)
    ran: list = []
    monkeypatch.setattr(mw, "run_skill", lambda db, name, args: ran.append(args) or {"ok": True})
    result = mw.execute_skill(
        None, "bulk_mutation",
        {"scope": "wanted_submissions", "action": "add_shift", "nurse_name": "김민지", "date": "2026-05-14"},
        _ctx("nurse"),
    )
    assert result.blocked and ran == []
