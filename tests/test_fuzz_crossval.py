"""Randomized 교차검증 회귀 — frontier DP ⟷ 독립 oracle, 대칭 auto soundness.

전체 하니스(수천건·CP-SAT)는 tools/infeasible_cases/fuzz_crossval.py. 여기선 CI 용 소량:
false-INFEASIBLE=0, false-FEASIBLE=0, auto(대칭 자동감지)=plain 을 잠근다.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

import exact_oracle  # noqa: E402

exact_oracle._BUDGET = 120_000        # CI 속도용 낮은 상한(느린 case→UNKNOWN skip)

from fuzz_crossval import main  # noqa: E402


def test_frontier_matches_oracle_and_symmetry_sound():
    """소량 CI 샘플. 전량(수천·CP-SAT)은 tools/infeasible_cases/fuzz_crossval.py."""
    agg, ex = main(n=200, seed=101, use_cpsat=False)
    assert agg["false_inf"] == 0, ex["false_inf"]
    assert agg["false_feas"] == 0, ex["false_feas"]
    assert agg["auto_mismatch"] == 0, ex["auto_mismatch"]
    assert agg["sym_fired"] > 0            # 대칭 축소가 실제로 발동했는가(검증 유효성)
