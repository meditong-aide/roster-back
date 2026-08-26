"""병동이동 × 발행본 가드 (assignment_service).

배경: 발행본은 간호사에게 이미 배포된 확정 근무표다. 그 달 표는 옛 병동 기준으로
만들어졌는데 원장(nurse_assignments)만 바꾸면 재생성·이관 이력이 배포본과 어긋난다.
가드가 **서비스단**에 있어야 에이전트·프론트 REST 두 경로가 같이 막힌다.

결정(2026-08-26): 발행본만 막고 **과거월 자체는 통과**시킨다(소급 정정 허용).
"""
from datetime import date

import pytest
from fastapi import HTTPException

from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import Group, IssuedRosterSnapshot, NurseAssignment, Schedule


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


@pytest.fixture
def target_ward(db, seed_data):
    db.add(Group(group_id="GRP002", office_id="OFF001", group_name="중환자실2"))
    db.flush()


def _issue(db, group_id, year, month, *, active=True, sid="SCHISS0001"):
    """그 달 발행본 스냅샷 1건."""
    db.add(Schedule(schedule_id=sid, office_id="OFF001", group_id=group_id,
                    year=year, month=month, version=1, status="issued", dropped=False))
    db.flush()
    db.add(IssuedRosterSnapshot(office_id="OFF001", group_id=group_id, schedule_id=sid,
                                version=1, is_active_issued=active, year=year, month=month))
    db.flush()


def _transfer(nurse="박지은", start="2026-09-01", preview=False):
    return {"operation": "create", "kind": "병동이동", "nurse_name": nurse,
            "target_ward": "중환자실2", "start_date": start, "preview_only": preview}


# ── 차단 ────────────────────────────────────────────────


def test_blocked_when_effective_month_is_issued(db, seed_data, target_ward):
    _issue(db, "GRP001", 2026, 9)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert "error" in res.data
    assert "발행된 근무표" in res.data["error"]
    assert db.query(NurseAssignment).count() == 0


def test_blocked_when_later_month_is_issued(db, seed_data, target_ward):
    """발효월 이후 달이 발행돼 있어도 막는다 — 그 표도 옛 소속 기준이다."""
    _issue(db, "GRP001", 2026, 11)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert "error" in res.data and "2026년 11월" in res.data["error"]


def test_error_lists_blocking_months(db, seed_data, target_ward):
    _issue(db, "GRP001", 2026, 9, sid="SCHISS0009")
    _issue(db, "GRP001", 2026, 10, sid="SCHISS0010")
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert "2026년 9월" in res.data["error"] and "2026년 10월" in res.data["error"]


# ── 통과 ────────────────────────────────────────────────


def test_allowed_when_issued_month_is_before_effective(db, seed_data, target_ward):
    """발효월 이전 발행본은 무관 — 그 달은 옛 소속이 맞다."""
    _issue(db, "GRP001", 2026, 7)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert res.data.get("ok") is True, res.data


def test_allowed_when_issue_deactivated(db, seed_data, target_ward):
    """발행 취소(is_active_issued=False)면 다시 이동 가능해야 한다."""
    _issue(db, "GRP001", 2026, 9, active=False)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert res.data.get("ok") is True, res.data


def test_past_month_still_allowed(db, seed_data, target_ward):
    """★ 결정: 과거월 자체는 막지 않는다(소급 정정 허용)."""
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-03-01"), _hn())
    assert res.data.get("ok") is True, res.data
    row = db.query(NurseAssignment).first()
    assert row.start_date == date(2026, 3, 1)


def test_draft_schedule_does_not_block(db, seed_data, target_ward):
    """초안(발행 안 된 Schedule)만 있으면 막지 않는다 — 재생성하면 되니까."""
    db.add(Schedule(schedule_id="SCHDRAFT01", office_id="OFF001", group_id="GRP001",
                    year=2026, month=9, version=1, status="draft", dropped=False))
    db.flush()
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert res.data.get("ok") is True, res.data


def test_other_ward_issue_does_not_block(db, seed_data, target_ward):
    """판정 스코프는 source 그룹 — 남의 병동 발행본은 무관."""
    _issue(db, "GRP002", 2026, 9)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert res.data.get("ok") is True, res.data


def test_dispatch_not_blocked(db, seed_data, target_ward):
    """파견은 이 가드 대상이 아니다(임시 근무, 소속 유지)."""
    _issue(db, "GRP001", 2026, 9)
    res = execute_skill(db, "manage_assignment",
                        {"operation": "create", "kind": "파견", "nurse_name": "박지은",
                         "target_ward": "중환자실2", "start_date": "2026-09-01",
                         "end_date": "2026-09-30", "preview_only": False}, _hn())
    assert res.data.get("ok") is True, res.data


# ── 서비스단이라 REST 경로도 막히는가 ──────────────────


def test_guard_lives_in_service_not_only_skill(db, seed_data, target_ward):
    """스킬을 우회해 서비스를 직접 불러도 막혀야 한다(프론트 REST 경로 등가)."""
    from schemas.roster_schema import NurseAssignmentCreate
    from services import assignment_service

    _issue(db, "GRP001", 2026, 9)
    req = NurseAssignmentCreate(
        nurse_id="N002", source_group_id="GRP001", office_id="OFF001",
        target_group_id="GRP002", start_date=date(2026, 9, 1),
        reason="병동이동",
    )
    with pytest.raises(HTTPException) as ei:
        assignment_service.create_assignment(req, db, current_user=None, notify=False)
    assert ei.value.status_code == 409
    assert "발행된 근무표" in str(ei.value.detail)
