"""월 근무한도 저장 검증이 allowed_shifts 를 대상월 as-of(period)로 보는지 (regression).

버그: 6월 N전담(['N'])→8월 해제([])를 미래발효로 저장하면 nurses.allowed_shifts 컬럼은
as-of-today('N') stale. 저장 서비스가 home 간호사를 raw nurse(컬럼)로 검증해 8월인데
'N전담' 오판 → MONTHLY_LIMIT_NIGHT_DEDICATED_* / NOT_IN_WORK_SHIFTS 오차단.

수정: 저장 서비스가 home 도 _effective_nurse_for_group(대상월 as-of period)로 검증.
이 테스트는 그 effective 뷰가 8월엔 [](제한없음), 7월엔 ['N']을 내는지 확정한다.
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import Office, Group, Nurse, NurseAllowedShiftPeriod
from services.nurse_monthly_limit_service import _effective_nurse_for_group
from services.precheck.monthly_limit_validator import _is_night_dedicated, _allowed_work_shifts


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    # 컬럼 캐시 = 현재값 N전담(['N']) — 미래 8월 해제는 캐시투영 스킵되어 stale
    db.add(Nurse(nurse_id="177659", account_id="a1", group_id="A", office_id="o1",
                 name="김수선", active=1, grade=2, allowed_shifts=["N"]))
    db.add(NurseAllowedShiftPeriod(nurse_id="177659",
           valid_from=date(2026, 6, 1), valid_to=date(2026, 8, 1), allowed_shifts=["N"]))
    db.add(NurseAllowedShiftPeriod(nurse_id="177659",
           valid_from=date(2026, 8, 1), valid_to=None, allowed_shifts=[]))
    db.flush()
    return db


def test_august_effective_is_not_night_dedicated(seeded):
    db = seeded
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "177659").first()
    # 컬럼만 보면 N전담 오판(버그 재현)
    assert _is_night_dedicated(nurse) is True
    # 저장 서비스가 쓰는 effective 뷰(대상월 as-of) → 8월은 [](제한없음) → N전담 아님
    eff_aug = _effective_nurse_for_group(db, nurse, "A", 2026, 8)
    assert _allowed_work_shifts(eff_aug) is None  # [] = 제한없음
    assert _is_night_dedicated(eff_aug) is False


def test_july_effective_still_night_dedicated(seeded):
    db = seeded
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "177659").first()
    # 7월은 6월 구간 유효 → 여전히 N전담(무회귀)
    eff_jul = _effective_nurse_for_group(db, nurse, "A", 2026, 7)
    assert _allowed_work_shifts(eff_jul) == {"N"}
    assert _is_night_dedicated(eff_jul) is True


def test_unmigrated_nurse_falls_back_to_column(db):
    # period 없음 → effective = 원본 nurse(캐시) → 무회귀
    db.add(Office(office_id="o2", office_name="병원2"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o2"))
    db.add(Nurse(nurse_id="n2", account_id="a2", group_id="B", office_id="o2",
                 name="n2", active=1, grade=2, allowed_shifts=["N"]))
    db.flush()
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "n2").first()
    eff = _effective_nurse_for_group(db, nurse, "B", 2026, 8)
    assert _is_night_dedicated(eff) is True  # 캐시 폴백
