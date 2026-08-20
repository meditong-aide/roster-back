"""근무표 생성 시 allowed_shifts 대상월 as-of 오버레이 (regression).

버그: 6월 N전담(['N']) → 8월 해제([])를 미래발효로 저장하면 nurses.allowed_shifts 컬럼은
as-of-TODAY(현재 7월) 캐시라 ['N'] 그대로 stale. 8월 생성 시 day-grain 하드제약은 period
(=[], 제한없음)로 정확하지만 is_n_only_profile 등 엔진 로직이 컬럼(['N'])을 읽어 8월인데
야간전담으로 오판 → D/E 실종.

수정: generate_roster_service 가 엔진 투입 전 대상월 as-of period 값으로 컬럼을 오버레이한다.
이 테스트는 그 오버레이가 의존하는 메커니즘(fetch_periods + resolve_asof → is_n_only_profile)이
8월엔 [](제한없음), 7월엔 ['N'](야간전담)을 내는지 확정한다.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from db.models import Office, Group, Nurse, NurseAllowedShiftPeriod
from services.nurse_period_resolver import fetch_periods, resolve_asof
from services.cp_sat.allowed_shift_types import is_n_only_profile


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    # 컬럼 캐시는 현재(7월) 값 = N전담 ['N'] (미래 8월 해제는 캐시투영 스킵되어 stale)
    db.add(Nurse(nurse_id="177659", account_id="a1", group_id="A", office_id="o1",
                 name="김수선", active=1, grade=2, allowed_shifts=["N"]))
    # 6월 N전담(close-before-open 으로 8/1 에 닫힘) + 8월 해제([])
    db.add(NurseAllowedShiftPeriod(nurse_id="177659",
           valid_from=date(2026, 6, 1), valid_to=date(2026, 8, 1), allowed_shifts=["N"]))
    db.add(NurseAllowedShiftPeriod(nurse_id="177659",
           valid_from=date(2026, 8, 1), valid_to=None, allowed_shifts=[]))
    db.flush()
    return db


def _asof_column(db, nid, year, month, col_default):
    """generate_roster_service 오버레이와 동일 계산: 대상월 as-of allowed_shifts."""
    ms = date(year, month, 1)
    rows = fetch_periods(db, NurseAllowedShiftPeriod, [nid], ms, ms + timedelta(days=1))
    return resolve_asof(rows.get(nid), ms, "allowed_shifts", default=col_default)


def test_stale_column_is_night_only_but_august_asof_is_unrestricted(seeded):
    db = seeded
    stale_col = ["N"]
    # 버그 재현: 컬럼만 보면 8월에도 야간전담으로 오판
    assert is_n_only_profile(stale_col) is True
    # 수정: 8월 as-of 오버레이 → [](제한없음) → 야간전담 아님 → D/E 가능
    aug = _asof_column(db, "177659", 2026, 8, stale_col)
    assert list(aug) == []
    assert is_n_only_profile(aug) is False


def test_july_asof_still_night_only(seeded):
    db = seeded
    # 7월은 6월 구간이 유효 → 여전히 N전담(무회귀: 과거월 오판 없음)
    jul = _asof_column(db, "177659", 2026, 7, ["N"])
    assert set(jul) == {"N"}
    assert is_n_only_profile(jul) is True


def test_unmigrated_nurse_keeps_column(db):
    # period row 없음(미이행) → 컬럼 유지(무회귀)
    db.add(Office(office_id="o2", office_name="병원2"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o2"))
    db.add(Nurse(nurse_id="n2", account_id="a2", group_id="B", office_id="o2",
                 name="n2", active=1, grade=2, allowed_shifts=["N"]))
    db.flush()
    aug = _asof_column(db, "n2", 2026, 8, ["N"])
    assert set(aug) == {"N"}  # default(컬럼) 유지
