"""Solver-guided 파이프라인 — graph precheck → CP-SAT fallback (포지셔닝의 실코드).

피드백 커밋5: 그래프 계층을 **두 번째 솔버가 아니라 solver 의 presolve/설명 계층**으로 배선.
graph 가 짧은 budget 안에 certificate 를 내면 CP-SAT 실행을 **생략**하고, 못 내면(UNKNOWN)
CP-SAT 로 이관. "중복 계산"이 아니라 "빠른 사례는 solver 생략, 어려운 사례만 solver".

  graph = solve_hybrid(짧은 budget)
  ├ INFEASIBLE_CERTIFIED → certificate 반환, CP-SAT 생략   (graph 가속)
  ├ FEASIBLE_WITNESS     → feasible 반환, CP-SAT 생략       (지원범위 확정)
  └ UNKNOWN(폭 초과/미지원) → CP-SAT fallback

주의: 여기 CP-SAT 은 IR shadow compiler(compile_cpsat, 지원 제약만). 실서비스에선 이 fallback 이
**운영 CP-SAT(전 제약)**이어야 미지원 제약까지 맞다. in-scope 벤치마크에선 compile_cpsat 가
전 모델과 동치라 유효.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.ontology_graph.certificate import Certificate


@dataclass
class PipelineResult:
    status: str                 # FEASIBLE / INFEASIBLE / UNKNOWN
    via: str                    # graph / cpsat
    certificate: Certificate | None = None
    cpsat_skipped: bool = False


def diagnose_pipeline(nurses: list, config: dict, num_days: int,
                      graph_budget: int = 2_000_000, cpsat_time: float = 5.0) -> PipelineResult:
    """graph precheck 후 필요할 때만 CP-SAT."""
    from services.ontology_graph.hybrid_solver import solve_hybrid
    gr = solve_hybrid(nurses, config, num_days, budget=graph_budget)
    if gr.status == "INFEASIBLE_CERTIFIED":
        return PipelineResult("INFEASIBLE", "graph", certificate=gr.certificate, cpsat_skipped=True)
    if gr.status == "FEASIBLE_WITNESS":
        return PipelineResult("FEASIBLE", "graph", cpsat_skipped=True)
    # UNKNOWN → CP-SAT fallback
    from services.ontology_graph.roster_ir import compile_cpsat, parse_to_ir
    cp = compile_cpsat(parse_to_ir(nurses, config, num_days), time_limit=cpsat_time)
    status = {True: "FEASIBLE", False: "INFEASIBLE", None: "UNKNOWN"}[cp]
    return PipelineResult(status, "cpsat", cpsat_skipped=False)
