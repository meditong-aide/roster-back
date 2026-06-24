"""근무자관리 월 셀렉터 — 속성 as-of 표시.

group_members_in_month 가 grade·전담(allowed_shifts)을 선택 월(month_start) 기준 period
as-of 로 보여주는지. gap→캐시 폴백(무회귀).
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import (
    Office, Group, Nurse, NurseGradePeriod, NurseAllowedShiftPeriod,
)
from services.assignment_service import group_members_in_month


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="a1", group_id="A", office_id="o1",
                 name="n1", active=1, grade=2, is_night_nurse=[]))
    db.flush()
    return db


def _m(res, nid):
    return next(x for x in res["members"] if x["nurse_id"] == nid)


def test_grade_shows_asof_month(seeded):
    db = seeded
    # 7월까지 grade=2, 8월부터 grade=4 (close-before-open)
    db.add(NurseGradePeriod(nurse_id="n1", group_id="A",
           valid_from=date(2026, 7, 1), valid_to=date(2026, 8, 1), grade=2))
    db.add(NurseGradePeriod(nurse_id="n1", group_id="A",
           valid_from=date(2026, 8, 1), valid_to=None, grade=4))
    db.flush()
    assert _m(group_members_in_month(db, "A", 2026, 7), "n1")["as_of_grade"] == 2
    assert _m(group_members_in_month(db, "A", 2026, 8), "n1")["as_of_grade"] == 4


def test_grade_gap_falls_back_to_cache(seeded):
    db = seeded  # period 없음 → 캐시 grade=2
    assert _m(group_members_in_month(db, "A", 2026, 8), "n1")["as_of_grade"] == 2


def test_night_dedicated_shows_asof_month(seeded):
    db = seeded
    # 8월부터 N전담(allowed=["N"]) — 7월은 gap→캐시([])
    db.add(NurseAllowedShiftPeriod(nurse_id="n1",
           valid_from=date(2026, 8, 1), valid_to=None, allowed_shifts=["N"]))
    db.flush()
    jul = _m(group_members_in_month(db, "A", 2026, 7), "n1")
    aug = _m(group_members_in_month(db, "A", 2026, 8), "n1")
    assert jul["is_night_dedicated"] is False and jul["badge"] != "N전담"
    assert aug["is_night_dedicated"] is True and aug["badge"] == "N전담"
