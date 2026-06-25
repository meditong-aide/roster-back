"""사이드프로필(PATCH /nurses/{id} = update_nurse_profile_service) 도 선택월 발효 + weekend period.

버그: 사이드패널 저장이 bulk 와 달리 valid_from=현재월 기본 + weekend 가 period 아닌 캐시.
수정: effective_year/month → valid_from, is_weekend_off → nurse_weekendoff_period.
참조: app/services/nurse_service.py::update_nurse_profile_service / _persist_profile_period_change
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from db.models import Office, Group, Nurse, NurseWeekendOffPeriod, NurseGradePeriod
from schemas.roster_schema import NurseProfileUpdate
from schemas.auth_schema import User as UserSchema
from services.nurse_service import update_nurse_profile_service
from services.nurse_period_resolver import fetch_periods, resolve_asof


def _admin():
    return UserSchema(
        nurse_id="ADM", account_id="acc_ADM", office_id="o1", group_id="A",
        is_head_nurse=False, is_master_admin=True, name="관리자", EmpSeqNo="",
        EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True, hn_auth=None,
        original_group_id="A", gw_useYN="Y", qpis_useYN="Y",
    )


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, is_weekend_off=False, allowed_shifts=[], grade=1))
    db.flush()
    return db


def _wk(db, day):
    rows = fetch_periods(db, NurseWeekendOffPeriod, ["n1"], day, day + timedelta(days=1))
    return resolve_asof(rows.get("n1"), day, "weekend_off")


def _gr(db, day):
    rows = fetch_periods(db, NurseGradePeriod, ["n1"], day, day + timedelta(days=1), group_id="A")
    return resolve_asof(rows.get("n1"), day, "grade")


def test_sideprofile_weekend_selected_month_close_before_open(seeded):
    """사이드프로필 6월 ON → 8월 OFF: 6/7월 유지, 8월부터 OFF (weekend 가 period 로)."""
    db = seeded
    u = _admin()
    update_nurse_profile_service("n1", NurseProfileUpdate(is_weekend_off=True), u, db,
                                 effective_year=2026, effective_month=6)
    assert _wk(db, date(2026, 6, 15)) == 1

    update_nurse_profile_service("n1", NurseProfileUpdate(is_weekend_off=False), u, db,
                                 effective_year=2026, effective_month=8)
    assert _wk(db, date(2026, 6, 15)) == 1   # ★ 6월 유지
    assert _wk(db, date(2026, 7, 15)) == 1   # ★ 7월 유지
    assert _wk(db, date(2026, 8, 15)) == 0   # 8월부터 OFF


def test_sideprofile_grade_selected_month(seeded):
    """grade 도 사이드프로필에서 선택월 발효."""
    db = seeded
    u = _admin()
    update_nurse_profile_service("n1", NurseProfileUpdate(grade=2), u, db,
                                 effective_year=2026, effective_month=6)
    update_nurse_profile_service("n1", NurseProfileUpdate(grade=3), u, db,
                                 effective_year=2026, effective_month=8)
    assert _gr(db, date(2026, 7, 15)) == 2   # 7월 유지
    assert _gr(db, date(2026, 8, 15)) == 3   # 8월부터 3
