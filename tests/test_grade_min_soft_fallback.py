"""Grade min 'G1≥1' soft fallback 검증 (Phase 2 경량 결정).

grade 시점 테이블(nurse_grade_period)은 폐기하고 경량(캐시+target_grade override)으로 가되,
grade min 룰은 **hard 단독이 아닌 soft(allow_soft_fallback=True)** 를 권장한다.

이 테스트는 `_add_minimum_constraints` 가:
  - soft: 공급<수요여도 FEASIBLE(slack 흡수) + 페널티 항 노출, 공급충분 시 0-페널티 달성
  - hard: 공급<수요면 INFEASIBLE
임을 못박아, '권장 방향(soft)'이 G1 부족 달에 즉시 infeasible 되지 않음(= '예민함' 제거)을 보장한다.

참조: app/services/constraints/grade_constraints.py::_add_minimum_constraints
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from services.constraints.grade_constraints import _add_minimum_constraints


def _build(target_g1: int, n_g1_nurses: int, allow_soft: bool):
    """G1 등급 n명, 1일·1시프트(D), G1 최소 target_g1 명 모델."""
    m = cp_model.CpModel()
    x = {(n, 0, 0): m.NewBoolVar(f"x_{n}") for n in range(n_g1_nurses)}

    def X(n, d, s):
        return x[(n, d, s)]

    terms = _add_minimum_constraints(
        m, X, by_grade={1: list(range(n_g1_nurses))},
        day_idx=0, shift_idx=0, shift_code="D",
        target={1: target_g1}, allow_soft_fallback=allow_soft, penalty_weight=100,
    )
    return m, x, terms


def test_grade_min_soft_feasible_when_g1_short():
    """G1 1명뿐인데 2명 요구 → soft 면 slack 으로 FEASIBLE (즉시 infeasible 아님)."""
    m, x, terms = _build(target_g1=2, n_g1_nurses=1, allow_soft=True)
    status = cp_model.CpSolver().Solve(m)
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    assert len(terms) == 1  # slack penalty 항 노출(목적함수에서 차감)


def test_grade_min_hard_infeasible_when_g1_short():
    """같은 부족 상황 + hard → INFEASIBLE (= 순수 hard 의 '예민함')."""
    m, x, terms = _build(target_g1=2, n_g1_nurses=1, allow_soft=False)
    status = cp_model.CpSolver().Solve(m)
    assert status == cp_model.INFEASIBLE
    assert terms == []  # hard 는 목적 항 없음


def test_grade_min_soft_zero_penalty_when_available():
    """G1 2명·1명 요구, soft + 페널티 최소화 → G1 충족(slack 0, 페널티 0) 달성 가능."""
    m, x, terms = _build(target_g1=1, n_g1_nurses=2, allow_soft=True)
    m.Maximize(sum(terms))  # terms=[-w*slack] → maximize == slack 최소화
    solver = cp_model.CpSolver()
    status = solver.Solve(m)
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    assert solver.ObjectiveValue() == 0  # slack 0 = G1 min 완전 충족
    assert sum(solver.Value(x[(n, 0, 0)]) for n in range(2)) >= 1
