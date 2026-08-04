"""Solver-guided 파이프라인 — graph INFEASIBLE presolve → solver (포지셔닝의 실코드).

피드백 교정: 근무표 서비스는 feasibility 판정뿐 아니라 **실제 근무표 생성·최적화**가 필요하다.
따라서 graph 가 FEASIBLE 이라고 solver 를 생략하면 안 된다(배정 결과·공정성·선호 최적화·미지원
soft/hard 제약이 남음). **solver 생략은 sound 한 INFEASIBLE certificate 를 얻었을 때만.**

  graph INFEASIBLE_CERTIFIED → solver 생략, 원인 certificate 반환   (유일한 short-circuit)
  graph FEASIBLE_WITNESS     → solver 실행(생성·최적화). graph 는 signal/domain reduction/warm-start
  graph UNKNOWN              → solver 실행

주장 주의: "solver 생략률"은 **infeasible 사례 중 graph 가 INFEASIBLE 인증한 비율**로만 말해야
한다. FEASIBLE 은 생략 대상이 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.ontology_graph.certificate import Certificate
from services.ontology_graph.verifier import FeasibilityVerifier, ShadowIRVerifier


@dataclass
class PipelineResult:
    status: str                 # FEASIBLE / INFEASIBLE / UNKNOWN
    via: str                    # graph_certificate / solver
    certificate: Certificate | None = None
    solver_invoked: bool = False
    graph_signal: str = ""      # graph 판정(FEASIBLE_WITNESS/UNKNOWN) — solver 실행 시 signal


def diagnose_pipeline(nurses: list, config: dict, num_days: int,
                      graph_budget: int = 2_000_000,
                      verifier: FeasibilityVerifier | None = None) -> PipelineResult:
    """graph INFEASIBLE presolve 후, 그 외에는 항상 solver 실행(생성·최적화 위해)."""
    from services.ontology_graph.hybrid_solver import solve_hybrid
    gr = solve_hybrid(nurses, config, num_days, budget=graph_budget)
    if gr.status == "INFEASIBLE_CERTIFIED":
        # 유일한 sound short-circuit: 지원 subset infeasible ⟹ 전체 infeasible
        return PipelineResult("INFEASIBLE", "graph_certificate", certificate=gr.certificate,
                              solver_invoked=False)
    # FEASIBLE/UNKNOWN → solver 로 실제 판정·생성(graph 는 signal). production verifier 주입 권장.
    v = verifier or ShadowIRVerifier()
    res = v.check(nurses, config, num_days)
    status = {True: "FEASIBLE", False: "INFEASIBLE", None: "UNKNOWN"}[res.feasible]
    return PipelineResult(status, "solver", solver_invoked=True, graph_signal=gr.status)
