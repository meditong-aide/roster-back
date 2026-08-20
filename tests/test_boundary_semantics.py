"""Step 0 — BoundaryState 계약 + relation-form message + terminal + rejection trace.

hybrid(component 분리→component frontier DP→AND) 가 exact 하려면 component 경계로 나르는 것이
단순 근무값이 아니라 joint BoundaryState 여야 하고, message 가 정확히 합성돼야 한다.
"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from fuzz_crossval import _rand_case  # noqa: E402
from services.ontology_graph.frontier_dp import (  # noqa: E402
    _prep,
    diagnose_frontier,
    fresh_joint,
    frontier_message,
    terminal_ok,
)


def test_message_composition_is_exact():
    """message(0,mid) 후 message(mid,D) 의 exit == message(0,D) 의 exit (계약이 이어짐)."""
    rng = random.Random(3)
    checked = 0
    for _ in range(120):
        nu, cfg, D = _rand_case(rng)
        if D < 3:
            continue
        prep = _prep(nu, cfg)
        k = len(prep)
        mid = D // 2
        full = frontier_message(prep, cfg, 0, D, {fresh_joint(k)}, symmetry=False)
        m1 = frontier_message(prep, cfg, 0, mid, {fresh_joint(k)}, symmetry=False)
        if m1.overflow or full.overflow:
            continue
        m2 = frontier_message(prep, cfg, mid, D, set(m1.exit_frontier), symmetry=False)
        if m2.overflow:
            continue
        checked += 1
        assert full.exit_frontier == m2.exit_frontier
    assert checked > 30


def test_terminal_ok_closed_horizon():
    """closed-horizon: 열린 run(r>0) 도 회복빚(k>0) 도 없어야 종료 허용(자기완결)."""
    cfg = {"not_one_night": True}
    assert terminal_ok(((0, 0, 0, ""),), cfg) is True
    assert terminal_ok(((1, 0, 0, ""),), cfg) is False    # 열린 야간 run
    assert terminal_ok(((0, 1, 0, ""),), cfg) is False    # 회복 OFF 빚
    assert terminal_ok(((2, 0, 0, ""),), cfg) is False     # run 끝냈어도 회복 미완=애매→불가


def test_closed_terminal_rejects_lone_night_end():
    """1명·1일·N수요1·not_one_night: lenient=feasible(다음달로 전달), closed=infeasible."""
    nu = [{"nurse_id": "n0", "name": "N0", "grade": 1, "team_id": "A", "allowed_shifts": ["N"]}]
    cfg = {"not_one_night": True, "daily_shift_requirements": {"D": 0, "E": 0, "N": 1},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    assert diagnose_frontier(nu, cfg, 1, terminal="lenient").status == "FEASIBLE_WITNESS"
    assert diagnose_frontier(nu, cfg, 1, terminal="closed").status == "INFEASIBLE_CERTIFIED"


def test_rejection_trace_embedded_in_collapse():
    """붕괴 cert 에 최소 rejection trace(day·dead/live·best_cov·binding)가 처음부터 실려있다."""
    nu = [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A"} for i in range(5)]
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    r = diagnose_frontier(nu, cfg, 6)
    assert r.status == "INFEASIBLE_CERTIFIED"
    rej = r.certificate.witness["rejection"]
    assert set(rej) >= {"day", "dead_states", "live_states", "best_cov", "binding"}
    assert isinstance(rej["day"], int)
    # factor-level: 제거 사유가 factor 종류별로 집계됨(coverage 붕괴)
    assert "rejected_by" in rej
    assert sum(rej["rejected_by"].values()) > 0


def test_rejection_trace_attributes_personal_sequence_collapse():
    """N전담 banned 4연속: 모든 상태가 **개인 시퀀스 factor**로 사망(personal_dead)."""
    nu = [{"nurse_id": "x", "name": "X", "grade": 1, "team_id": "A", "allowed_shifts": ["N"]}]
    nu += [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A"} for i in range(4)]
    cfg = {"two_offs_after_three_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
           "initial_constraints": {"forbidden": {"x": {2: ["O"], 3: ["O"], 4: ["O"], 5: ["O"]}},
                                   "forced_off": {}}}
    r = diagnose_frontier(nu, cfg, 7)
    rej = r.certificate.witness["rejection"]
    assert rej["rejected_by"]["personal_dead"] > 0
    assert rej["rejected_by"]["D_coverage"] == 0    # 커버리지가 아니라 개인 시퀀스가 원인
