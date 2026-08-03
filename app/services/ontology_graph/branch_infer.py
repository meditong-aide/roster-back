"""Surplus-Certified Branch-and-Infer (N축 PoC).

우리 완전판정기(개인 상태DAG + N-pool frontier DP)를 leaf 로 쓰고, 즉시 판정되지 않는
정수 결합만 **1레벨 분기**해 양쪽 실패 certificate 를 proof tree 로 병합한다.
IIS 없이 sound·행동가능한 원인 설명 + 검증된 복구 후보.

계층:
  Tier2 개인 automaton  → sequence_path_empty
  Tier3 N-pool DP(정확) → night_supply_deficit(집계) / 결합 판정
  Tier4 정수 분기       → "어느 배정이든 실패"의 proof tree
  Tier7 검증 복구       → antecedent 완화 → N축 재판정으로 검증
솔버 없음(N축 자기완결). 상태 3개(certificate.py) — UNKNOWN 을 FEASIBLE 로 취급 금지.
"""

from __future__ import annotations

import copy
from typing import Any

from services.ontology_graph.certificate import (
    FEASIBLE,
    INFEASIBLE,
    UNKNOWN,
    Certificate,
    ProofNode,
)
from services.ontology_graph.joint_coverage import (
    _MAX_POOL,
    _demand_n,
    _feasible_over,
    _n_pool,
)
from services.ontology_graph.lagrangian import _night_rules, per_nurse_night_feasible


def _max_nights(must_n: set[int], must_notn: set[int], num_days: int, config: dict) -> int:
    """한 간호사가 야간규칙 하에서 낼 수 있는 **최대 N 일수**(공급 상한). DP."""
    max_run, rec_trig, min_run = _night_rules(config)
    best: dict[tuple[int, int], int] = {(0, 0): 0}     # (r,k) -> max N so far
    for d in range(num_days):
        mn, mo = d in must_n, d in must_notn
        nxt: dict[tuple[int, int], int] = {}
        for (r, k), cnt in best.items():
            if not mo and k == 0 and r + 1 <= max_run:            # N
                s = (r + 1, 0)
                nxt[s] = max(nxt.get(s, -1), cnt + 1)
            if not mn:                                            # notN
                if r == 0:
                    s = (0, max(0, k - 1))
                    nxt[s] = max(nxt.get(s, -1), cnt)
                elif r >= min_run:
                    s = (0, 1 if r >= rec_trig else 0)
                    nxt[s] = max(nxt.get(s, -1), cnt)
        if not nxt:
            return -1                                             # 개인 배열 자체 불가
        best = nxt
    return max(best.values())


def _seq_cert(m: dict, num_days: int, config: dict) -> Certificate | None:
    """개인 automaton 경로 소멸 → sequence_path_empty certificate(antecedent 포함)."""
    if per_nurse_night_feasible(m["must_n"], m["must_notn"], num_days, config):
        return None
    ante: list[str] = []
    if m["must_n"]:
        ds = ", ".join(str(d + 1) for d in sorted(m["must_n"])[:6])
        ante.append(f"{m['name']}: {ds}일 근무 강제(OFF 금지)")
    if m["must_notn"]:
        ds = ", ".join(str(d + 1) for d in sorted(m["must_notn"])[:6])
        ante.append(f"{m['name']}: {ds}일 야간 불가(강제 OFF/야간 금지)")
    rules = []
    if config.get("two_offs_after_three_nig"):
        rules.append("3연속 야간 후 2일 휴식")
    if config.get("not_one_night"):
        rules.append("고립 1야간 금지")
    if rules:
        ante.append("규칙: " + " · ".join(rules))
    return Certificate(kind="sequence_path_empty",
                       group_id=f"nurse:{m['nurse_id']}", capacity=0, demand=1, deficit=1,
                       antecedents=ante,
                       witness={"nurse_id": m["nurse_id"], "name": m["name"],
                                "days": sorted(m["must_n"] | m["must_notn"])})


