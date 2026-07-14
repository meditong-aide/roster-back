"""manage_assignment 스킬 (매니페스트 첫 시민) + resolve_group 그라운딩.

파견 조회/생성-preview/취소 + 병동명→group_id 해석 + HN 전용 권한(매니페스트 파생) 검증.
"""

from __future__ import annotations

import pytest

from agents_v2.errors import ErrorType, classify
from agents_v2.grounding.internal import resolve_group
from agents_v2.middleware import _check_permission, execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import Group


def _hn_ctx() -> SessionContext:
    return SessionContext(
        office_id="OFF001", group_id="GRP001", year=2026, month=8,
        nurse_id="N001", nurse_name="김민지", user_role="HN",
    )


def _nurse_ctx() -> SessionContext:
    return SessionContext(
        office_id="OFF001", group_id="GRP001", year=2026, month=8,
        nurse_id="N003", nurse_name="이수정", user_role="nurse",
    )


@pytest.fixture()
def target_ward(db, seed_data):
    g = Group(group_id="GRP002", office_id="OFF001", group_name="중환자실2")
    db.add(g)
    db.flush()
    return g


# ── resolve_group (병동명 → group_id) ─────────────────────────


def test_resolve_group_exact(db, seed_data, target_ward):
    r = resolve_group(db, "OFF001", "중환자실2")
    assert r.resolved is True and r.value == "GRP002"


def test_resolve_group_not_found(db, seed_data):
    r = resolve_group(db, "OFF001", "없는병동")
    assert r.error is not None


def test_resolve_group_excludes_source(db, seed_data):
    # source(9병동=GRP001) 제외 시 후보에서 빠져 못 찾음
    r = resolve_group(db, "OFF001", "9병동", exclude_group_id="GRP001")
    assert r.error is not None


# ── 권한: 매니페스트에서 파생된 HN 전용 게이트 ────────────────


def test_manage_assignment_hn_only():
    assert _check_permission("manage_assignment", {"operation": "list"}, _nurse_ctx()) is not None
    assert _check_permission("manage_assignment", {"operation": "list"}, _hn_ctx()) is None


# ── list ──────────────────────────────────────────────────────


def test_list_month_empty(db, seed_data):
    res = execute_skill(db, "manage_assignment", {"operation": "list"}, _hn_ctx())
    assert res.data.get("scope") == "month"
    assert res.data.get("dispatches") == []


def test_list_by_nurse_empty(db, seed_data):
    res = execute_skill(
        db, "manage_assignment", {"operation": "list", "nurse_name": "김민지"}, _hn_ctx()
    )
    assert res.data.get("scope") == "nurse"
    assert res.data.get("assignments") == []


# ── create (preview) ──────────────────────────────────────────


def test_create_preview_shape(db, seed_data, target_ward):
    res = execute_skill(
        db, "manage_assignment",
        {
            "operation": "create", "nurse_name": "김민지", "target_ward": "중환자실2",
            "start_date": "2026-08-01", "end_date": "2026-08-31", "preview_only": True,
        },
        _hn_ctx(),
    )
    d = res.data
    assert d.get("preview") is True
    assert d["summary"]["nurse"] == "김민지"
    assert d["summary"]["to_ward"] == "중환자실2"
    assert d["summary"]["reason"] == "파견"
    # outcome taxonomy 가 PREVIEW 로 분류 → 승인 플로우에 물림
    assert classify(d) is ErrorType.PREVIEW


def test_reason_of_kind():
    from agents_v2.skills.manage_assignment import _reason_of
    assert _reason_of({"kind": "병동이동"}) == "병동이동"
    assert _reason_of({"kind": "파견"}) == "파견"
    assert _reason_of({}) == "파견"  # 기본


