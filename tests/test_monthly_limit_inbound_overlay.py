"""파견 인바운드 간호사 monthly-limit capability 오버레이 회귀 테스트.

버그: monthly-limit preflight 가 base `nurses.is_night_nurse`(예 ["D"])만 보고
파견 대상 그룹의 `nurse_assignment.target_shift_types`(예 ["D","N"])를 무시 →
파견지에서 가능해진 N 의 min/exact 설정이 MONTHLY_LIMIT_NOT_IN_WORK_SHIFTS 로 오차단.
수정: inbound 행은 effective nurse(오버레이 적용)로 검증.
"""
from __future__ import annotations

from datetime import date

from db.models import Nurse, NurseAssignment
from services.nurse_monthly_limit_service import (
    _EffectiveNurseView,
    _effective_nurse_for_group,
)
from services.precheck.monthly_limit_validator import validate_monthly_limit_row

HOME = "home1"
TARGET = "tgt1"


def _nurse(db, is_night):
    n = Nurse(
        nurse_id="450065", account_id="a450065", group_id=HOME, office_id="o1",
        name="전수빈", active=1, is_night_nurse=is_night, work_shifts=[],
    )
    db.add(n)
    db.flush()
    return n


def _dispatch(db, *, target=TARGET, shift_types, start=date(2026, 6, 19)):
    a = NurseAssignment(
        nurse_id="450065", source_group_id=HOME, target_group_id=target,
        office_id="o1", start_date=start, reason="파견", status="active",
        target_shift_types=shift_types,
    )
    db.add(a)
    db.flush()
    return a


def _n_row(n_exact=2):
    return {
        "group_id": TARGET, "nurse_id": "450065", "year": 2026, "month": 6,
        "n_min": n_exact, "n_exact": n_exact,
    }


def _blocked(issues):
    return any(i["reason_code"] == "MONTHLY_LIMIT_NOT_IN_WORK_SHIFTS" for i in issues)


def test_effective_view_overrides_capability(db):
    n = _nurse(db, ["D"])
    _dispatch(db, shift_types=["D", "N"])
    eff = _effective_nurse_for_group(db, n, TARGET, 2026, 6)
    assert isinstance(eff, _EffectiveNurseView)
    assert set(eff.is_night_nurse) == {"D", "N"}  # 오버레이 적용
    assert eff.name == "전수빈" and eff.nurse_id == "450065"  # 나머지는 위임


def test_effective_fallback_no_dispatch(db):
    n = _nurse(db, ["D"])
    assert _effective_nurse_for_group(db, n, TARGET, 2026, 6) is n


def test_effective_fallback_dispatch_other_group(db):
    n = _nurse(db, ["D"])
    _dispatch(db, target="other_grp", shift_types=["D", "N"])
    # 활성 파견이 있어도 target_group 이 다르면 오버레이 안 함
    assert _effective_nurse_for_group(db, n, TARGET, 2026, 6) is n


def test_base_D_only_blocks_N(db):
    """가드(수정 전 동작): base D전담 nurse 에 N=2 → 차단."""
    n = _nurse(db, ["D"])
    issues = validate_monthly_limit_row(
        row=_n_row(2), nurse=n, cap_days=30, year=2026, month=6, max_night=15,
    )
    assert _blocked(issues)


def test_inbound_DN_allows_N(db):
    """수정: 파견지 D/N effective nurse → N=2 허용(차단 없음)."""
    n = _nurse(db, ["D"])
    _dispatch(db, shift_types=["D", "N"])
    eff = _effective_nurse_for_group(db, n, TARGET, 2026, 6)
    issues = validate_monthly_limit_row(
        row=_n_row(2), nurse=eff, cap_days=30, year=2026, month=6, max_night=15,
    )
    assert not _blocked(issues)