def _perday_cert(pool: list[dict], config: dict, num_days: int) -> Certificate | None:
    """어느 날 야간 **가능 인원 < 수요** → 증명된 커버리지 부족(자격 상한)."""
    for d in range(num_days):
        need = _demand_n(config, d)
        if need <= 0:
            continue
        elig = [m for m in pool if d not in m["must_notn"]]
        if len(elig) < need:
            return Certificate(kind="night_coverage_deficit",
                               group_id=f"night:day:{d + 1}", capacity=len(elig),
                               demand=need, deficit=need - len(elig),
                               antecedents=[f"{m['name']}: {d + 1}일 야간 불가"
                                            for m in pool if d in m["must_notn"]][:6],
                               witness={"day": d, "pool": [m["nurse_id"] for m in pool]})
    return None


def _aggregate_cert(pool: list[dict], config: dict, num_days: int) -> Certificate | None:
    """월 최대 야간 공급 합 < 총 수요 → 증명된 집계 부족."""
    supply = sum(max(0, _max_nights(m["must_n"], m["must_notn"], num_days, config)) for m in pool)
    demand = sum(_demand_n(config, d) for d in range(num_days))
    if demand > supply:
        return Certificate(kind="night_supply_deficit", group_id=f"night:pool:{len(pool)}",
                           capacity=supply, demand=demand, deficit=demand - supply,
                           antecedents=[f"야간 가능 {len(pool)}명, 각자 회복규칙상 매일은 불가"],
                           witness={"pool": [m["nurse_id"] for m in pool]})
    return None


def _local_reason(pool: list[dict], config: dict, num_days: int) -> Certificate | None:
    """(수정된) pool 의 infeasibility 를 값 certificate 로. 없으면 None."""
    return (_perday_cert(pool, config, num_days)
            or next((c for m in pool if (c := _seq_cert(m, num_days, config))), None)
            or _aggregate_cert(pool, config, num_days))


def _fix(pool: list[dict], idx: int, day: int, do_n: bool) -> list[dict]:
    """pool 복사본에 '간호사 idx 가 day 에 N/notN' 강제."""
    p = copy.deepcopy(pool)
    (p[idx]["must_n"] if do_n else p[idx]["must_notn"]).add(day)
    return p


def _bottleneck_day(pool: list[dict], config: dict, num_days: int) -> int | None:
    """수요 대비 여유가 가장 적은 날."""
    best, bd = None, None
    for d in range(num_days):
        need = _demand_n(config, d)
        if need <= 0:
            continue
        slack = sum(1 for m in pool if d not in m["must_notn"]) - need
        if best is None or slack < best:
            best, bd = slack, d
    return bd


def diagnose_night_axis(nurses: list, config: dict, num_days: int) -> ProofNode:
    """N축 진단 → ProofNode(INFEASIBLE_CERTIFIED / FEASIBLE / UNKNOWN)."""
    pool = _n_pool(nurses, config)
    if not pool or len(pool) > _MAX_POOL:
        return ProofNode(UNKNOWN)
    # Tier2: 개인 시퀀스
    for m in pool:
        c = _seq_cert(m, num_days, config)
        if c:
            return ProofNode(INFEASIBLE, certificate=c)
    # Tier3 집계 공급
    ac = _aggregate_cert(pool, config, num_days)
    if ac:
        return ProofNode(INFEASIBLE, certificate=ac)
    # Tier3 결합 정확 판정
    ok = _feasible_over(pool, config, num_days)
    if ok is True:
        return ProofNode(FEASIBLE)
    if ok is None:
        return ProofNode(UNKNOWN)
    # Tier4: 결합 infeasible → 병목 (nurse,day) 분기로 narrative
    d = _bottleneck_day(pool, config, num_days)
    if d is not None:
        for i, m in enumerate(pool):
            if d in m["must_notn"] or d in m["must_n"]:
                continue                                   # 이미 확정된 배정
            p0, p1 = _fix(pool, i, d, False), _fix(pool, i, d, True)
            f0 = _feasible_over(p0, config, num_days) is False
            f1 = _feasible_over(p1, config, num_days) is False
            if f0 and f1:
                r0 = _local_reason(p0, config, num_days)
                r1 = _local_reason(p1, config, num_days)
                return ProofNode(
                    INFEASIBLE, branch_literal=f"{m['name']} {d + 1}일 야간",
                    children=[ProofNode(INFEASIBLE, certificate=r0),
                              ProofNode(INFEASIBLE, certificate=r1)])
    # 분기로 깔끔한 이분이 안 나오면: 결합 부족 자체를 certificate 로
    return ProofNode(INFEASIBLE, certificate=Certificate(
        kind="joint_night_infeasible", group_id=f"night:pool:{len(pool)}",
        capacity=float("nan"), demand=_demand_n(config, 0), deficit=1,
        antecedents=[f"{m['name']}" for m in pool],
        witness={"pool": [m["nurse_id"] for m in pool]}))


