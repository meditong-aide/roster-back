"""Separator 컴포넌트 분해 — 큰 결합을 작은 exact 조각으로 (미니 솔버 없이 bounded-width).

frontier DP 는 |frontier| 가 넓으면(긴 horizon·다인) UNKNOWN 이 된다. 이를 두 축으로 분해:

1. **대칭 축소**(frontier_dp.symmetry): 교환가능 간호사 상태를 multiset 로 접어 순열 폭발 제거.
2. **슬라이딩 window 투영**(여기): 짧은 window 로 시간축을 자른다. window 하위문제는
   - 모든 간호사 포함(가장 관대), fresh 진입상태(r=k=0, 회복빚 없음 = 가장 관대),
   - free exit(window 이후 제약 없음)
   이라 **전체 문제의 완화(relaxation)** 다. 따라서 **window infeasible ⟹ 전체 infeasible**
   (sound, 한쪽 방향). 어떤 window 도 infeasible 아니면 결론 유보(전체 feasible 증명 아님) → UNKNOWN.

이 조합으로 병목이 짧은 구간·소수 간호사에 국소화되면, 전역이 커도 exact 인증 가능.
반대방향(전체 feasible)까지 증명하려면 window 간 이음(경계상태 일치)이 필요 → 후속.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.ontology_graph.certificate import INFEASIBLE, UNKNOWN, Certificate
from services.ontology_graph.frontier_dp import _interchangeable, _prep, diagnose_frontier


@dataclass
class DecomposeResult:
    status: str
    certificate: Certificate | None = None
    window: tuple | None = None          # (a, b) 전역 일자 구간(붕괴 국소화)
    width_max: int = 0


def _window_config(config: dict, a: int, b: int) -> dict:
    """[a, b) 구간으로 banned/forced 를 잘라 0-기준 재색인한 하위 config(나머지 규칙 동일)."""
    cfg = dict(config)
    ic = config.get("initial_constraints") or {}
    fb, fo = {}, {}
    for nid, dm in (ic.get("forbidden") or {}).items():
        w = {int(d) - a: codes for d, codes in dm.items() if a <= int(d) < b}
        if w:
            fb[nid] = w
    for nid, days in (ic.get("forced_off") or {}).items():
        w = [int(d) - a for d in days if a <= int(d) < b]
        if w:
            fo[nid] = w
    cfg["initial_constraints"] = {"forbidden": fb, "forced_off": fo}
    return cfg


def windowed_certify(nurses: list, config: dict, num_days: int,
                     window: int = 8, stride: int = 4,
                     cap: int = 100_000) -> DecomposeResult:
    """슬라이딩 window 로 국소 붕괴를 sound 하게 인증(window infeasible ⟹ 전체 infeasible)."""
    prepped = _prep(nurses, config)
    best_width = 0

    def _pressure(a: int, b: int) -> int:
        """window 내 강제근무(banned-O)·강제OFF 밀도 — 병목일수록 큼."""
        p = 0
        for n in prepped:
            p += sum(1 for d in n["banned"] if a <= d < b)
            p += sum(1 for d in n["foff"] if a <= d < b)
        return p

    starts = list(range(0, max(1, num_days - window + 1), stride))
    starts.sort(key=lambda a: -_pressure(a, min(num_days, a + window)))  # 병목 밀집 window 먼저
    for a in starts:
        b = min(num_days, a + window)
        sub_cfg = _window_config(config, a, b)
        sym = _interchangeable(prepped, a, b)         # window 내 교환가능 → 대칭 축소
        r = diagnose_frontier(nurses, sub_cfg, b - a, cap=cap, symmetry=sym or None)
        best_width = max(best_width, r.width_max)
        if r.status == INFEASIBLE and r.certificate is not None:
            c = r.certificate
            gday = (c.witness.get("day", 0) or 0) + a         # 전역 일자로 보정
            cert = Certificate(kind=c.kind, group_id=f"day:{gday + 1}",
                               capacity=c.capacity, demand=c.demand, deficit=c.deficit,
                               antecedents=[s.replace(f"{(c.witness.get('day', 0) or 0) + 1}일",
                                                      f"{gday + 1}일") for s in c.antecedents],
                               witness={**c.witness, "day": gday, "window": [a, b]})
            return DecomposeResult(INFEASIBLE, certificate=cert, window=(a, b),
                                   width_max=best_width)
    return DecomposeResult(UNKNOWN, width_max=best_width)


def decompose_diagnose(nurses: list, config: dict, num_days: int) -> DecomposeResult:
    """전체 frontier DP 먼저; UNKNOWN(폭 초과)이면 window 분해로 국소 인증 재시도."""
    full = diagnose_frontier(nurses, config, num_days)
    if full.status == INFEASIBLE:
        return DecomposeResult(INFEASIBLE, certificate=full.certificate,
                               window=(0, num_days), width_max=full.width_max)
    if full.status == "FEASIBLE_WITNESS":
        return DecomposeResult(full.status, width_max=full.width_max)
    # UNKNOWN → 분해
    return windowed_certify(nurses, config, num_days)
