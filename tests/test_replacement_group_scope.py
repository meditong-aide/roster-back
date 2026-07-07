"""긴급대체 추천(/roster/replacement/recommend)의 그룹 스코프 회귀.

증상: 다수 그룹 관리자(HN)가 home 아닌 관리병동의 근무표에서 '대체 근무자 찾기'를
누르면 "스케줄 없음"이 떴다. 원인은 _resolve_target_group 이 HN 을 무조건 home 으로
덮어써 Schedule.group_id == home 필터로 비-home 관리병동 스케줄을 떨군 것.

수정: 스코프는 스케줄 행의 group_id 가 진실(schedule_id 가 그룹 확정), 권한은
assert_caller_can_access_group 으로 별도 검증하는 SSOT 헬퍼 _load_schedule_for_caller
로 전환. 이 테스트는 그 헬퍼를 직접 검증한다(test_schedule_id_group_scope 와 동형).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from db.models import Group, Nurse, Office, Schedule
from schemas.auth_schema import User as UserSchema
from services.replacement_recommend_service import _load_schedule_for_caller


def _user(*, group_id, hn_auth="HN"):
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id="o1", group_id=group_id,
        is_head_nurse=True, is_master_admin=False, name="수간",
        EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True,
        hn_auth=hn_auth, original_group_id=group_id, gw_useYN="Y", qpis_useYN="Y",
    )


def _add_schedule(db, schedule_id, group_id, *, dropped=False):
    db.add(Schedule(
        schedule_id=schedule_id, group_id=group_id, office_id="o1",
        year=2026, month=7, version=1, status="draft", dropped=dropped,
        name="7월 근무표 VER1",
    ))
    db.flush()


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    # B = HN 이 hn_id 로 관리하는 비-home 그룹
    db.add(Group(group_id="B", group_name="B병동", office_id="o1", hn_id=["HN"]))
    db.add(Group(group_id="OTHER", group_name="타병동", office_id="o1"))
    # 호출자: DB home = A, hn_auth=HN
    db.add(Nurse(nurse_id="HN", account_id="acc_HN", group_id="A", office_id="o1",
                 name="수간", active=1, is_head_nurse=True, hn_auth="HN",
                 allowed_shifts=[]))
    db.flush()
    return db


def test_loads_managed_non_home_schedule(seeded):
    """home(A) 아닌 관리그룹 B 의 스케줄도 로드된다 — 핵심 회귀(스케줄 없음 버그)."""
    db = seeded
    _add_schedule(db, "sched_B", "B")
    user = _user(group_id="A")  # 토큰/홈은 A

    schedule = _load_schedule_for_caller(db, user, "sched_B")

    assert schedule.schedule_id == "sched_B"
    assert schedule.group_id == "B"  # 호출자 home(A)이 아니라 행의 값(B)


def test_rejects_unmanaged_group_schedule(seeded):
    """관리하지 않는 그룹(OTHER) 스케줄은 403 — schedule_id 만으로 우회 불가(IDOR 방지)."""
    db = seeded
    _add_schedule(db, "sched_OTHER", "OTHER")
    user = _user(group_id="A")

    with pytest.raises(HTTPException) as ei:
        _load_schedule_for_caller(db, user, "sched_OTHER")
    assert ei.value.status_code == 403


def test_dropped_schedule_not_found(seeded):
    """dropped 스케줄은 로드 대상이 아니다(ValueError → 라우터 400)."""
    db = seeded
    _add_schedule(db, "sched_dropped", "B", dropped=True)
    user = _user(group_id="A")

    with pytest.raises(ValueError, match="schedule not found"):
        _load_schedule_for_caller(db, user, "sched_dropped")


def test_missing_schedule_not_found(seeded):
    """존재하지 않는 schedule_id 는 ValueError(→ 라우터 400)."""
    db = seeded
    user = _user(group_id="A")

    with pytest.raises(ValueError, match="schedule not found"):
        _load_schedule_for_caller(db, user, "nope")