# ── 검증된 복구 후보 (antecedent → 완화 → N축 재판정) ────────────────────────────

def _collect_certs(node: ProofNode) -> list[Certificate]:
    out = [node.certificate] if node.certificate else []
    for c in node.children:
        out.extend(_collect_certs(c))
    return out


def verified_repairs(node: ProofNode, nurses: list, config: dict, num_days: int,
                     max_try: int = 12) -> list[dict[str, Any]]:
    """leaf certificate 의 antecedent → 완화 후보 생성 → **N축 재판정으로 검증**.

    반환: [{action, target, verified}] — verified=True(그 완화로 N축 feasible/미인증)만 카드감.
    (실서비스는 실제 solver 로 최종검증; 여기선 도메인 재판정으로 저비용 후보 선별.)
    """
    certs = _collect_certs(node)
    cands: list[dict[str, Any]] = []
    seen = set()
    for c in certs:
        # 개인 시퀀스 충돌 → 그 간호사 금지근무 해제 / 근무유형 D·E 추가
        if c.kind == "sequence_path_empty":
            nid = c.witness.get("nurse_id")
            for act in (("banned_release", nid), ("allowed_add_de", nid)):
                if act not in seen:
                    seen.add(act); cands.append({"action": act[0], "nurse_id": nid})
        # 커버리지/공급 부족 → 야간 수요 -1 / 불가 간호사 야간 허용
        elif c.kind in ("night_coverage_deficit", "night_supply_deficit", "joint_night_infeasible"):
            if ("demand_down", None) not in seen:
                seen.add(("demand_down", None)); cands.append({"action": "reduce_night_demand"})
            for a in c.antecedents:
                if a not in seen and len(seen) < max_try:
                    seen.add(a)
    # 검증: 각 후보를 config/nurses 사본에 적용 후 N축 재판정
    out = []
    for cand in cands[:max_try]:
        cfg2, nrs2 = _apply_candidate(cand, nurses, config)
        if cfg2 is None:
            continue
        st = diagnose_night_axis(nrs2, cfg2, num_days).status
        cand["verified"] = (st != INFEASIBLE)     # 더는 N축 infeasible 아님 → 유효 후보
        out.append(cand)
    return out


def _apply_candidate(cand: dict, nurses: list, config: dict):
    """복구 후보를 config/nurses 사본에 적용. (N축 도메인만; 실 write 아님)"""
    cfg = copy.deepcopy(config)
    nrs = copy.deepcopy(nurses)
    ic = cfg.setdefault("initial_constraints", {})
    fb = ic.setdefault("forbidden", {})
    act = cand.get("action")
    nid = cand.get("nurse_id")
    if act == "banned_release" and nid is not None:
        fb.pop(str(nid), None)                         # 그 간호사 금지 전부 해제
        return cfg, nrs
    if act == "allowed_add_de" and nid is not None:
        for nu in nrs:
            if str(_g(nu, "nurse_id")) == str(nid):
                cur = set(_g(nu, "allowed_shifts") or []) | {"D", "E", "N"}
                _s(nu, "allowed_shifts", sorted(cur))
        return cfg, nrs
    if act == "reduce_night_demand":
        dsr = cfg.get("daily_shift_requirements")
        if isinstance(dsr, dict) and int(dsr.get("N", 0) or 0) > 0:
            dsr = dict(dsr); dsr["N"] = int(dsr["N"]) - 1
            cfg["daily_shift_requirements"] = dsr
        else:
            cfg["nig_req"] = max(0, int(cfg.get("nig_req", 0) or 0) - 1)
        return cfg, nrs
    return None, None


def _g(nu, k):
    return nu.get(k) if isinstance(nu, dict) else getattr(nu, k, None)


def _s(nu, k, v):
    if isinstance(nu, dict):
        nu[k] = v
    else:
        setattr(nu, k, v)
