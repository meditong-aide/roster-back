"""grade 쓰기 경로를 nurse_grade_period 로 일원화 (P7, grade).

agent/업데이트 경로가 컬럼 직접쓰기 대신 period 에 쓰고 nurses.grade 는 단방향 투영만
받는지 검증. is_night_nurse(allowed_shifts) 선례와 동일 패턴.
참조: app/agents_v2/tools/nurse_tools.py, app/services/nurse_service.py.
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import Office, Group, Nurse, NurseGradePeriod
from agents_v2.tools.nurse_tools import update_nurse_attribute


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
                 name="n1", active=1, grade=2, is_night_nurse=[]))
    db.flush()
    return db


def test_agent_grade_update_writes_period_and_projects_cache(seeded):
    db = seeded
    res = update_nurse_attribute(db, "n1", "A", "grade", 3)
    assert isinstance(res, dict)

    # period 에 기록됨 (진실) — 병동귀속이라 group_id 포함
    rows = db.query(NurseGradePeriod).filter_by(nurse_id="n1").all()
    assert len(rows) == 1
    assert rows[0].grade == 3
    assert rows[0].group_id == "A"
    assert rows[0].valid_from == date.today() and rows[0].valid_to is None

    # nurses.grade 는 단방향 투영으로 동기화됨 (오늘 발효)
    assert db.query(Nurse).filter_by(nurse_id="n1").first().grade == 3


def test_agent_grade_change_is_close_before_open(seeded):
    db = seeded
    update_nurse_attribute(db, "n1", "A", "grade", 3)
    update_nurse_attribute(db, "n1", "A", "grade", 1)

    rows = (
        db.query(NurseGradePeriod).filter_by(nurse_id="n1")
        .order_by(NurseGradePeriod.valid_from).all()
    )
    # 같은 날(오늘) 두 번 변경 → 제자리 갱신(구간 1개, 마지막 값)
    assert len(rows) == 1
    assert rows[0].grade == 1
    assert db.query(Nurse).filter_by(nurse_id="n1").first().grade == 1
