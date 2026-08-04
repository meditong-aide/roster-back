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
    """graph-only 로그(production 비교는 분석기가 join). 실행단위 키·stage·버전 기록."""
    monkeypatch.setenv("AIDE_SHADOW_DIAGNOSIS", "1")
    rec = run_shadow(_nu(5), _CFG, 2026, 8, request_id="r1")
    assert rec is not None
    assert rec["graph_status"] == "INFEASIBLE_CERTIFIED"
    assert "core_days" in rec                        # 범위 축소 데이터
    # 실행 단위 join 키 + stage(피드백 fix1·2)
    assert rec["request_id"] == "r1" and rec["attempt_id"] == "primary_hard"
    assert rec["graph_model_stage"] == "primary_hard"
    # 버전·해시·플래그(fix5/point5)
    assert rec["graph_version"] and rec["ir_version"] and rec["input_hash"] != "?"
    assert "flags" in rec


def test_sampling_gate(monkeypatch):
    """샘플링(운영 지연 방지): 0% 면 request_id 있는 실행은 건너뛴다."""
    monkeypatch.setenv("AIDE_SHADOW_DIAGNOSIS", "1")
    monkeypatch.setenv("AIDE_SHADOW_SAMPLE_PCT", "0")
    assert run_shadow(_nu(5), _CFG, 2026, 8, request_id="r1") is None
    monkeypatch.setenv("AIDE_SHADOW_SAMPLE_PCT", "100")
    assert run_shadow(_nu(5), _CFG, 2026, 8, request_id="r1") is not None


def test_never_raises_on_bad_input(monkeypatch):
    monkeypatch.setenv("AIDE_SHADOW_DIAGNOSIS", "1")
    # 깨진 config/nurses 여도 예외 전파 없이 dict/None
    assert run_shadow(None, {}, 2026, 8) is not None or True
    assert run_shadow(_nu(3), {"daily_shift_requirements": {}}, 0, 0) is None  # month 0 → None
