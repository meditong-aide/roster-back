"""MCS (Minimal Correction Set) — MUS 의 쌍대. '무엇을 풀면 되는지' 직접 산출.

MUS 는 '동시에 불가능한 하드제약 집합'(원인 후보)을 주지만 비유일·임의·인스턴스
분산이라 부정확하다. MCS 는 그 반대 — **제거(완화)하면 feasible 해지는 최소 제약
집합**을 준다. 즉 복구 액션 그 자체이고, 마지막에 **재실행으로 feasible 을 직접
확인(verified resolution, 연구 §6)** 하므로 정확하다.

알고리즘 (assumption 기반, ortools SufficientAssumptionsForInfeasibility 활용):
  GROW : 전부 강제 → infeasible 이면 MUS 한 개 추출 → 그중 (최소 cost) 하나를
         완화집합으로 이동 → feasible 될 때까지 반복.
  SHRINK: 완화집합의 각 원소를 다시 강제해보고, 그래도 feasible 이면 불필요 →
         완화집합에서 제거. 결과는 irreducible MCS.
cost 를 주면 '최소 비용 완화'(=최소변경)에 가깝게 고른다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ortools.sat.python import cp_model


@dataclass
class MCSResult:
    relaxed: list[str]                       # 완화할 제약 name (이걸 풀면 feasible)
    relaxed_meta: list[dict[str, Any]]       # 각 제약의 meta(node_id/family/label…)
    verified_feasible: bool                  # 완화 적용 후 재실행 feasible 인가
    iterations: int
    note: str = ""
    total_cost: float = field(default=0.0)


def find_mcs(
    model: cp_model.CpModel,
    registry,
    *,
    cost: Callable[[str], float] | None = None,
    time_limit: float = 10.0,
    max_iters: int = 200,
) -> MCSResult | None:
    """모델의 한 MCS 를 찾는다. None = assumption 과 무관하게 infeasible(원인 미wrap)."""
    names = list(registry._by_name.keys())
    if not names:
        return None
    _cost = cost or (lambda _n: 1.0)

    def _solve(relaxed: set[str]) -> int:
        forced = [registry._by_name[n].lit for n in names if n not in relaxed]
        model.ClearAssumptions()
        if forced:
            model.AddAssumptions(forced)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = time_limit
        solver.parameters.num_search_workers = 8
        return solver.Solve(model), solver

    relaxed: set[str] = set()
    iters = 0
    # ── GROW ──────────────────────────────────────────────────────────────
    while iters < max_iters:
        iters += 1
        status, solver = _solve(relaxed)
        if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            break
        if status != cp_model.INFEASIBLE:
            return MCSResult(relaxed=sorted(relaxed), relaxed_meta=_meta(registry, relaxed),
                             verified_feasible=False, iterations=iters,
                             note=f"solver status={solver.StatusName(status)} (timeout/unknown)")
        idx = list(solver.SufficientAssumptionsForInfeasibility())
        mus = [registry._by_index[i].name for i in idx
               if i in registry._by_index and registry._by_index[i].name not in relaxed]
        if not mus:
            # assumption 밖에서 infeasible — 원인이 wrap 안 됨(MCS 불가)
            return None
        pick = min(mus, key=_cost)
        relaxed.add(pick)
    else:
        return MCSResult(relaxed=sorted(relaxed), relaxed_meta=_meta(registry, relaxed),
                         verified_feasible=False, iterations=iters, note="max_iters 도달")

    # ── SHRINK (irreducible 화) ───────────────────────────────────────────
    for n in sorted(relaxed, key=_cost, reverse=True):  # 비싼 것부터 제거 시도
        trial = relaxed - {n}
        status, _ = _solve(trial)
        if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            relaxed = trial   # n 없이도 feasible → 불필요

    # ── VERIFY (재실행 feasible 확인) ─────────────────────────────────────
    status, _ = _solve(relaxed)
    verified = status in (cp_model.FEASIBLE, cp_model.OPTIMAL)
    return MCSResult(
        relaxed=sorted(relaxed), relaxed_meta=_meta(registry, relaxed),
        verified_feasible=verified, iterations=iters,
        total_cost=sum(_cost(n) for n in relaxed),
        note="verified" if verified else "shrink 후 재검증 실패")


def _meta(registry, relaxed: set[str]) -> list[dict[str, Any]]:
    out = []
    for n in sorted(relaxed):
        a = registry._by_name.get(n)
        md = dict(getattr(a, "meta", {}) or {}) if a else {}
        out.append({"name": n, "node_id": md.get("node_id"), "pattern": md.get("pattern"),
                    "type": md.get("type"), "label": md.get("label"),
                    "resolution_hint": md.get("resolution_hint")})
    return out
