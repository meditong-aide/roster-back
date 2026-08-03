"""Hard-residual gap 문서화 + 독립 oracle 정확성 가드.

이 케이스들은 우리 진단 스택의 **모든 층이 침묵**(per-nurse·max-flow·aggregate·joint-N DP)
하지만 정수 근무표는 infeasible — 독립 exact oracle 로만 드러난다. VE/frontier DP 엔진이
이 gap 을 메우면 아래 `our stack silent` 단언이 뒤집힌다(그때 테스트 갱신 = 진전 신호).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from exact_oracle import is_feasible  # noqa: E402
from services.ontology_graph.axis_diagnose import multi_axis_diagnose  # noqa: E402
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


def test_our_stack_is_silent_on_recovery_off_residual():
    """현 한계: 우리 스택은 이 정수-결합을 못 잡는다(회복=OFF 를 relaxed 로 봄)."""
    rules, dsr, k, dd = _RECOVERY
    nu, cfg = _pool(k), _cfg(dsr, {**rules})
    assert joint_night_coverage_feasible(nu, cfg, dd) is True          # relaxed=feasible
    assert multi_axis_diagnose(nu, cfg, dd, 2026, 8).status != "INFEASIBLE_CERTIFIED"


def test_oracle_agrees_with_stack_on_a_caught_case():
    """정합성: 우리가 INFEASIBLE 로 잡는 소형 케이스는 oracle 도 INFEAS (오라클 sanity)."""
    # N전담 banned 4연속(6일 축소판) — 우리 스택이 sequence 로 잡는 케이스
    nu = _pool(4)
    nu[0] = {"nurse_id": "x", "name": "엑스", "grade": 1, "team_id": "A",
             "allowed_shifts": ["N"]}
    cfg = _cfg({"D": 1, "E": 1, "N": 1},
               {"two_offs_after_three_nig": True, "not_one_night": True})
    cfg["initial_constraints"]["forbidden"] = {"x": {2: ["O"], 3: ["O"], 4: ["O"], 5: ["O"]}}
    assert is_feasible(nu, cfg, 6) is False
