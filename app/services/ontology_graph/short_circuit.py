"""Graph short-circuit 게이팅 — 엄격 조건 + 분리 플래그 + fail-open (피드백 point 4·8).

**절대 그냥 켜지 않는다.** short-circuit(운영 CP-SAT 생략)은 아래를 **모두** 만족할 때만,
그것도 단계적(canary)·kill-switch·fail-open 하에서만 허용한다.

분리 플래그(하나로 묶지 말 것):
  AIDE_SHADOW_DIAGNOSIS  — shadow 로깅(무영향)
  AIDE_GRAPH_SHORT_CIRCUIT — INFEASIBLE 시 solver 생략(위험, 조건부)
  AIDE_REPAIR_VERIFY     — repair 를 solver 로 재검증(비용)

fail-open 원칙: can_short_circuit 이 False/의심이면 **항상 production solver 실행**.
"""

from __future__ import annotations

import os

GRAPH_ENGINE_VERSION = "0.1.0"
IR_SCHEMA_VERSION = "0.1.0"

# short-circuit 허용 certificate 종류 — **산술적으로 확실히 국소적인 것만**.
# 제외: joint_sequencing_collapse·joint_frontier_empty·conditioning_infeasible(덜 국소적,
#       설명 정확성 미검증) → 이들은 shadow/설명용이지 solver 생략 근거로 쓰지 않는다.
ALLOWED_SHORT_CIRCUIT_CERTS = frozenset({
    "empty_domain",                # 강제근무+강제OFF 같은 칸 충돌(자명)
    "sequence_path_empty",         # 개인 automaton 경로 소멸(exact)
    "recovery_off_starvation",     # 회복 OFF 잠식(정량)
    "night_coverage_deficit",      # 일별 야간 자격 부족(Hall)
    "night_supply_deficit",        # 월 야간 공급 부족(집계)
    "coverage_deficit",            # max-flow 부족(증명된 하한)
    "forced_coverage_deficit",     # 전원 강제로 커버리지 미달
})


def shadow_enabled() -> bool:
    return os.environ.get("AIDE_SHADOW_DIAGNOSIS") == "1"


def short_circuit_flag() -> bool:
    return os.environ.get("AIDE_GRAPH_SHORT_CIRCUIT") == "1"


def repair_verify_flag() -> bool:
    return os.environ.get("AIDE_REPAIR_VERIFY") == "1"


def canary_pass(request_id: str | None) -> bool:
    """AIDE_SHORT_CIRCUIT_CANARY_PCT(0~100) 로 단계적 활성(request_id 해시 기반, 결정적)."""
    try:
        pct = int(os.environ.get("AIDE_SHORT_CIRCUIT_CANARY_PCT", "0"))
    except ValueError:
        pct = 0
    if pct >= 100:
        return True
    if pct <= 0 or not request_id:
        return False
    h = 0
    for ch in str(request_id):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return (h % 100) < pct


def can_short_circuit(graph_status: str, certificate, nurses: list, config: dict,
                      *, request_id: str | None = None,
                      graph_version: str = GRAPH_ENGINE_VERSION,
                      ir_version: str = IR_SCHEMA_VERSION) -> tuple[bool, str]:
    """운영 CP-SAT 생략 허용? (허용여부, 사유). fail-open: False 면 반드시 solver 실행."""
    if not short_circuit_flag():
        return False, "flag_off"
    if graph_status != "INFEASIBLE_CERTIFIED":
        return False, "not_certified"                # FEASIBLE/UNKNOWN 은 절대 생략 안 함
    from services.ontology_graph.scope_manifest import unmodeled_active
    if unmodeled_active(nurses, config):
        return False, "out_of_scope"                 # 미지원 제약 있으면 생략 금지
    if certificate is None or certificate.kind not in ALLOWED_SHORT_CIRCUIT_CERTS:
        return False, "cert_type_not_allowed"
    if graph_version != GRAPH_ENGINE_VERSION or ir_version != IR_SCHEMA_VERSION:
        return False, "version_mismatch"
    if not canary_pass(request_id):
        return False, "canary_excluded"
    return True, "ok"


def classify_graph_unknown(nurses: list, config: dict, engine_reason: str = "") -> str:
    """UNKNOWN 을 이분법이 아니라 사유별로(피드백 point 6). 재귀 hybrid 필요성 판단에 필수."""
    from services.ontology_graph.scope_manifest import unmodeled_active
    if unmodeled_active(nurses, config):
        return "UNKNOWN_SCOPE"                        # 미지원 제약 → 재귀 hybrid 무관
    if engine_reason == "timeout":
        return "UNKNOWN_TIMEOUT"
    return "UNKNOWN_WIDTH"                            # frontier 폭발 → 재귀 hybrid 대상
