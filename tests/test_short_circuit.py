"""Short-circuit 엄격 조건 + 분리 플래그 + typed UNKNOWN (피드백 point4·6·8).

short-circuit(운영 solver 생략)은 절대 느슨하게 켜지지 않는다: 플래그·인증·범위·cert종류·버전·
canary 를 모두 만족할 때만. fail-open: 조건 미달이면 (False, 사유).
"""

from __future__ import annotations

from types import SimpleNamespace

from services.ontology_graph.short_circuit import (
    can_short_circuit,
    classify_graph_unknown,
)


def _cert(kind):
    return SimpleNamespace(kind=kind)


_NU = [{"nurse_id": f"n{i}"} for i in range(4)]
_CFG = {"not_one_night": True, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
        "initial_constraints": {"forbidden": {}, "forced_off": {}}}


def test_flag_off_never_short_circuits(monkeypatch):
    monkeypatch.delenv("AIDE_GRAPH_SHORT_CIRCUIT", raising=False)
    ok, why = can_short_circuit("INFEASIBLE_CERTIFIED", _cert("recovery_off_starvation"), _NU, _CFG)
    assert ok is False and why == "flag_off"


def test_all_conditions_required(monkeypatch):
    monkeypatch.setenv("AIDE_GRAPH_SHORT_CIRCUIT", "1")
    monkeypatch.setenv("AIDE_SHORT_CIRCUIT_CANARY_PCT", "100")
    # FEASIBLE/UNKNOWN 은 절대 생략 안 함
    assert can_short_circuit("FEASIBLE_WITNESS", _cert("x"), _NU, _CFG)[1] == "not_certified"
    assert can_short_circuit("UNKNOWN", None, _NU, _CFG)[1] == "not_certified"
    # 허용 안 된 cert 종류
    assert can_short_circuit("INFEASIBLE_CERTIFIED", _cert("joint_sequencing_collapse"),
                             _NU, _CFG)[1] == "cert_type_not_allowed"
    # 미지원 제약 있으면 생략 금지
    nu2 = [{"nurse_id": "n0", "n_exact": 13}, {"nurse_id": "n1"}]
    assert can_short_circuit("INFEASIBLE_CERTIFIED", _cert("recovery_off_starvation"),
                             nu2, _CFG)[1] == "out_of_scope"
    # 버전 불일치
    assert can_short_circuit("INFEASIBLE_CERTIFIED", _cert("recovery_off_starvation"),
                             _NU, _CFG, graph_version="9.9.9")[1] == "version_mismatch"
    # 전부 만족 → ok
    ok, why = can_short_circuit("INFEASIBLE_CERTIFIED", _cert("recovery_off_starvation"),
                                _NU, _CFG, request_id="r1")
    assert ok is True and why == "ok"


def test_canary_excludes_by_default(monkeypatch):
    monkeypatch.setenv("AIDE_GRAPH_SHORT_CIRCUIT", "1")
    monkeypatch.delenv("AIDE_SHORT_CIRCUIT_CANARY_PCT", raising=False)  # 0%
    ok, why = can_short_circuit("INFEASIBLE_CERTIFIED", _cert("recovery_off_starvation"),
                                _NU, _CFG, request_id="r1")
    assert ok is False and why == "canary_excluded"


def test_typed_unknown(monkeypatch):
    # 미지원 제약 → SCOPE (재귀 hybrid 무관)
    nu2 = [{"nurse_id": "n0", "off_days": 9}]
    assert classify_graph_unknown([{"nurse_id": "n0"}], {"off_days": 9}) == "UNKNOWN_SCOPE"
    # 지원 범위인데 UNKNOWN → WIDTH (재귀 hybrid 대상)
    assert classify_graph_unknown(_NU, _CFG) == "UNKNOWN_WIDTH"
    assert classify_graph_unknown(_NU, _CFG, engine_reason="timeout") == "UNKNOWN_TIMEOUT"
