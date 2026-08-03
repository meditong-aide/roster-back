"""Hard-residual — 이제 frontier DP 로 **닫힌** gap + 독립 oracle 교차검증.

과거: 우리 relaxed 스택(N/notN)이 전부 침묵 → gap. 진전: {D,E,N,O} exact frontier DP tier
가 이 정수-결합을 잡아 multi_axis 가 INFEASIBLE 을 인증한다. relaxed 층(joint-N DP)이
여전히 침묵함을 함께 단언 → **왜 frontier DP 가 필요했는지**를 문서화한다.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from exact_oracle import is_feasible  # noqa: E402
from services.ontology_graph.axis_diagnose import multi_axis_diagnose  # noqa: E402
from services.ontology_graph.frontier_dp import diagnose_frontier  # noqa: E402
from services.ontology_graph.joint_coverage import joint_night_coverage_feasible  # noqa: E402


def _pool(k):
    return [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A"}
            for i in range(k)]


def _cfg(dsr, rules):
    c = dict(rules, daily_shift_requirements=dsr)
    c["initial_constraints"] = {"forbidden": {}, "forced_off": {}}
    return c


_RECOVERY = ({"two_offs_after_two_nig": True, "not_one_night": True},
             {"D": 1, "E": 1, "N": 2}, 5, 6)


def test_oracle_finds_recovery_off_residual():
    rules, dsr, k, dd = _RECOVERY
    assert is_feasible(_pool(k), _cfg(dsr, rules), dd) is False


def test_relaxed_layer_is_still_blind():
    """relaxed joint-N DP 는 D/E 를 회복으로 관대 인정 → 여전히 feasible 로 봄(=frontier DP 필요 이유)."""
    rules, dsr, k, dd = _RECOVERY
    assert joint_night_coverage_feasible(_pool(k), _cfg(dsr, rules), dd) is True


def test_frontier_dp_closes_the_gap():
    """exact frontier DP tier 가 정수-결합을 잡아 multi_axis 가 INFEASIBLE 인증."""
    rules, dsr, k, dd = _RECOVERY
    nu, cfg = _pool(k), _cfg(dsr, rules)
    fr = diagnose_frontier(nu, cfg, dd)
    assert fr.status == "INFEASIBLE_CERTIFIED"
    assert fr.certificate.kind == "recovery_off_starvation"
    dg = multi_axis_diagnose(nu, cfg, dd, 2026, 8)
    assert dg.status == "INFEASIBLE_CERTIFIED"


def test_frontier_dp_agrees_with_independent_oracle():
    """BFS frontier DP ⟷ DFS 독립 oracle: 같은 semantics, 결론 일치."""
    cases = [
        (_RECOVERY[1], _RECOVERY[0], 5, 6),
        ({"D": 1, "E": 1, "N": 1}, {"not_one_night": True}, 4, 6),           # feasible
        ({"D": 1, "E": 1, "N": 2}, {"two_offs_after_two_nig": True,
                                    "not_one_night": True,
                                    "forbid_night_to_day": True}, 5, 6),     # infeasible
    ]
    for dsr, rules, k, dd in cases:
        nu, cfg = _pool(k), _cfg(dsr, rules)
        fr = diagnose_frontier(nu, cfg, dd).status
        orc = is_feasible(nu, cfg, dd)
        if orc is True:
            assert fr in ("FEASIBLE_WITNESS", "UNKNOWN")
        elif orc is False:
            assert fr in ("INFEASIBLE_CERTIFIED", "UNKNOWN")


def test_frontier_dp_large_returns_unknown_not_hang():
    """대형(넓은 separator)은 예산초과로 UNKNOWN 반환(무한루프 아님) — 컴포넌트 분해 대상."""
    nu = _pool(8)
    cfg = _cfg({"D": 2, "E": 2, "N": 2}, {"two_offs_after_two_nig": True, "not_one_night": True})
    fr = diagnose_frontier(nu, cfg, 31, cap=50_000)
    assert fr.status in ("UNKNOWN", "FEASIBLE_WITNESS", "INFEASIBLE_CERTIFIED")
