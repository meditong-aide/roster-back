"""1N ban 면제 helper 검증.

정책 (사용자 지정, 2026-05-17):
  nurse_monthly_limit 에서 N=1 가능성이 명시되면 (n_min / n_max / n_exact 중
  어느 하나라도 1) 해당 nurse 는 not_one_night hard 제약에서 면제.
"""

from __future__ import annotations

import pytest

from services.constraints.monthly_limit_constraints import (
    collect_single_n_allowed_nurse_indices,
)


class _Nurse:
    def __init__(self, mn=None, mx=None, ex=None):
        self.n_min, self.n_max, self.n_exact = mn, mx, ex


class _RS:
    def __init__(self, nurses):
        self.nurses = nurses


@pytest.mark.parametrize("label,nurse,expected_exempt", [
    ("n_exact=1",       _Nurse(ex=1),         True),    # 정확 1회
    ("n_max=1",         _Nurse(mx=1),         True),    # 상한 1
    ("n_min=1",         _Nurse(mn=1),         True),    # 하한 1 (NEW)
    ("n_min=1,n_max=5", _Nurse(mn=1, mx=5),   True),    # 하한 1, 상한 5 (NEW)
    ("n_min=1,n_max=1", _Nurse(mn=1, mx=1),   True),    # 둘 다 1
    ("n_exact=2",       _Nurse(ex=2),         False),   # 면제 X
    ("n_max=3",         _Nurse(mx=3),         False),   # 상한 3
    ("n_min=2",         _Nurse(mn=2),         False),   # 하한 2
    ("n_min=2,n_max=4", _Nurse(mn=2, mx=4),   False),
    ("none",            _Nurse(),             False),   # 명시 없음
])
def test_single_n_allowed_exempt_logic(label, nurse, expected_exempt):
    rs = _RS([nurse])
    exempt_set = collect_single_n_allowed_nurse_indices(rs)
    is_exempt = 0 in exempt_set
    assert is_exempt == expected_exempt, (
        f"[{label}] n_min={nurse.n_min} n_max={nurse.n_max} n_exact={nurse.n_exact}"
        f" → 면제={is_exempt}, expected={expected_exempt}"
    )


def test_multiple_nurses_mixed():
    nurses = [
        _Nurse(ex=1),                                    # 0: 면제
        _Nurse(mn=2, mx=4),                              # 1: 면제 X
        _Nurse(mn=1, mx=5),                              # 2: 면제 (NEW)
        _Nurse(),                                        # 3: 면제 X
        _Nurse(mx=1),                                    # 4: 면제
    ]
    exempt = collect_single_n_allowed_nurse_indices(_RS(nurses))
    assert exempt == {0, 2, 4}, f"expected {{0, 2, 4}}, got {exempt}"


def test_empty_roster_returns_empty_set():
    assert collect_single_n_allowed_nurse_indices(_RS([])) == set()


def test_n_exact_takes_precedence_over_min_max():
    """n_exact 가 있으면 (mn, mx) 정규화 시 (exact, exact) 로 우선."""
    n = _Nurse(mn=5, mx=10, ex=1)   # exact=1 우선 → 면제
    exempt = collect_single_n_allowed_nurse_indices(_RS([n]))
    assert 0 in exempt
