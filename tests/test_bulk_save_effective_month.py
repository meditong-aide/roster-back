"""bulk 저장이 선택월(year/month)을 period valid_from 으로 — 월 셀렉터 발효 정합.

버그: 6월 ON → 8월 OFF 시 valid_from=today 라 6월 구간을 제자리 덮어써 6/7월 유실.
수정: effective_year/month → valid_from=date(y,m,1), close-before-open 으로 6/7월 유지.
참조: app/services/nurse_service.py::bulk_update_nurses_service
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from db.models import Office, Group, Nurse, NurseWeekendOffPeriod, NurseGradePeriod
from schemas.roster_schema import NurseProfile
from schemas.auth_schema import User as UserSchema
from services.nurse_service import bulk_update_nurses_service
from services.nurse_period_resolver import fetch_periods, resolve_asof


def _hdn(group_id="A"):
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id="o1", group_id=group_id,
        is_head_nurse=True, is_master_admin=False, name="수간", EmpSeqNo="",
        EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True, hn_auth="HN",
        original_group_id=group_id, gw_useYN="Y", qpis_useYN="Y",
    )


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, allowed_shifts=[], grade=1))
    db.flush()
    return db


def _prof(**kw):
    return NurseProfile(office_id="o1", nurse_id="n1", group_id="A", account_id="acc_n1",
                        name="n1", **kw)


def _wk(db, day):
    rows = fetch_periods(db, NurseWeekendOffPeriod, ["n1"], day, day + timedelta(days=1))
    return resolve_asof(rows.get("n1"), day, "weekend_off")


def _gr(db, day):
    rows = fetch_periods(db, NurseGradePeriod, ["n1"], day, day + timedelta(days=1), group_id="A")
    return resolve_asof(rows.get("n1"), day, "grade")


def test_weekend_selected_month_close_before_open(seeded):
    """6월 ON → 8월 OFF: 6/7월 유지, 8월부터 OFF (제자리 덮어쓰기 버그 수정)."""
    db = seeded
    u = _hdn()
    bulk_update_nurses_service([_prof(is_weekend_off=True)], u, db, override_group_id="A",
                               effective_year=2026, effective_month=6)
    assert _wk(db, date(2026, 6, 15)) == 1

    bulk_update_nurses_service([_prof(is_weekend_off=False)], u, db, override_group_id="A",
                               effective_year=2026, effective_month=8)
    assert _wk(db, date(2026, 6, 15)) == 1   # ★ 6월 유지
    assert _wk(db, date(2026, 7, 15)) == 1   # ★ 7월 유지
    assert _wk(db, date(2026, 8, 15)) == 0   # 8월부터 OFF


def test_grade_selected_month_close_before_open(seeded):
    """grade 도 동일 — 6월 grade=2 → 8월 grade=3: 6/7월=2 유지."""
    db = seeded
    u = _hdn()
    bulk_update_nurses_service([_prof(grade=2)], u, db, override_group_id="A",
                               effective_year=2026, effective_month=6)
    bulk_update_nurses_service([_prof(grade=3)], u, db, override_group_id="A",
                               effective_year=2026, effective_month=8)
    assert _gr(db, date(2026, 7, 15)) == 2   # 7월 유지
    assert _gr(db, date(2026, 8, 15)) == 3   # 8월부터 3


def test_no_month_falls_back_today(seeded):
    """year/month 미동반 → valid_from=today (현재값 변경, 기존 동작 보존)."""
    db = seeded
    u = _hdn()
    bulk_update_nurses_service([_prof(is_weekend_off=True)], u, db, override_group_id="A")
    today = date.today()
    assert _wk(db, today) == 1   # 오늘 기준 적용
