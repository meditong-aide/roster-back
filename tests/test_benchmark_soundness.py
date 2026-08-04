"""벤치마크 soundness 가드 — graph 는 infeasible 을 절대 false-certify 하지 않는다.

전체 벤치마크는 tools/infeasible_cases/benchmark.py. 여기선 CI 용 소량 + 핵심 불변식.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

import exact_oracle  # noqa: E402

exact_oracle._BUDGET = 120_000

from benchmark import run  # noqa: E402


def test_no_false_certificate_and_certifies_infeasible():
    agg = run(n=80, seed=1, graph_budget=200_000)
    assert agg["graph_false_cert"] == 0          # 핵심: 오인증 0(sound)
    assert agg["inf"] > 0
    assert agg["graph_cert"] >= agg["inf"] * 0.8  # 대부분 인증
