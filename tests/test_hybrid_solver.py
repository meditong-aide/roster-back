"""Hybrid inference — component 분해 + component frontier DP + AND.

dense(1 component)는 frontier DP 로 효율적 판정(plain conditioning 의 UNKNOWN 해소), sparse 는
component 분리. oracle 교차검증으로 correctness, 이중차감 회귀 케이스 포함.
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
from services.ontology_graph.hybrid_solver import solve_hybrid  # noqa: E402


def _pool(k, allowed=None):
    return [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A",
             **({"allowed_shifts": allowed[i]} if allowed else {})} for i in range(k)]


def _cfg(dsr, rules, fb=None, fo=None):
    c = dict(rules, daily_shift_requirements=dsr)
    c["initial_constraints"] = {"forbidden": fb or {}, "forced_off": fo or {}}
    return c


def test_hybrid_matches_oracle():
    rng = random.Random(7)
    fi = ff = ck = 0
    for _ in range(150):
        nu, cfg, D = _rand_case(rng)
        orc = is_feasible(nu, cfg, D)
        if orc is None:
            continue
        r = solve_hybrid(nu, cfg, D, budget=300_000)
        if r.status == "UNKNOWN":
            continue
        ck += 1
        if r.status == "INFEASIBLE_CERTIFIED" and orc is True:
            fi += 1
        if r.status != "INFEASIBLE_CERTIFIED" and orc is False:
            ff += 1
    assert ck > 30
    assert fi == 0 and ff == 0


def test_dense_recovery_solved_by_component_frontier():
    """dense 회복 starvation: 1 component → frontier DP → INFEASIBLE (conditioning 은 UNKNOWN)."""
    r = solve_hybrid(_pool(5), _cfg({"D": 1, "E": 1, "N": 2},
                                    {"two_offs_after_two_nig": True, "not_one_night": True}), 6)
    assert r.status == "INFEASIBLE_CERTIFIED"
    assert r.components == 1


def test_sparse_splits_into_two_component_frontier_dps():
    fo = {f"n{i}": [3, 4, 5] for i in range(3)}
    fo.update({f"n{i}": [0, 1, 2] for i in range(3, 6)})
    r = solve_hybrid(_pool(6), _cfg({"D": 1, "E": 0, "N": 1}, {"not_one_night": True}, fo=fo), 6)
    assert r.components == 2
    assert r.component_sizes == (3, 3)


def test_no_double_count_of_in_component_forced_nurse():
    """회귀: component 안 강제(singleton) 간호사 기여를 req 에서 이중차감하면 안 됨(과거 falseFEAS)."""
    nu = _pool(5, allowed=[["N"], None, None, None, ["N"]])
    cfg = _cfg({"D": 0, "E": 2, "N": 2}, {"not_one_night": True, "two_offs_after_three_nig": True},
               fb={"n1": {4: ["N"]}, "n4": {2: ["O"]}},
               fo={"n2": [1], "n3": [2], "n4": [3]})
    assert is_feasible(nu, cfg, 6) is False
    assert solve_hybrid(nu, cfg, 6).status == "INFEASIBLE_CERTIFIED"


def test_empty_domain():
    r = solve_hybrid(_pool(4), _cfg({"D": 1, "E": 1, "N": 1}, {"not_one_night": True},
                                    fb={"n0": {2: ["O"]}}, fo={"n0": [2]}), 5)
    assert r.status == "INFEASIBLE_CERTIFIED"
    assert r.certificate.kind == "empty_domain"
