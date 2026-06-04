"""병동 내 영구 속성변경(team/grade) 이벤트 — 생성·발효·overlap 제외 검증.

속성 이벤트(permanent_change)는 존재 이벤트(파견/병동이동)와 달리 source==target 이고
기간 겹침 검사 대상이 아니다. 발효(flush)는 Nurse.team_id/grade 를 직접 갱신한다.
참조: docs/NURSE_GROUP_CHANGE_MODEL.md (옵션1 병동 내 팀 분류).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from db.models import Group, Nurse, NurseAssignment, Office
from services.assignment_service import (
    create_permanent_change,
    flush_pending_permanent_changes,
    _raise_if_overlap,
)


@pytest.fixture
def seed(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(
        nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
        name="김간호", active=1, team_id=1, grade=2,
    ))
    db.flush()
    return db


def test_create_permanent_change_row(seed):
    db = seed
    row = create_permanent_change(
        db, nurse_id="n1", group_id="A", office_id="o1",
        start_date=date(2026, 8, 1), new_team_id=3, new_grade=1,
    )
    assert row.kind == "permanent_change"
    assert row.source_group_id == row.target_group_id == "A"  # 병동 내
    assert row.status == "active"
    assert row.target_team_id == 3 and row.target_grade == 1
    assert row.payload == {"prev_team_id": 1, "prev_grade": 2}  # 되돌리기용 직전값


def test_create_requires_some_attr(seed):
    with pytest.raises(HTTPException):
        create_permanent_change(
            seed, nurse_id="n1", group_id="A", office_id="o1",
            start_date=date(2026, 8, 1),
        )


def test_flush_before_effective_date_noop(seed):
    db = seed
    create_permanent_change(
        db, nurse_id="n1", group_id="A", office_id="o1",
        start_date=date(2026, 8, 1), new_team_id=3,
    )
    n = flush_pending_permanent_changes(db, as_of=date(2026, 7, 31))
    assert n == 0
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "n1").first()
    assert nurse.team_id == 1  # 미발효 — 현재값 유지


def test_flush_on_effective_date_applies(seed):
    db = seed
    create_permanent_change(
        db, nurse_id="n1", group_id="A", office_id="o1",
        start_date=date(2026, 8, 1), new_team_id=3, new_grade=1,
    )
    n = flush_pending_permanent_changes(db, as_of=date(2026, 8, 1))
    assert n == 1
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "n1").first()
    assert nurse.team_id == 3 and nurse.grade == 1
    row = db.query(NurseAssignment).filter(NurseAssignment.nurse_id == "n1").first()
    assert row.status == "completed" and row.end_date == date(2026, 8, 1)


def test_flush_none_attr_not_overwritten(seed):
    db = seed
    create_permanent_change(
        db, nurse_id="n1", group_id="A", office_id="o1",
        start_date=date(2026, 8, 1), new_team_id=3,  # grade 미지정
    )
    flush_pending_permanent_changes(db, as_of=date(2026, 8, 1))
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "n1").first()
    assert nurse.team_id == 3
    assert nurse.grade == 2  # grade 는 target None → 미변경


def test_permanent_change_does_not_block_presence_event(seed):
    """active 속성변경이 있어도 파견/이동 같은 존재 이벤트 생성을 막지 않아야 함."""
    db = seed
    create_permanent_change(
        db, nurse_id="n1", group_id="A", office_id="o1",
        start_date=date(2026, 8, 1), new_team_id=3,
    )
    # overlap 검사가 속성 이벤트를 무시하므로 예외 없이 통과해야 함
    _raise_if_overlap(db, "n1", date(2026, 8, 5), date(2026, 8, 20))
