"""Production CP-SAT adapter — 운영 solve 결과를 표준 상태로, verifier 로 노출.

피드백 point1: 운영 CP-SAT 결과를 FEASIBLE/INFEASIBLE/TIMEOUT/ERROR 로 표준화해야 shadow 가
production↔graph 를 비교할 수 있다(그 전엔 graph-only). 또 repair 재검증도 이 adapter 로
운영 solver 를 호출한다.

standardize_status(roster_system, generated): 운영 엔진 산출을 표준 상태로.
make_production_verifier(solve_fn): FeasibilityVerifier(exact=True) — repair solver_verified 용.
"""

from __future__ import annotations

from services.ontology_graph.verifier import ProductionCpSatVerifier, VerificationResult


# CP-SAT raw StatusName → 표준 상태(피드백 fix3: _infeasible_empty 추론 대신 실제 raw status).
_RAW_MAP = {
    "OPTIMAL": "FEASIBLE", "FEASIBLE": "FEASIBLE",
    "INFEASIBLE": "INFEASIBLE",
    "UNKNOWN": "TIMEOUT",              # 시간초과/미결정
    "MODEL_INVALID": "ERROR",
}


def standardize_status(roster_system, generated, *, raw_status: str | None = None,
                       error: bool = False, timeout: bool = False) -> tuple[str, str]:
    """운영 solve 산출 → (표준상태, 출처). 표준상태∈FEASIBLE/INFEASIBLE/TIMEOUT/ERROR.

    우선순위: 실제 CP-SAT raw_status(신뢰) > 명시 error/timeout > _infeasible_empty 추론(약함).
    출처="raw"|"flag"|"empty"|"inferred" — 분석 시 신뢰도 구분용(추론은 ground truth 아님).
    """
    if raw_status:
        s = str(raw_status).upper().split("(")[0].strip()   # "UNKNOWN(3)"→"UNKNOWN"
        if s in _RAW_MAP:
            return _RAW_MAP[s], "raw"
    if error or roster_system is None:
        return "ERROR", "flag"
    if timeout:
        return "TIMEOUT", "flag"
    if bool(getattr(roster_system, "_infeasible_empty", False)):
        return "INFEASIBLE", "flag"    # fallback 신호(약한 근거) — raw 없을 때만
    empty = (not generated) or (isinstance(generated, dict) and not any(generated.values()))
    if empty:
        return "INFEASIBLE", "empty"   # 빈 결과=INFEASIBLE 확정 아님(timeout/error 가능) → 약함
    return "FEASIBLE", "inferred"


def make_production_verifier(solve_fn) -> ProductionCpSatVerifier:
    """운영 feasibility 콜(solve_fn: (nurses, config, num_days)->bool|None) → exact verifier.

    repair.verify_repairs(verifier=make_production_verifier(...)) 로 solver_verified 부여.
    solve_fn 은 운영 CP-SAT 을 feasibility-only 로 돌리는 클로저(db/current_user 등 캡처).
    """
    return ProductionCpSatVerifier(solve_fn)


def status_to_verification(status: str) -> VerificationResult:
    """표준 상태 → VerificationResult(exact=True, 운영 solver 기준)."""
    feasible = {"FEASIBLE": True, "INFEASIBLE": False}.get(status)   # TIMEOUT/ERROR → None
    return VerificationResult(feasible, "production_cpsat", exact=True)
