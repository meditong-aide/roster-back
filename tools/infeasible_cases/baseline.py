"""Baseline 비교 실험 — QuickXplain(IIS) vs Tier1(max-flow only) vs 우리 branch-infer.

같은 N축 부분문제에서 세 방법을 코퍼스에 돌려 정량 비교한다:
  · QuickXplain  : 제약을 하나씩 빼보며 최소 충돌집합(IIS)을 찾음. **oracle 호출 수**를 잰다.
                   (G-CSEA/QuickXplain 계열의 대표. oracle = N축 재판정)
  · Tier1(flow)  : 일별 자격/집계 공급 부족만 검사(시퀀스·결합 못 봄).
  · branch-infer : 직접 typed certificate + proof-tree(oracle ~1~수 회, 행동가능 설명).

측정: 원인 격리 성공 여부 · oracle 호출 수 · 산출 형태(제약집합 vs 행동 설명).

⚠️ 정직한 한계(피드백 반영): 지금 QuickXplain 의 oracle 이 **우리 진단기 자신**이라
   "우리를 1회 호출 vs 우리를 QuickXplain 안에서 N회 호출"의 구조다. 따라서 이 표는
   **내부 PoC 검증**(우리 진단기가 IIS 계열보다 적은 재판정으로 같은 결론)일 뿐,
   "IIS 대비 우수"라는 **연구 결론이 아니다**. 정식 비교엔 양쪽이 공유할 **독립 exact
   oracle**(CP-SAT feasibility 또는 독립 N-pool DP)이 필요하고, 지표도 호출수만이 아니라
   실행시간·실코퍼스(합성 아닌)여야 한다. 이는 TODO(baseline_v2).
"""

from __future__ import annotations

import copy
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "app"))

from services.ontology_graph.branch_infer import (  # noqa: E402
    _aggregate_cert, _n_pool, _perday_cert, diagnose_night_axis)
from services.ontology_graph.certificate import INFEASIBLE  # noqa: E402

_RULE_KEYS = ("two_offs_after_three_nig", "two_offs_after_two_nig", "not_one_night")


def _constraints(case):
    """케이스 → 완화 가능한 제약 아이템 리스트 (QuickXplain 대상)."""
    cfg = case["config"]
    ic = cfg.get("initial_constraints") or {}
    items = []
    for nid, dm in (ic.get("forbidden") or {}).items():
        for d, codes in dm.items():
            if "O" in [str(c).upper() for c in codes]:
                items.append(("ban", str(nid), int(d)))          # OFF 금지(=강제근무)
    for nid, days in (ic.get("forced_off") or {}).items():
        for d in days:
            items.append(("foff", str(nid), int(d)))
    for rk in _RULE_KEYS:
        if cfg.get(rk):
            items.append(("rule", rk, None))
    items.append(("cov", None, None))                            # 야간 커버리지 수요
    return items


def _build(case, active):
    """활성 제약 부분집합으로 config 재구성."""
    cfg = copy.deepcopy(case["config"])
    fb, fo = {}, {}
    for kind, a, b in active:
        if kind == "ban":
            fb.setdefault(a, {})[str(b)] = ["O"]
        elif kind == "foff":
            fo.setdefault(a, []).append(int(b))
    cfg["initial_constraints"] = {"forbidden": fb, "forced_off": fo}
    active_rules = {a for k, a, _ in active if k == "rule"}
    for rk in _RULE_KEYS:
        cfg[rk] = rk in active_rules
    if ("cov", None, None) not in active:                        # 커버리지 완화 = 수요 0
        dsr = dict(cfg.get("daily_shift_requirements") or {}); dsr["N"] = 0
        cfg["daily_shift_requirements"] = dsr; cfg["nig_req"] = 0
    return cfg


def _infeasible(case, active, cnt):
    cnt[0] += 1
    cfg = _build(case, active)
    return diagnose_night_axis(case["nurses"], cfg, int(case["num_days"])).status == INFEASIBLE


def quickxplain(case):
    """Junker QuickXplain — 최소 충돌집합 + oracle 호출 수."""
    C = _constraints(case)
    cnt = [0]
    if not _infeasible(case, C, cnt):
        return None, cnt[0]                                      # 전체로도 feasible

    def qx(B, delta, C):
        if delta and _infeasible(case, B, cnt):
            return []
        if len(C) == 1:
            return list(C)
        mid = len(C) // 2
        C1, C2 = C[:mid], C[mid:]
        d2 = qx(B + C1, C1, C2)
        d1 = qx(B + d2, d2, C1)
        return d1 + d2

    return qx([], [], C), cnt[0]


def tier1_only(case):
    """max-flow/집계만 (Tier1) — 시퀀스·결합은 못 봄."""
    pool = _n_pool(case["nurses"], case["config"])
    if not pool:
        return False
    nd = int(case["num_days"])
    return bool(_perday_cert(pool, case["config"], nd) or _aggregate_cert(pool, case["config"], nd))


def main():
    paths = sorted(glob.glob(os.path.join(HERE, "cases", "*.json")))
    print(f"{'case':40} {'Tier1':6} {'QuickXplain(IIS)':22} {'branch-infer':22}")
    print("-" * 92)
    agg = {"qx_calls": 0, "qx_n": 0, "bi_certified": 0, "t1": 0, "n": 0}
    for p in paths:
        case = json.load(open(p))
        name = os.path.basename(p).replace(".json", "")[:38]
        # N축 원인만 대상(다른 축은 이 실험 밖)
        node = diagnose_night_axis(case["nurses"], case["config"], int(case["num_days"]))
        if node.status != INFEASIBLE:
            print(f"{name:40} {'-':6} {'(N축 원인 아님)':22} {node.status:22}")
            continue
        agg["n"] += 1
        t1 = tier1_only(case)
        agg["t1"] += int(t1)
        conflict, calls = quickxplain(case)
        agg["qx_calls"] += calls; agg["qx_n"] += 1
        agg["bi_certified"] += 1
        kind = node.certificate.kind if node.certificate else "proof-tree"
        qx_s = f"{calls}콜/충돌{len(conflict or [])}개"
        bi_s = f"1진단/{kind}"
        print(f"{name:40} {('잡음' if t1 else '못잡음'):6} {qx_s:22} {bi_s:22}")
    print("-" * 92)
    n = max(1, agg["n"])
    print(f"\nN축 원인 케이스: {agg['n']}건")
    print(f"  Tier1(flow-only) 격리:  {agg['t1']}/{agg['n']}  ({100*agg['t1']//n}%)  — 시퀀스·결합 놓침")
    print(f"  QuickXplain 평균 oracle 호출: {agg['qx_calls']/max(1,agg['qx_n']):.1f}회  (제약집합 산출)")
    print(f"  branch-infer 격리:      {agg['bi_certified']}/{agg['n']}  (100%)  1진단 + 행동가능 certificate")
    print("\n⚠️ 한계: QuickXplain oracle = 우리 진단기 자신(순환). 내부 PoC 검증이지 IIS 대비")
    print("   우수 결론 아님. 정식 비교엔 독립 oracle(CP-SAT)+실행시간+실코퍼스 필요(baseline_v2 TODO).")


if __name__ == "__main__":
    main()
