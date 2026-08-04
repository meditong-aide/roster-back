"""Production adapter — 운영 solve 결과 표준화 + verifier + shadow 상관로깅.

이제 shadow 가 graph-only 가 아니라 production↔graph 비교(input_hash join).
"""

from __future__ import annotations

from types import SimpleNamespace

from services.ontology_graph.production_adapter import (
    make_production_verifier,
    standardize_status,
    status_to_verification,
)
from services.ontology_graph.shadow_diagnosis import log_production_status, run_shadow


def test_standardize_all_states():
    assert standardize_status(SimpleNamespace(_infeasible_empty=True), {"n": ["D"]})[0] == "INFEASIBLE"
    assert standardize_status(SimpleNamespace(_infeasible_empty=False), {"n": ["D"]})[0] == "FEASIBLE"
    assert standardize_status(SimpleNamespace(_infeasible_empty=False), {})[0] == "INFEASIBLE"
    assert standardize_status(None, {})[0] == "ERROR"
    assert standardize_status(SimpleNamespace(_infeasible_empty=False), {"n": ["D"]},
                              timeout=True)[0] == "TIMEOUT"


def test_verifier_factory_is_exact():
    v = make_production_verifier(lambda nu, c, d: True)
    r = v.check([], {}, 5)
    assert r.exact is True and r.feasible is True and r.backend == "production_cpsat"


def test_status_to_verification():
    assert status_to_verification("INFEASIBLE").feasible is False
    assert status_to_verification("FEASIBLE").feasible is True
    assert status_to_verification("TIMEOUT").feasible is None      # 이분법 아님


def test_graph_and_production_logs_correlate(monkeypatch):
    """graph 로그와 production 로그가 같은 input_hash → offline join 가능."""
    monkeypatch.setenv("AIDE_SHADOW_DIAGNOSIS", "1")
    nu = [{"nurse_id": f"n{i}", "allowed_shifts": []} for i in range(5)]
    cfg = {"two_offs_after_two_nig": True, "not_one_night": True,
           "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
           "initial_constraints": {"forbidden": {}, "forced_off": {}}}
    g = run_shadow(nu, cfg, 2026, 8, request_id="r1")
    p = log_production_status(nu, cfg, 2026, 8, "INFEASIBLE", request_id="r1")
    assert g["input_hash"] == p["input_hash"]
    assert p["kind"] == "production" and p["primary_hard_status"] == "INFEASIBLE"


def test_production_log_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("AIDE_SHADOW_DIAGNOSIS", raising=False)
    assert log_production_status([], {}, 2026, 8, "FEASIBLE") is None
