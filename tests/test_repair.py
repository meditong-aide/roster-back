"""Graph repair → 이중검증 — graph(domain_verified: FEASIBLE만) + verifier.

domain_verified 는 FEASIBLE_WITNESS 만 True(UNKNOWN≠verified). 근사 shadow 는 shadow_cpsat 로
분리(solver_verified 는 운영 exact verifier 로만). feasible 이면 후보 없음.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from services.ontology_graph.repair import verify_repairs  # noqa: E402
from services.ontology_graph.verifier import (  # noqa: E402
    ProductionCpSatVerifier,
    VerificationResult,
)


def _pool(k):
    return [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A"} for i in range(k)]


def test_domain_verified_only_true_on_feasible():
    """UNKNOWN 을 domain_verified 로 처리하지 않는다(과거 재발 버그)."""
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    reps = verify_repairs(_pool(5), cfg, 6)
    assert reps
    for r in reps:
        assert r.domain_verified in (True, False, None)
        # domain_verified True 는 graph status 가 정확히 FEASIBLE_WITNESS 일 때만
        if r.domain_verified is True:
            assert r.domain_status == "FEASIBLE_WITNESS"


def test_shadow_not_called_solver_verified():
    """근사 shadow CP-SAT 결과는 shadow_cpsat 에만, solver_verified 는 None(운영 verifier 없음)."""
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    reps = verify_repairs(_pool(5), cfg, 6)   # 기본 ShadowIRVerifier
    assert all(r.solver_verified is None for r in reps)
    assert any(r.shadow_cpsat is True for r in reps)   # shadow 는 채워짐


def test_production_verifier_sets_solver_verified():
    """운영 exact verifier(exact=True) 주입 시에만 solver_verified 채워짐."""
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}

    # 스텁: reduce_coverage 로 N 이 1 이하가 되면 feasible 이라고 판정하는 가짜 운영 solver
    def fake_solve(nu, c, nd):
        return (c["daily_shift_requirements"]["N"] <= 1)
    v = ProductionCpSatVerifier(fake_solve)
    reps = verify_repairs(_pool(5), cfg, 6, verifier=v)
    top = reps[0]
    assert top.solver_verified is True
    assert top.action == "reduce_coverage" and top.target["shift"] == "N"


def test_no_repairs_when_feasible():
    cfg = {"not_one_night": True, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    assert verify_repairs(_pool(6), cfg, 6) == []
