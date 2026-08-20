"""weekend_off·fixed_shift 쓰기 경로 period 일원화 (P7).

- fixed_shift → nurse_allowed_shift_period(통합 satellite). 기존 allowed 구간 없으면 []로 채움.
- is_weekend_off → nurse_weekendoff_period.
컬럼은 단방향 투영. allowed_shifts·grade 선례와 동일.
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import (
    Office, Group, Nurse, NurseAllowedShiftPeriod, NurseWeekendOffPeriod,
)
from agents_v2.tools.nurse_tools import update_nurse_attribute


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
                 name="n1", active=1, allowed_shifts=[], fixed_shift=None,
                 ))
    db.flush()
    return db


def test_fixed_shift_redirects_to_allowed_satellite(seeded):
    db = seeded
    # 기존 allowed 구간 없는 상태에서 fixed_shift 설정 → 새 row, allowed=[] 기본
    update_nurse_attribute(db, "n1", "A", "fixed_shift", "D")

    rows = db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="n1").all()
    assert len(rows) == 1
    assert rows[0].fixed_shift == "D"
    assert rows[0].allowed_shifts == []          # default=list (NOT NULL 안전)
    assert rows[0].valid_from == date.today() and rows[0].valid_to is None
    assert db.query(Nurse).filter_by(nurse_id="n1").first().fixed_shift == "D"


def test_weekend_off_redirects_to_period(seeded):
    db = seeded
    update_nurse_attribute(db, "n1", "A", "is_weekend_off", True)

    rows = db.query(NurseWeekendOffPeriod).filter_by(nurse_id="n1").all()
    assert len(rows) == 1
    assert rows[0].weekend_off == 1
    assert rows[0].valid_from == date.today() and rows[0].valid_to is None
    # 컬럼 캐시 제거 — period(as-of today) 로 검증.
    from services.nurse_period_resolver import is_weekend_off_asof
    assert is_weekend_off_asof(db, "n1") is True
