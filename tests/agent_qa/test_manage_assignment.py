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
