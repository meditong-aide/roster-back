"""근무자관리 월 셀렉터 — 속성 as-of 표시.

group_members_in_month 가 grade·전담(allowed_shifts)을 선택 월(month_start) 기준 period
as-of 로 보여주는지. gap→캐시 폴백(무회귀).
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import (
    Office, Group, Nurse, NurseGradePeriod, NurseAllowedShiftPeriod,
    NurseWeekendOffPeriod,
)
from services.assignment_service import group_members_in_month
from services.nurse_service import attach_member_badges_to_nurses


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="a1", group_id="A", office_id="o1",
                 name="n1", active=1, grade=2, allowed_shifts=[]))
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


def test_weekend_and_fixed_show_asof_month(seeded):
    db = seeded
    # 8월부터 주말휴무 + 고정근무 D
    db.add(NurseWeekendOffPeriod(nurse_id="n1", valid_from=date(2026, 8, 1),
           valid_to=None, weekend_off=1))
    db.add(NurseAllowedShiftPeriod(nurse_id="n1", valid_from=date(2026, 8, 1),
           valid_to=None, allowed_shifts=[], fixed_shift="D"))
    db.flush()
    jul = _m(group_members_in_month(db, "A", 2026, 7), "n1")
    aug = _m(group_members_in_month(db, "A", 2026, 8), "n1")
    assert jul["as_of_weekend_off"] == 0 and jul["as_of_fixed_shift"] is None  # gap→캐시
    assert aug["as_of_weekend_off"] == 1 and aug["as_of_fixed_shift"] == "D"


def test_nurses_base_fields_overwritten_by_asof(seeded):
    db = seeded  # 캐시: grade=2, is_weekend_off=False, fixed_shift=None, allowed_shifts=[]
    db.add(NurseGradePeriod(nurse_id="n1", group_id="A",
           valid_from=date(2026, 8, 1), valid_to=None, grade=5))
    db.add(NurseWeekendOffPeriod(nurse_id="n1", valid_from=date(2026, 8, 1),
           valid_to=None, weekend_off=1))
    db.add(NurseAllowedShiftPeriod(nurse_id="n1", valid_from=date(2026, 8, 1),
           valid_to=None, allowed_shifts=["N"], fixed_shift="N"))
    db.flush()
    # /nurses base dict (캐시값) → 월 병합 시 as-of 로 덮어써야 함
    rows = [{"nurse_id": "n1", "grade": 2, "is_weekend_off": False,
             "fixed_shift": None, "allowed_shifts": [], "team_id": 9}]
    attach_member_badges_to_nurses(db, rows, "A", 2026, 8)
    r = rows[0]
    assert r["grade"] == 5              # 캐시 2 → as-of 5
    assert r["is_weekend_off"] is True  # False → 1
    assert r["fixed_shift"] == "N"      # None → N
    assert r["allowed_shifts"] == ["N"]  # [] → ["N"]


def test_exclusion_partner_name_resolved_office_wide(seeded):
    """상호배제 파트너 이름은 현재 목록에 없어도(타그룹) office-wide 로 해석돼야 한다."""
    from services.mutual_exclusion_period import set_mutual_exclusion
    db = seeded  # n1 ∈ A병동
    # 파트너 n2 는 다른 그룹 B병동 소속 → A병동 월 멤버 목록엔 없음
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add(Nurse(nurse_id="n2", account_id="a2", group_id="B", office_id="o1",
                 name="이영희", active=1, grade=2, allowed_shifts=[]))
    db.flush()
    set_mutual_exclusion(db, nurse_id="n1", partner_id="n2", office_id="o1",
                         valid_from=date(2026, 8, 1))
    db.flush()
    rows = [{"nurse_id": "n1", "grade": 2, "allowed_shifts": [], "team_id": 9}]
    attach_member_badges_to_nurses(db, rows, "A", 2026, 8)
    r = rows[0]
    assert r["exclusion_partner_id"] == "n2"
    assert r["exclusion_partner_name"] == "이영희"  # 타그룹이라 목록에 없어도 이름 해석됨


def test_exclusion_partner_name_none_when_no_relation(seeded):
    """관계 없으면 id·name 모두 None."""
    db = seeded
    rows = [{"nurse_id": "n1", "grade": 2, "allowed_shifts": [], "team_id": 9}]
    attach_member_badges_to_nurses(db, rows, "A", 2026, 8)
    r = rows[0]
    assert r["exclusion_partner_id"] is None
    assert r["exclusion_partner_name"] is None
