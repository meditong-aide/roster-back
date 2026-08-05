"""Feasibility verifier 인터페이스 — 연구 코드(그래프)와 운영 solver 를 분리.

피드백: repair·pipeline 이 compile_cpsat() 을 **직접** 호출하면 "근사 shadow 결과를
solver_verified 라 부르는" 오류가 난다. 대신 Protocol 을 두고 구현체를 주입한다.

  ShadowIRVerifier        : IR→CP-SAT(compile_cpsat). **근사**(3연속-회복 인코딩) — solver_verified
                            라 부르면 안 됨. shadow 결과일 뿐.
  ProductionCpSatVerifier : 운영 전-제약 CP-SAT. **진짜 solver_verified**. (배선 시 구현.)
  BruteForceVerifier      : 소형 exact oracle(tools). 검증용.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class VerificationResult:
    feasible: bool | None       # True / False / None(판정불가/미지원)
    backend: str
    exact: bool                 # 이 backend 가 지원범위에서 exact 한가(shadow=False)


@runtime_checkable
class FeasibilityVerifier(Protocol):
    def check(self, nurses: list, config: dict, num_days: int) -> VerificationResult:
        ...


class ShadowIRVerifier:
    """IR→CP-SAT shadow. 근사(exact=False) — solver_verified 로 승격 금지."""

    backend = "shadow_ir_cpsat"

    def check(self, nurses: list, config: dict, num_days: int) -> VerificationResult:
        from services.ontology_graph.roster_ir import compile_cpsat, parse_to_ir
        cp = compile_cpsat(parse_to_ir(nurses, config, num_days))
        return VerificationResult(cp, self.backend, exact=False)


class ProductionCpSatVerifier:
    """운영 전-제약 CP-SAT 어댑터. 배선 시 check() 구현 → 진짜 solver_verified."""

    backend = "production_cpsat"

    def __init__(self, solve_fn=None):
        self._solve = solve_fn      # (nurses, config, num_days) -> bool|None

    def check(self, nurses: list, config: dict, num_days: int) -> VerificationResult:
        if self._solve is None:
            raise NotImplementedError("운영 CP-SAT 어댑터 미배선 — solve_fn 주입 필요")
        return VerificationResult(self._solve(nurses, config, num_days), self.backend, exact=True)
