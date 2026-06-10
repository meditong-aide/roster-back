"""Grade cascade: 인원 0인 grade 의 '시니어 ≥N' 요구를 다음 상위(존재) grade 로 이양.

정책: grade 1=최상위. grade 1 없으면 2, 2 없으면 3... hard 가 '설정 grade 부재'만으로
헛 infeasible 나는 것 방지. min 만 적용, max(anti-pair)는 미적용.
참조: app/services/constraints/grade_constraints.py
"""

from __future__ import annotations

from services.constraints.grade_constraints import (
    _cascade_constraints_to_existing_grades,
)


class _N:
    def __init__(self, grade):
        self.grade = grade


class _RS:
    def __init__(self, grades):
        self.nurses = [_N(g) for g in grades]


def test_no_grade1_falls_to_grade2():
    rs = _RS([2, 2, 3])  # grade 1 인원 없음
    out = _cascade_constraints_to_existing_grades(rs, {"D": {1: 1}, "E": {1: 1}})
    assert out == {"D": {2: 1}, "E": {2: 1}}


def test_identity_when_grade_present():
    rs = _RS([1, 2, 3])
    cmap = {"D": {1: 1}, "N": {1: 1}}
    assert _cascade_constraints_to_existing_grades(rs, cmap) == cmap


def test_chains_to_grade3_when_1_and_2_absent():
    rs = _RS([3, 3])
    assert _cascade_constraints_to_existing_grades(rs, {"N": {1: 1}}) == {"N": {3: 1}}


def test_no_higher_grade_keeps_original():
    # grade 2 요구인데 2 없음 + 더 상위(>2) 없음 → 원래 유지(진짜 불가는 노출)
    rs = _RS([1, 1])
    assert _cascade_constraints_to_existing_grades(rs, {"D": {2: 1}}) == {"D": {2: 1}}


def test_merge_uses_max_not_sum():
    # grade1(없음)→2 이양 + 기존 grade2 요구 → max 로 합쳐 demand 부풀림 방지
    rs = _RS([2, 2])
    out = _cascade_constraints_to_existing_grades(rs, {"D": {1: 1, 2: 1}})
    assert out == {"D": {2: 1}}


def test_empty_or_no_nurses_is_noop():
    assert _cascade_constraints_to_existing_grades(_RS([]), {"D": {1: 1}}) == {"D": {1: 1}}
    assert _cascade_constraints_to_existing_grades(_RS([1]), {}) == {}
