"""fixed_shift 를 nurse_allowed_shift_period 로 통합 — carry-forward 검증.

allowed_shifts 와 fixed_shift 는 한 satellite 의 두 컬럼. 한쪽만 다른 valid_from 에서
바꾸면 새 구간을 열되 형제 컬럼은 직전 구간값을 carry-forward 해야 한다(allowed_shifts NOT NULL).
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import Office, Group, Nurse, NurseAllowedShiftPeriod
from services.nurse_period_resolver import upsert_period


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
                 name="n1", active=1, allowed_shifts=[], fixed_shift=None))
    db.flush()
    return db


def test_changing_fixed_carries_forward_allowed(seeded):
    db = seeded
    # 1) allowed 변경(7/1) → row1: allowed=["N"], fixed=None
    upsert_period(db, NurseAllowedShiftPeriod, "n1", date(2026, 7, 1),
                  "allowed_shifts", ["N"], nurse=db.query(Nurse).get("n1"),
                  cache_attr="allowed_shifts", carry_attrs=["fixed_shift"],
                  today=date(2026, 7, 1))
    # 2) fixed 변경(8/1, 미래 구간) → 새 구간, allowed 캐리포워드
    upsert_period(db, NurseAllowedShiftPeriod, "n1", date(2026, 8, 1),
                  "fixed_shift", "N_A", nurse=db.query(Nurse).get("n1"),
                  cache_attr="fixed_shift", carry_attrs=["allowed_shifts"],
                  today=date(2026, 8, 1))
    db.flush()

    rows = (db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="n1")
            .order_by(NurseAllowedShiftPeriod.valid_from).all())
    assert len(rows) == 2
    # 첫 구간 닫힘, fixed 없음
    assert rows[0].valid_from == date(2026, 7, 1) and rows[0].valid_to == date(2026, 8, 1)
    assert rows[0].allowed_shifts == ["N"] and rows[0].fixed_shift is None
    # 새 구간: fixed 신규 + allowed 캐리포워드 (NOT NULL 위반 없이)
    assert rows[1].valid_from == date(2026, 8, 1) and rows[1].valid_to is None
    assert rows[1].fixed_shift == "N_A" and rows[1].allowed_shifts == ["N"]


def test_same_day_two_attrs_share_one_row(seeded):
    db = seeded
    n = db.query(Nurse).get("n1")
    # 같은 날 allowed + fixed → 한 row 에 둘 다 (backfill 패턴)
    upsert_period(db, NurseAllowedShiftPeriod, "n1", date(2026, 7, 1),
                  "allowed_shifts", ["D"], nurse=n, cache_attr="allowed_shifts",
                  carry_attrs=["fixed_shift"], today=date(2026, 7, 1))
    upsert_period(db, NurseAllowedShiftPeriod, "n1", date(2026, 7, 1),
                  "fixed_shift", "D_A", nurse=n, cache_attr="fixed_shift",
                  carry_attrs=["allowed_shifts"], today=date(2026, 7, 1))
    db.flush()
    rows = db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="n1").all()
    assert len(rows) == 1
    assert rows[0].allowed_shifts == ["D"] and rows[0].fixed_shift == "D_A"
    # 캐시 투영
    assert n.fixed_shift == "D_A"
