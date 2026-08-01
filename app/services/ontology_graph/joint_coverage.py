"""다인 N-커버리지 결합 판정 — 조인트 frontier DP (bounded-treewidth 정확추론).

"각자는 되는데 셋이 같이는 N 커버 못 함" — 단일 상태DAG(per-nurse)로는 못 보는 결합.
nurse×day 격자에서 결합은 **일별 N 커버리지 한 줄**뿐 → treewidth ≈ k(N 가능 인원).
k 가 작으면 조인트 상태 frontier 를 날짜순으로 전개해 **정확 판정**. 크면 inconclusive
(→ 솔버에 위임). 각 간호사는 하루 {N, notN}. notN(=OFF 또는 D/E) 은 회복을 **관대하게**
충족한다고 모델 → 이 DP 가 infeasible 이라 하면 **진짜 infeasible**(sound, 오탐 없음).
post-solve 진단이라 관대 모델의 false-feasible 은 무해(다른 진단으로 흐름).

핵심 API:
  joint_night_coverage_feasible(nurses, config, num_days) -> True | False | None(inconclusive)
  detect_joint_night_infeasible(nurses, config, num_days) -> dict | None (병목 간호사 포함)
"""

from __future__ import annotations

from itertools import product
from typing import Any

from services.ontology_graph.lagrangian import _night_rules, _nurse_attr

_MAX_POOL = 7          # N 가능 인원이 이보다 많으면 정확판정 포기(폭발) → 솔버 위임
_FRONTIER_CAP = 300_000


def _demand_n(config: dict, day: int) -> int:
    dsr = config.get("daily_shift_requirements") or {}
    if isinstance(dsr, dict) and dsr:
        return int(dsr.get("N", 0) or 0)
    return int(config.get("nig_req", 0) or 0)


def _n_pool(nurses: list, config: dict) -> list[dict]:
    """N 을 설 수 있는 간호사 + 그들의 일별 강제(N/notN) 집합."""
    ic = config.get("initial_constraints") or {}
    fo = ic.get("forced_off") or {}
    fb = ic.get("forbidden") or {}
    pool: list[dict] = []
    for nu in nurses:
        nid = str(_nurse_attr(nu, "nurse_id"))
        allowed = [str(x).strip().upper() for x in (_nurse_attr(nu, "allowed_shifts") or [])]
        work = (set(allowed) & {"D", "E", "N"}) if allowed else {"D", "E", "N"}
        if "N" not in work:
            continue                                   # N 못 섬 → 공급 아님
        day_map = fb.get(nid) or {}
        must_n, cant_n = set(), set()
        for d, codes in day_map.items():
            up = {str(c).strip().upper() for c in (codes or [])}
            if "O" in up and "N" not in up:            # OFF 금지인데 N 가능 → 강제 N
                must_n.add(int(d))
            if "N" in up:                              # N 금지 → notN 강제
                cant_n.add(int(d))
        forced_off = {int(d) for d in (fo.get(nid) or [])}
        pool.append({"nurse_id": nid, "name": _nurse_attr(nu, "name") or nid,
                     "must_n": must_n, "must_notn": (forced_off | cant_n),
                     "n_only": work == {"N"}})
    return pool


def _feasible_over(pool: list[dict], config: dict, num_days: int) -> bool | None:
    """주어진 N-pool 로 일별 N 수요를 충족하는 조인트 배열 존재? True/False/None(폭발)."""
    max_run, rec_trig, min_run = _night_rules(config)
    demand = [_demand_n(config, d) for d in range(num_days)]
    if max(demand, default=0) == 0:
        return True
    k = len(pool)
    frontier: set[tuple] = {tuple((0, 0) for _ in range(k))}
    for d in range(num_days):
        need = demand[d]
        # 간호사별 (현 상태 무관) 강제
        mustn = [d in p["must_n"] for p in pool]
        musto = [d in p["must_notn"] for p in pool]
        nxt: set[tuple] = set()
        for js in frontier:
            per: list[list[tuple]] = []               # per[i] = [(new_state, is_N), ...]
            dead = False
            for i, (r, kk) in enumerate(js):
                ch: list[tuple] = []
                if not musto[i] and kk == 0 and r + 1 <= max_run:
                    ch.append(((r + 1, 0), 1))                       # N
                if not mustn[i]:
                    if r == 0:
                        ch.append(((0, max(0, kk - 1)), 0))          # notN(회복소진)
                    elif r >= min_run:
                        ch.append(((0, 1 if r >= rec_trig else 0), 0))  # 런 종료
                if not ch:
                    dead = True
                    break
                per.append(ch)
            if dead:
                continue
            for combo in product(*per):
                if sum(c[1] for c in combo) >= need:              # 커버리지 충족
                    nxt.add(tuple(c[0] for c in combo))
                    if len(nxt) > _FRONTIER_CAP:
                        return None                                # 폭발 → inconclusive
        if not nxt:
            return False
        frontier = nxt
    return True


def joint_night_coverage_feasible(nurses: list, config: dict, num_days: int):
    """다인 N-커버리지 실현가능성. True/False/None(N-pool 없음·과대 → 판정 유보)."""
    pool = _n_pool(nurses, config)
    if not pool or len(pool) > _MAX_POOL:
        return None
    return _feasible_over(pool, config, num_days)


def detect_joint_night_infeasible(nurses: list, config: dict, num_days: int) -> dict | None:
    """N-pool 결합이 infeasible 이면 병목(제거 시 feasible 되는 간호사)까지 격리해 반환.

    반환 None = feasible 또는 판정유보. dict = {culprits, pool_ids, demand_n, ...}.
    """
    pool = _n_pool(nurses, config)
    if not pool or len(pool) > _MAX_POOL:
        return None
    if _feasible_over(pool, config, num_days) is not False:
        return None                                   # feasible or inconclusive
    # 병목 격리: 한 명 제외(그 간호사 강제 해제)했을 때 feasible 로 바뀌는 간호사 = culprit.
    culprits = []
    for i in range(len(pool)):
        relaxed = [dict(p) for j, p in enumerate(pool) if j != i]
        # 남은 pool 이 1명 부족해도 수요 충족 가능한지(즉 그 1명이 병목인지)
        if _feasible_over(relaxed + [dict(pool[i], must_n=set(), must_notn=set())],
                          config, num_days) is True:
            culprits.append({"nurse_id": pool[i]["nurse_id"], "name": pool[i]["name"],
                             "n_only": pool[i]["n_only"]})
    return {
        "classification": "coverage_shortage", "top_family": "night_coverage",
        "pool_ids": [p["nurse_id"] for p in pool],
        "demand_n": _demand_n(config, 0),
        "culprits": culprits or [{"nurse_id": p["nurse_id"], "name": p["name"],
                                  "n_only": p["n_only"]} for p in pool],
    }
