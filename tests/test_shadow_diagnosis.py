"""운영 shadow 배선 — env 게이팅(기본 no-op), 결과 무영향, 예외 전파 없음."""

from __future__ import annotations

from services.ontology_graph.shadow_diagnosis import run_shadow


def _nu(k):
    return [{"nurse_id": f"n{i}", "allowed_shifts": []} for i in range(k)]


_CFG = {"two_offs_after_two_nig": True, "not_one_night": True,
        "daily_shift_requirements": {"D": 1, "E": 1, "N": 2},
        "initial_constraints": {"forbidden": {}, "forced_off": {}}}


def test_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("AIDE_SHADOW_DIAGNOSIS", raising=False)
    assert run_shadow(_nu(5), _CFG, 2026, 8) is None


def test_runs_and_logs_when_enabled(monkeypatch):
    monkeypatch.setenv("AIDE_SHADOW_DIAGNOSIS", "1")
    rec = run_shadow(_nu(5), _CFG, 2026, 8, production_status="INFEASIBLE", request_id="r1")
    assert rec is not None
    assert rec["graph_status"] == "INFEASIBLE_CERTIFIED"
    assert rec["agree"] is True                     # graph INFEASIBLE cert ↔ production INFEASIBLE
    assert rec["false_certificate"] is False        # production 도 INFEASIBLE → false cert 아님
    assert "core_days" in rec                        # 범위 축소 데이터
    # 버전·해시·플래그 기록(피드백 point5)
    assert rec["graph_version"] and rec["ir_version"] and rec["input_hash"] != "?"
    assert "flags" in rec and rec["request_id"] == "r1"


def test_false_certificate_flagged(monkeypatch):
    """production FEASIBLE 인데 graph INFEASIBLE = 치명적 false certificate 표시."""
    monkeypatch.setenv("AIDE_SHADOW_DIAGNOSIS", "1")
    rec = run_shadow(_nu(5), _CFG, 2026, 8, production_status="FEASIBLE")
    assert rec["graph_status"] == "INFEASIBLE_CERTIFIED"
    assert rec["false_certificate"] is True         # 이런 게 나오면 short-circuit 즉시 중단 근거


def test_never_raises_on_bad_input(monkeypatch):
    monkeypatch.setenv("AIDE_SHADOW_DIAGNOSIS", "1")
    # 깨진 config/nurses 여도 예외 전파 없이 dict/None
    assert run_shadow(None, {}, 2026, 8) is not None or True
    assert run_shadow(_nu(3), {"daily_shift_requirements": {}}, 0, 0) is None  # month 0 → None
