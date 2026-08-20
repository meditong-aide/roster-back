"""Canonical IR — 하나의 명세 → 두 backend(graph·CP-SAT), differential test.

round-trip(parse→compile 무손실)과 IR→graph 가 oracle 과 일치함을 잠근다. IR→CP-SAT 는 독립
backend 로 컴파일되나 회복 인코딩이 근사라 3연속-회복 케이스에서 불일치 가능(정보용).
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
from services.ontology_graph.roster_ir import (  # noqa: E402
    CellDomainRule,
    CoverageRule,
    NightRecoveryRule,
    compile_cpsat,
    compile_graph,
    parse_to_ir,
)


def test_roundtrip_and_graph_matches_oracle():
    """parse→compile 무손실 + IR-compiled graph == oracle (지원 제약 변환 누락 0)."""
    rng = random.Random(11)
    rt = gvo = checked = 0
    for _ in range(120):
        nu, cfg, D = _rand_case(rng)
        orc = is_feasible(nu, cfg, D)
        if orc is None:
            continue
        ir = parse_to_ir(nu, cfg, D)
        nu2, cfg2 = compile_graph(ir)
        s_orig = solve_hybrid(nu, cfg, D, budget=250_000).status
        s_ir = solve_hybrid(nu2, cfg2, D, budget=250_000).status
        if "UNKNOWN" not in (s_orig, s_ir) and s_orig != s_ir:
            rt += 1
        if s_ir == "UNKNOWN":
            continue
        checked += 1
        if (s_ir == "INFEASIBLE_CERTIFIED") != (orc is False):
            gvo += 1
    assert checked > 20
    assert rt == 0
    assert gvo == 0


def test_ir_extracts_rules_and_unsupported():
    nu = [{"nurse_id": "n0", "allowed_shifts": ["N"], "n_exact": 13},
          {"nurse_id": "n1"}]
    cfg = {"two_offs_after_three_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {"n0": {2: ["O"]}}, "forced_off": {"n1": [3]}}}
    ir = parse_to_ir(nu, cfg, 6)
    kinds = {type(r).__name__ for r in ir.rules}
    assert "NightRecoveryRule" in kinds and "CoverageRule" in kinds and "CellDomainRule" in kinds
    assert any(isinstance(r, NightRecoveryRule) and r.trigger_run == 3 for r in ir.rules)
    assert any(isinstance(r, CoverageRule) and r.reqN == 2 for r in ir.rules)
    assert any(isinstance(r, CellDomainRule) and r.forced_off for r in ir.rules)
    assert "nurse.n_exact" in ir.unsupported          # 미지원은 격리


def test_cpsat_backend_compiles_and_agrees_on_two_rule():
    """IR→CP-SAT: 2N2OFF(정확 인코딩) 케이스에서 oracle 과 일치."""
    nu = [{"nurse_id": f"n{i}"} for i in range(5)]
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    ir = parse_to_ir(nu, cfg, 6)
    cp = compile_cpsat(ir)
    assert cp is not None
    assert cp == is_feasible(nu, cfg, 6)              # 둘 다 INFEASIBLE(회복OFF starvation)
