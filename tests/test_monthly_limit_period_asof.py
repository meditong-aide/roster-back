"""P4 회귀: 월한도 검증의 effective nurse 가 allowed/fixed 를 period as-of 로 읽는다.

버그: _effective_nurse_for_group 이 home 간호사를 raw 캐시로 폴백 → 구 resolver(assignment
.target_*→캐시)만 봄. 생성기는 period as-of 라 미래발효 변경에서 검증과 생성이 어긋났다.
수정: home 도 NurseAllowedShiftPeriod as-of 오버레이(gap 이면 캐시 폴백 — 무회귀).
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import Office, Group, Nurse, NurseAllowedShiftPeriod
from services.nurse_monthly_limit_service import _effective_nurse_for_group


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, is_weekend_off=False, allowed_shifts=["D", "E", "N"], grade=1))
    # 8월부터 야간만(["N"])로 발효되는 미래 변경
    db.add(NurseAllowedShiftPeriod(
        nurse_id="n1", valid_from=date(2026, 8, 1), valid_to=None,
        allowed_shifts=["N"], fixed_shift=None, source="test",
    ))
    db.flush()
    return db


def test_home_nurse_allowed_resolved_as_of_target_month(seeded):
    db = seeded
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "n1").first()

    # 7월: period 구간 전 → 캐시 폴백
    eff_jul = _effective_nurse_for_group(db, nurse, "A", 2026, 7)
    assert list(eff_jul.allowed_shifts) == ["D", "E", "N"]

    # 8월: period as-of → 미래발효 ["N"] 반영(캐시 아님)
    eff_aug = _effective_nurse_for_group(db, nurse, "A", 2026, 8)
    assert list(eff_aug.allowed_shifts) == ["N"]

    # 원본 세션 객체는 불변(읽기전용 뷰)
    assert list(nurse.allowed_shifts) == ["D", "E", "N"]
