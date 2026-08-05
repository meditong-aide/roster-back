"""Exact scope manifest + component ownership invariant — 숨은 false feasible 방지 락.

미지원 hard constraint(월 quota·주말휴무·grade 등) 활성이면 exact 주장 금지(UNKNOWN).
간호사가 복수 component 에 걸치면(전체 horizon 재sweep 이 겹침) UNKNOWN.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from services.ontology_graph.frontier_dp import diagnose_frontier  # noqa: E402
from services.ontology_graph.hybrid_solver import solve_hybrid  # noqa: E402
from services.ontology_graph.scope_manifest import exact_or_unknown, unmodeled_active  # noqa: E402


def _cfg(extra=None):
    c = {"not_one_night": True, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
         "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    if extra:
        c.update(extra)
    return c


def test_supported_scope_returns_none():
    nu = [{"nurse_id": f"n{i}"} for i in range(4)]
    assert exact_or_unknown(nu, _cfg()) is None


def test_monthly_quota_gates_feasible_only():
    """비대칭: 미지원(n_exact) + 지원 subset FEASIBLE → UNKNOWN. subset INFEASIBLE → 그대로."""
    # 지원 subset 이 feasible 한 넉넉 케이스(6명>3슬롯) → 미지원 제약 때문에 UNKNOWN
    nu_feas = [{"nurse_id": f"n{i}"} for i in range(6)]
    nu_feas[0]["n_exact"] = 13
    assert "nurse.n_exact" in unmodeled_active(nu_feas, _cfg())
    assert diagnose_frontier(nu_feas, _cfg(), 5).status == "UNKNOWN"
    assert solve_hybrid(nu_feas, _cfg(), 5).status == "UNKNOWN"


def test_infeasible_subset_returned_despite_unmodeled():
    """지원 subset 이 이미 INFEASIBLE 이면 미지원 제약이 있어도 certificate 그대로(sound)."""
    # 2명인데 D1E1N1=3슬롯/일 → 지원 subset(커버리지)만으로 infeasible
    nu = [{"nurse_id": "n0", "n_exact": 13}, {"nurse_id": "n1", "is_weekend_off": True}]
    assert unmodeled_active(nu, _cfg())              # 미지원 활성
    assert diagnose_frontier(nu, _cfg(), 5).status == "INFEASIBLE_CERTIFIED"
    assert solve_hybrid(nu, _cfg(), 5).status == "INFEASIBLE_CERTIFIED"


def test_config_level_unmodeled_flags():
    nu = [{"nurse_id": "n0"}]
    assert exact_or_unknown(nu, _cfg({"off_days": 9})) is not None
    assert exact_or_unknown(nu, _cfg({"max_nig_per_month": 15})) is not None


def test_night_only_via_allowed_is_supported():
    """야간전담을 allowed_shifts=['N'] 로 표현하면 지원(is_night_only 플래그와 중복 아님)."""
    nu = [{"nurse_id": "n0", "allowed_shifts": ["N"], "is_night_only": True}]
    assert exact_or_unknown(nu, _cfg()) is None


def test_hybrid_feasible_subset_with_unmodeled_is_unknown():
    """지원 subset FEASIBLE + 미지원(주말휴무) → UNKNOWN(feasible 주장 금지)."""
    nu = [{"nurse_id": f"n{i}"} for i in range(6)]
    nu[0]["is_weekend_off"] = True
    assert solve_hybrid(nu, _cfg(), 5).status == "UNKNOWN"


def test_health_leave_flags_unmodeled():
    """보건휴가(OFF 하한 +1 HARD) 활성 시 그래프 미지원 → FEASIBLE 주장 금지(UNKNOWN)."""
    from services.ontology_graph.scope_manifest import unmodeled_active
    nu = [{"nurse_id": f"n{i}"} for i in range(6)]
    assert "config.health_leave_enabled" in unmodeled_active(nu, _cfg({"health_leave_enabled": True}))
    nu2 = [dict(n) for n in nu]
    nu2[0]["health_leave_extra_off"] = True
    assert "nurse.health_leave_extra_off" in unmodeled_active(nu2, _cfg())
    # 지원 subset feasible + 보건휴가 → UNKNOWN(비대칭 scope)
    assert solve_hybrid(nu, _cfg({"health_leave_enabled": True}), 6).status == "UNKNOWN"


def test_preceptee_shift_count_false_flags_unmodeled():
    """프리셉티 커버리지 제외(shift_count=False)만 미지원. 기본 True(일치)면 지원."""
    from services.ontology_graph.scope_manifest import unmodeled_active
    nu = [{"nurse_id": f"n{i}"} for i in range(5)]
    # 기본(둘 다 카운트) = 그래프와 일치 → 지원
    assert unmodeled_active(nu, _cfg({"preceptee_on": True})) == []
    # 제외(그래프 과다=낙관) → FEASIBLE 불확실 → 미지원 표시
    assert "config.preceptee_shift_count=False" in unmodeled_active(
        nu, _cfg({"preceptee_on": True, "preceptee_shift_count": False}))
    # preceptee 기능 자체 off → 무관
    assert unmodeled_active(nu, _cfg({"preceptee_on": False, "preceptee_shift_count": False})) == []
