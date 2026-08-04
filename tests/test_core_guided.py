"""Core-guided 사이클 — solver assumption core → graph 설명 → repair → solver 재검증.

+ 독립 ScheduleValidator(운영 출력 검증).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

import exact_oracle  # noqa: E402

exact_oracle._BUDGET = 200_000

from exact_oracle import is_feasible  # noqa: E402
from services.ontology_graph.core_guided import (  # noqa: E402
    compile_cpsat_assumptions,
    core_days,
    core_guided_diagnosis,
)
from services.ontology_graph.roster_ir import parse_to_ir  # noqa: E402
from services.ontology_graph.schedule_validator import validate_schedule  # noqa: E402
from services.ontology_graph.verifier import ProductionCpSatVerifier  # noqa: E402

_EXACT = ProductionCpSatVerifier(lambda nu, c, d: is_feasible(nu, c, d))


def _banned_case():
    nu = [{"nurse_id": "x", "allowed_shifts": ["N"]}] + [{"nurse_id": f"n{i}"} for i in range(4)]
    cfg = {"two_offs_after_three_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
           "initial_constraints": {"forbidden": {"x": {2: ["O"], 3: ["O"], 4: ["O"], 5: ["O"]}},
                                   "forced_off": {}}}
    return nu, cfg, 7


def test_assumption_core_narrows_to_implicated_cells():
    nu, cfg, D = _banned_case()
    cr = compile_cpsat_assumptions(parse_to_ir(nu, cfg, D))
    assert cr.feasible is False
    # core 는 x 의 강제근무 셀들을 지목(범위 축소)
    assert ("x", 2) in cr.core["cells"] or 2 in core_days(cr.core)
    assert core_days(cr.core)                       # 비어있지 않음


def test_full_cycle_certificate_and_verified_repairs():
    nu, cfg, D = _banned_case()
    res = core_guided_diagnosis(nu, cfg, D, verifier=_EXACT)
    assert res.solver_feasible is False
    assert res.certificate is not None              # graph 구조 원인
    # exact verifier → solver_verified repair 존재
    assert any(r.solver_verified is True for r in res.repairs)
    assert res.repairs[0].solver_verified is True   # 랭킹: 확정 먼저


def test_feasible_case_ends_cycle():
    nu = [{"nurse_id": f"n{i}"} for i in range(6)]
    cfg = {"not_one_night": True, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    res = core_guided_diagnosis(nu, cfg, 6, verifier=_EXACT)
    assert res.solver_feasible is True
    assert res.repairs == []


def test_schedule_validator_catches_sequence_and_coverage():
    nu = [{"nurse_id": f"n{i}"} for i in range(5)]
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    # 고립 N (n2 day1 O after single N) → sequence 위반
    bad = {"n0": "DDDDD", "n1": "EEEEE", "n2": "NONON", "n3": "OOOOO", "n4": "OOOOO"}
    r = validate_schedule(bad, nu, cfg, 5)
    assert not r.valid
    assert any(v["kind"] == "sequence_or_cell" for v in r.violations)


def test_schedule_validator_accepts_valid():
    nu = [{"nurse_id": f"n{i}"} for i in range(4)]
    cfg = {"not_one_night": True, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    # 매일 D/E/N 각 1 + 1명 OFF (시퀀스 위반 없음: N 은 n2 가 연속)
    good = {"n0": "DDDD", "n1": "EEEE", "n2": "NNNN", "n3": "OOOO"}
    r = validate_schedule(good, nu, cfg, 4)
    assert r.valid, r.violations
