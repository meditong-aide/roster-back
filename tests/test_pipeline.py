"""Solver-guided 파이프라인 — graph INFEASIBLE presolve → solver.

교정: graph FEASIBLE 은 solver 를 생략하지 않는다(실제 생성·최적화 필요). solver 생략은
sound INFEASIBLE certificate 를 얻었을 때만.
"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

import exact_oracle  # noqa: E402

exact_oracle._BUDGET = 120_000

from exact_oracle import is_feasible  # noqa: E402
from fuzz_crossval import _rand_case  # noqa: E402
from services.ontology_graph.pipeline import diagnose_pipeline  # noqa: E402
from services.ontology_graph.verifier import ProductionCpSatVerifier  # noqa: E402

# exact verifier(오라클 래핑) 주입 → 파이프라인 fallback 이 exact
_EXACT = ProductionCpSatVerifier(lambda nu, c, d: is_feasible(nu, c, d))


def test_only_infeasible_certificate_skips_solver():
    """graph INFEASIBLE → solver 생략. FEASIBLE/UNKNOWN → solver(exact) 실행."""
    rng = random.Random(5)
    ck = wrong = 0
    skip_all_infeasible = True
    for _ in range(120):
        nu, cfg, D = _rand_case(rng)
        orc = is_feasible(nu, cfg, D)
        if orc is None:
            continue
        r = diagnose_pipeline(nu, cfg, D, graph_budget=300_000, verifier=_EXACT)
        if r.status == "UNKNOWN":
            continue
        ck += 1
        # solver 생략은 INFEASIBLE(graph_certificate) 일 때만
        if not r.solver_invoked and r.via != "graph_certificate":
            skip_all_infeasible = False
        if r.via == "graph_certificate" and r.status != "INFEASIBLE":
            skip_all_infeasible = False
        if (r.status == "INFEASIBLE") != (orc is False):
            wrong += 1
    assert ck > 20
    assert wrong == 0
    assert skip_all_infeasible                 # 생략은 오직 INFEASIBLE certificate


def test_graph_infeasible_short_circuits():
    nu = [{"nurse_id": f"n{i}"} for i in range(5)]
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    r = diagnose_pipeline(nu, cfg, 6)
    assert r.status == "INFEASIBLE" and r.via == "graph_certificate"
    assert r.solver_invoked is False and r.certificate is not None


def test_feasible_invokes_solver():
    """지원범위 FEASIBLE 이라도 solver 를 실행한다(생성·최적화)."""
    nu = [{"nurse_id": f"n{i}"} for i in range(6)]
    cfg = {"not_one_night": True, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    r = diagnose_pipeline(nu, cfg, 6)
    assert r.solver_invoked is True
    assert r.via == "solver"
