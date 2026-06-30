"""P5 회귀: 대체 추천 후보 capability 를 대상 월 as-of period 로 오버레이.

버그: _nurse_is_night_capable 등이 nurse.allowed_shifts(캐시=오늘값)를 읽어 날짜기준 아님.
수정: 후보 로드 직후 _overlay_candidate_capability_asof 로 그 달 period 값 주입(gap=캐시).
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import Office, Group, Nurse, NurseAllowedShiftPeriod
from services.replacement_recommend_service import _overlay_candidate_capability_asof


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    # 캐시는 야간가능(["N"]), 8월부터 주간전담(["D"])로 발효 → 8월엔 야간불가여야
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, allowed_shifts=["N"], grade=1))
    db.add(NurseAllowedShiftPeriod(
        nurse_id="n1", valid_from=date(2026, 8, 1), valid_to=None,
        allowed_shifts=["D"], fixed_shift=None, source="test",
    ))
    db.flush()
    return db


def test_candidate_night_capability_as_of_month(seeded):
    db = seeded
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "n1").first()

    # 8월 as-of → 캐시(["N"])가 아니라 period 발효값(["D"])으로 오버레이
    _overlay_candidate_capability_asof(db, [nurse], date(2026, 8, 31))
    assert list(nurse.allowed_shifts) == ["D"]


def test_gap_keeps_cache(seeded):
    db = seeded
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "n1").first()
    # 7월: 구간 전 → 캐시 유지(["N"])
    _overlay_candidate_capability_asof(db, [nurse], date(2026, 7, 31))
    assert list(nurse.allowed_shifts) == ["N"]
