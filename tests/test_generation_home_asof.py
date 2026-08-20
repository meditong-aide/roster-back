"""생성기 home 속성 as-of 오버레이.

grade/weekend_off/fixed_shift 를 대상월 period 값으로 생성(캐시 오늘값이 아니라).
미래월을 미리 생성해도 미래발효 변경이 반영되고, gap 이면 캐시 유지(비회귀).
참조: app/services/roster_create_service.py::_overlay_home_profile_asof
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import (
    Office, Group, Nurse, NurseGradePeriod, NurseWeekendOffPeriod, NurseAllowedShiftPeriod,
)
from services.roster_create_service import _overlay_home_profile_asof
from services.nurse_period_resolver import upsert_period


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="a1", group_id="A", office_id="o1", name="n1",
                 active=1, grade=1, allowed_shifts=[], fixed_shift=None))
    db.flush()
    return db


def test_overlay_applies_target_month_period(seeded):
    """8월부터 grade=3/weekend=1/fixed=D → 8월 생성 시 캐시(1/False/None) 대신 period 값."""
    db = seeded
    upsert_period(db, NurseGradePeriod, "n1", date(2026, 8, 1), "grade", 3, group_id="A")
    upsert_period(db, NurseWeekendOffPeriod, "n1", date(2026, 8, 1), "weekend_off", 1)
    upsert_period(db, NurseAllowedShiftPeriod, "n1", date(2026, 8, 1), "fixed_shift", "D",
                  carry_attrs=["allowed_shifts"])
    db.flush()
    n = db.query(Nurse).filter_by(nurse_id="n1").first()
    assert (n.grade, n.fixed_shift) == (1, None)  # 캐시(주말휴무는 컬럼 언매핑 → overlay가 채움)
    _overlay_home_profile_asof(db, [n], "A", date(2026, 8, 1))
    assert n.grade == 3
    assert bool(n.is_weekend_off) is True
    assert n.fixed_shift == "D"


def test_overlay_gap_keeps_cache(seeded):
    """대상월이 구간 이전(gap)이면 캐시 유지 — 미래발효 변경이 그 이전 월엔 미적용."""
    db = seeded
    upsert_period(db, NurseGradePeriod, "n1", date(2026, 8, 1), "grade", 3, group_id="A")
    db.flush()
    n = db.query(Nurse).filter_by(nurse_id="n1").first()
    _overlay_home_profile_asof(db, [n], "A", date(2026, 7, 1))   # 7월 < 8월 구간
    assert n.grade == 1   # 캐시 유지(미적용)


def test_overlay_grade_group_bound(seeded):
    """grade 는 group-bound — 다른 그룹 구간은 안 잡힘(캐시 유지)."""
    db = seeded
    upsert_period(db, NurseGradePeriod, "n1", date(2026, 8, 1), "grade", 3, group_id="B")  # 타 그룹
    db.flush()
    n = db.query(Nurse).filter_by(nurse_id="n1").first()
    _overlay_home_profile_asof(db, [n], "A", date(2026, 8, 1))   # 조회 그룹 A
    assert n.grade == 1   # B 구간은 A 조회에 안 잡힘 → 캐시
