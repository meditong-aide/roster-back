"""Solver-guided 파이프라인 — graph precheck → CP-SAT fallback.

graph 가 짧은 budget 에 certificate 를 내면 CP-SAT 생략(가속), 못 내면 CP-SAT. 오판 없음.
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


def test_pipeline_correct_and_skips_solver_when_graph_decides():
    rng = random.Random(5)
    ck = skipped = wrong = 0
    for _ in range(120):
        nu, cfg, D = _rand_case(rng)
        orc = is_feasible(nu, cfg, D)
        if orc is None:
            continue
        r = diagnose_pipeline(nu, cfg, D, graph_budget=300_000)
        if r.status == "UNKNOWN":
            continue
        ck += 1
        if r.cpsat_skipped:
            skipped += 1
        if (r.status == "INFEASIBLE") != (orc is False):
            wrong += 1
    assert ck > 20
    assert wrong == 0                       # 파이프라인 오판 없음
    assert skipped > ck // 2                # 상당수는 graph 가 solver 없이 확정


def test_graph_infeasible_skips_cpsat():
    nu = [{"nurse_id": f"n{i}"} for i in range(5)]
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    r = diagnose_pipeline(nu, cfg, 6)
    assert r.status == "INFEASIBLE" and r.via == "graph" and r.cpsat_skipped
    assert r.certificate is not None
