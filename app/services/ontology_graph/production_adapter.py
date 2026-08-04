"""Production CP-SAT adapter — 운영 solve 결과를 표준 상태로, verifier 로 노출.

피드백 point1: 운영 CP-SAT 결과를 FEASIBLE/INFEASIBLE/TIMEOUT/ERROR 로 표준화해야 shadow 가
production↔graph 를 비교할 수 있다(그 전엔 graph-only). 또 repair 재검증도 이 adapter 로
운영 solver 를 호출한다.

standardize_status(roster_system, generated): 운영 엔진 산출을 표준 상태로.
make_production_verifier(solve_fn): FeasibilityVerifier(exact=True) — repair solver_verified 용.
"""

from __future__ import annotations

from services.ontology_graph.verifier import ProductionCpSatVerifier, VerificationResult


def standardize_status(roster_system, generated, *, error: bool = False,
                       timeout: bool = False) -> str:
    """운영 solve 산출 → FEASIBLE / INFEASIBLE / TIMEOUT / ERROR (이분법 금지)."""
    if error or roster_system is None:
        return "ERROR"
    if timeout:
        return "TIMEOUT"
    # 엔진이 실근무를 한 건도 못 배정한 신호(운영 코드의 실제 플래그)
    if bool(getattr(roster_system, "_infeasible_empty", False)):
        return "INFEASIBLE"
    if isinstance(generated, dict):
        if not generated or not any(generated.values()):
            return "INFEASIBLE"
    elif not generated:
        return "INFEASIBLE"
    return "FEASIBLE"


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
