"""schedule_id 진입 엔드포인트의 그룹 스코프 — 행의 group_id 가 진실, 권한만 검증.

GET /roster/schedule/{id}, /publish, /unpublish, /save, /copy, DELETE /{id},
PATCH /{id}/name, /export 는 모두 공통 헬퍼 _load_schedule_for_caller 로
스케줄을 로드한다. schedule_id 는 PK라 그룹을 이미 확정하므로, 호출자-resolve group 을
필터로 쓰던 기존 방식은 HN 이 home 아닌 관리병동의 스케줄을 group_id 미전송 시 404 로
숨기는 버그가 있었다(전도연 HN 의 7월 9A draft 렌더 실패 케이스).

이 테스트는 그 SSOT 헬퍼를 직접 검증한다:
  ① home 아닌 '관리' 그룹의 스케줄도 group_id 없이 로드된다(버그 회귀 방지)
  ② 관리하지 않는 그룹의 스케줄은 403
  ③ dropped / 존재하지 않는 schedule_id 는 404
참조: app/routers/roster.py, app/services/group_access.py.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from db.models import Group, Nurse, Office, Schedule
from schemas.auth_schema import User as UserSchema
from routers.roster import _load_schedule_for_caller


def _user(*, group_id, is_head_nurse=True, hn_auth=None):
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id="o1", group_id=group_id,
        is_head_nurse=is_head_nurse, is_master_admin=False, name="수간",
        EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True,
        hn_auth=hn_auth, original_group_id=group_id, gw_useYN="Y", qpis_useYN="Y",
    )


def _add_schedule(db, schedule_id, group_id, *, dropped=False, status="draft"):
    db.add(Schedule(
        schedule_id=schedule_id, group_id=group_id, office_id="o1",
        year=2026, month=7, version=1, status=status, dropped=dropped,
        name="7월 근무표 VER1",
    ))
    db.flush()


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    # B = HN 이 hn_id 로 관리하는 비-home 그룹(= 9A 상황)
    db.add(Group(group_id="B", group_name="B병동", office_id="o1", hn_id=["HN"]))
    db.add(Group(group_id="OTHER", group_name="타병동", office_id="o1"))
    # 호출자: DB home = A, hn_auth=HN
    db.add(Nurse(nurse_id="HN", account_id="acc_HN", group_id="A", office_id="o1",
                 name="수간", active=1, is_head_nurse=True, hn_auth="HN",
                 allowed_shifts=[]))
    db.flush()
    return db


def test_loads_managed_non_home_schedule_without_group_id(seeded):
    """home(A) 아닌 관리그룹 B 의 스케줄도 group_id 전달 없이 로드된다(핵심 회귀)."""
    db = seeded
    _add_schedule(db, "sched_B", "B")
    user = _user(group_id="A", hn_auth="HN")  # 토큰/홈은 A

    schedule = _load_schedule_for_caller(db, user, "sched_B")

    assert schedule.schedule_id == "sched_B"
    assert schedule.group_id == "B"  # 그룹은 호출자 home(A)가 아니라 행의 값(B)


def test_rejects_unmanaged_group_schedule(seeded):
    """관리하지 않는 그룹(OTHER)의 스케줄은 403 — schedule_id 만으로 우회 불가(IDOR 방지)."""
    db = seeded
    _add_schedule(db, "sched_OTHER", "OTHER")
    user = _user(group_id="A", hn_auth="HN")

    with pytest.raises(HTTPException) as ei:
        _load_schedule_for_caller(db, user, "sched_OTHER")
    assert ei.value.status_code == 403


def test_dropped_schedule_is_404(seeded):
    """dropped 스케줄은 기본적으로 404(include_dropped=False)."""
    db = seeded
    _add_schedule(db, "sched_dropped", "B", dropped=True)
    user = _user(group_id="A", hn_auth="HN")

    with pytest.raises(HTTPException) as ei:
        _load_schedule_for_caller(db, user, "sched_dropped")
    assert ei.value.status_code == 404


def test_missing_schedule_is_404(seeded):
    """존재하지 않는 schedule_id 는 404."""
    db = seeded
    user = _user(group_id="A", hn_auth="HN")

    with pytest.raises(HTTPException) as ei:
        _load_schedule_for_caller(db, user, "nope")
    assert ei.value.status_code == 404
