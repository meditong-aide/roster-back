"""Graph repair → CP-SAT verify — certificate 기반 복구 후보 이중 검증.

graph(domain_verified) + CP-SAT(solver_verified) 3-tier. feasible 이면 후보 없음.
2N2OFF(CP-SAT 정확 인코딩) 케이스로 solver_verified 를 확정 검증.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from services.ontology_graph.repair import verify_repairs  # noqa: E402


def _pool(k):
    return [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A"} for i in range(k)]


def test_repairs_dual_verified_on_recovery_starvation():
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    reps = verify_repairs(_pool(5), cfg, 6)
    assert reps
    # 최소 하나는 graph·CP-SAT 둘 다 통과(solver_verified) — N 수요 -1
    top = reps[0]
    assert top.solver_verified is True and top.domain_verified is True
    assert top.action == "reduce_coverage" and top.target["shift"] == "N"


def test_no_repairs_when_feasible():
    cfg = {"not_one_night": True, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    assert verify_repairs(_pool(6), cfg, 6) == []


def test_ranking_puts_solver_verified_first():
    """banned N전담: release 후보 중 solver_verified 가 앞에 온다(3-tier 랭킹)."""
    nu = [{"nurse_id": "x", "name": "X", "grade": 1, "team_id": "A", "allowed_shifts": ["N"]}]
    nu += _pool(4)
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
           "initial_constraints": {"forbidden": {"x": {2: ["O"], 3: ["O"], 4: ["O"]}},
                                   "forced_off": {}}}
    reps = verify_repairs(nu, cfg, 7)
    assert reps
    verified = [r for r in reps if r.solver_verified is True]
    if verified:
        # 첫 후보는 solver_verified 여야(랭킹)
        assert reps[0].solver_verified is True
