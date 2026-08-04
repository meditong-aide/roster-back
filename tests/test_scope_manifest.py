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


def test_monthly_quota_flags_unmodeled():
    nu = [{"nurse_id": "n0", "n_exact": 13}, {"nurse_id": "n1"}]
    assert "nurse.n_exact" in unmodeled_active(nu, _cfg())
    assert diagnose_frontier(nu, _cfg(), 5).status == "UNKNOWN"
    assert solve_hybrid(nu, _cfg(), 5).status == "UNKNOWN"


def test_config_level_unmodeled_flags():
    nu = [{"nurse_id": "n0"}]
    assert exact_or_unknown(nu, _cfg({"off_days": 9})) is not None
    assert exact_or_unknown(nu, _cfg({"max_nig_per_month": 15})) is not None


def test_night_only_via_allowed_is_supported():
    """야간전담을 allowed_shifts=['N'] 로 표현하면 지원(is_night_only 플래그와 중복 아님)."""
    nu = [{"nurse_id": "n0", "allowed_shifts": ["N"], "is_night_only": True}]
    assert exact_or_unknown(nu, _cfg()) is None


def test_hybrid_out_of_scope_certificate():
    nu = [{"nurse_id": "n0", "is_weekend_off": True}, {"nurse_id": "n1"}]
    r = solve_hybrid(nu, _cfg(), 5)
    assert r.status == "UNKNOWN"
    assert r.certificate.kind == "out_of_scope"