def test_transfer_create_preview(db, seed_data, target_ward):
    # 병동이동: reason=병동이동, 영구(종료일 무시), preview
    res = execute_skill(
        db, "manage_assignment",
        {
            "operation": "create", "kind": "병동이동", "nurse_name": "김민지",
            "target_ward": "중환자실2", "start_date": "2026-08-01",
            "end_date": "2026-08-31",  # 병동이동은 무시돼야
            "preview_only": True,
        },
        _hn_ctx(),
    )
    d = res.data
    assert d.get("preview") is True
    assert d["summary"]["reason"] == "병동이동"
    assert "영구" in d["summary"]["period"]  # end_date 무시 → 영구
    assert d["summary"]["effective_month"] == "2026-08"  # 발효월 확인용
    # group_id 로 해석돼 target 병동이 이름으로 표시(그룹명 그대로 저장 아님)
    assert d["summary"]["to_ward"] == "중환자실2"
    assert classify(d) is ErrorType.PREVIEW


def test_transfer_normalizes_midmonth(db, seed_data, target_ward):
    # 병동이동은 월 단위 발효 → 월 중간 날짜도 그 달 1일(발효월)로 정규화
    res = execute_skill(
        db, "manage_assignment",
        {
            "operation": "create", "kind": "병동이동", "nurse_name": "김민지",
            "target_ward": "중환자실2", "start_date": "2026-08-15", "preview_only": True,
        },
        _hn_ctx(),
    )
    assert res.data["summary"]["effective_month"] == "2026-08"
    assert "8월" in res.data["summary"]["period"]


def test_transfer_clarifies_missing_month(db, seed_data, target_ward):
    # 발효월을 모르면 임의로 넣지 말고 되물어야 한다 (assignment 오삽입 방지)
    res = execute_skill(
        db, "manage_assignment",
        {
            "operation": "create", "kind": "병동이동", "nurse_name": "김민지",
            "target_ward": "중환자실2", "preview_only": True,
        },
        _hn_ctx(),
    )
    assert res.data.get("needs_clarification") is True
    assert "월" in res.data["question"]


def test_transfer_clarifies_missing_ward(db, seed_data):
    # 병동 미지정 → 에러가 아니라 clarify
    res = execute_skill(
        db, "manage_assignment",
        {
            "operation": "create", "kind": "병동이동", "nurse_name": "김민지",
            "start_date": "2026-08-01", "preview_only": True,
        },
        _hn_ctx(),
    )
    assert res.data.get("needs_clarification") is True
    assert "병동" in res.data["question"]


def test_update_person_attr_rejects_group_id(db, seed_data):
    # 병동이동을 update_person_attr(group_id)로 시도 → 차단(병동이동 스킬로 유도)
    from agents_v2.skills.update_person_attr import update_person_attr
    res = update_person_attr(db, {
        "nurse_ids": ["N001"], "group_id": "GRP001",
        "field": "group_id", "value": "GRP002",
    })
    assert "error" in res and "병동이동" in res["error"]


def test_create_unknown_ward_errors(db, seed_data):
    res = execute_skill(
        db, "manage_assignment",
        {
            "operation": "create", "nurse_name": "김민지", "target_ward": "없는병동",
            "start_date": "2026-08-01", "preview_only": True,
        },
        _hn_ctx(),
    )
    assert "error" in res.data


def test_create_missing_start_date_clarifies(db, seed_data, target_ward):
    res = execute_skill(
        db, "manage_assignment",
        {"operation": "create", "nurse_name": "김민지", "target_ward": "중환자실2", "preview_only": True},
        _hn_ctx(),
    )
    assert res.data.get("needs_clarification") is True


# ── cancel ────────────────────────────────────────────────────


def test_cancel_no_active_dispatch(db, seed_data):
    res = execute_skill(
        db, "manage_assignment",
        {"operation": "cancel", "nurse_name": "김민지", "preview_only": True},
        _hn_ctx(),
    )
    assert "error" in res.data


# ── nurse 는 차단(e2e) ────────────────────────────────────────


def test_nurse_blocked_e2e(db, seed_data):
    res = execute_skill(db, "manage_assignment", {"operation": "list"}, _nurse_ctx())
    assert res.data.get("permission_denied") is True
