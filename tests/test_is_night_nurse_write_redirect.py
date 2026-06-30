# tests/test_allowed_shifts_write_redirect.py
"""1순위 — allowed_shifts 옛 쓰기 경로를 nurse_allowed_shift_period 로 일원화.

옛 수정 경로(agent update)가 컬럼 직접쓰기 대신 period 에 쓰고 컬럼은 투영만 받는지 검증.
참조: app/agents_v2/tools/nurse_tools.py, app/services/nurse_service.py.
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import Office, Group, Nurse, NurseAllowedShiftPeriod
from agents_v2.tools.nurse_tools import update_nurse_attribute


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
                 name="n1", active=1, allowed_shifts=[]))
    db.flush()
    return db


def test_agent_update_writes_period_and_projects_cache(seeded):
    db = seeded
    res = update_nurse_attribute(db, "n1", "A", "allowed_shifts", ["N"])
    assert isinstance(res, dict)

    # period 에 기록됨 (진실)
    rows = db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="n1").all()
    assert len(rows) == 1
    assert rows[0].allowed_shifts == ["N"]
    assert rows[0].valid_from == date.today() and rows[0].valid_to is None

    # 컬럼은 단방향 투영으로 동기화됨 (오늘 발효)
    assert db.query(Nurse).filter_by(nurse_id="n1").first().allowed_shifts == ["N"]


def test_agent_update_change_is_close_before_open(seeded):
    db = seeded
    update_nurse_attribute(db, "n1", "A", "allowed_shifts", ["N"])
    update_nurse_attribute(db, "n1", "A", "allowed_shifts", ["D", "E"])

    rows = db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="n1") \
             .order_by(NurseAllowedShiftPeriod.valid_from).all()
    # 같은 날(오늘) 두 번 변경 → 제자리 갱신(구간 1개, 마지막 값)
    assert len(rows) == 1
    assert rows[0].allowed_shifts == ["D", "E"]
    assert db.query(Nurse).filter_by(nurse_id="n1").first().allowed_shifts == ["D", "E"]
