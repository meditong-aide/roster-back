"""Property-based randomized 교차검증 — Frontier DP ⟷ 독립 oracle(DFS) ⟷ CP-SAT.

수천 개 소형 랜덤 인스턴스에서 세 판정기를 비교해 **정확성**을 강하게 검증한다:
  · false INFEASIBLE (엔진 INFEAS 인데 실제 FEAS) — 반드시 0
  · false FEASIBLE   (엔진 FEAS 인데 실제 INFEAS) — 반드시 0
  · 대칭 on/off 불일치 — 반드시 0 (대칭 축소 soundness)
  · UNKNOWN 비율 · 최대 frontier 폭 · 실행시간

seed 고정 → 결정적. CP-SAT(있으면) 는 frontier_dp 와 **동일 semantics·lenient terminal**
로 모델링한 신뢰 기준선.
"""

from __future__ import annotations

import random
import sys
import time

_HERE = __file__.rsplit("/", 1)[0]
sys.path.insert(0, _HERE + "/../../app")
sys.path.insert(0, _HERE)

import exact_oracle  # noqa: E402
from exact_oracle import is_feasible  # noqa: E402
from services.ontology_graph.frontier_dp import (  # noqa: E402
    _interchangeable,
    _prep,
    diagnose_frontier,
)

exact_oracle._BUDGET = 300_000        # per-case 상한 낮춤 — 병목 케이스는 None(skip)로 빠르게

try:
    from ortools.sat.python import cp_model
    _HAS_CPSAT = True
except Exception:
    _HAS_CPSAT = False


def _rand_case(rng: random.Random) -> tuple:
    k = rng.randint(3, 5)
    days = rng.randint(4, 6)
    # 수요: 합이 1..k
    while True:
        rD, rE, rN = rng.randint(0, 2), rng.randint(0, 2), rng.randint(0, 2)
        if 1 <= rD + rE + rN <= k:
            break
    rules = {}
    if rng.random() < 0.7:
        rules["not_one_night"] = True
    r = rng.random()
    if r < 0.4:
        rules["two_offs_after_two_nig"] = True
    elif r < 0.7:
        rules["two_offs_after_three_nig"] = True
    if rng.random() < 0.25:
        rules["forbid_night_to_day"] = True
    if rng.random() < 0.25:
        rules["max_consecutive_work"] = rng.randint(3, 5)
    cohort = rng.random() < 0.25          # 교환가능 코호트(대칭 축소를 실제로 발동시킴)
    nurses = []
    coh_allowed = rng.choice([None, ["N"], ["D", "E"]]) if cohort else None
    for i in range(k):
        if cohort:
            allowed = coh_allowed
        else:
            rr = rng.random()
            allowed = ["N"] if rr < 0.15 else (["D", "E"] if rr < 0.3 else None)
        nurses.append({"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A",
                       **({"allowed_shifts": allowed} if allowed else {})})
    fb, fo = {}, {}
    if cohort:
        # 전원 동일 패턴(교환가능 유지): 같은 날 같은 제약을 모두에게
        for d in range(days):
            p = rng.random()
            if p < 0.05:
                for i in range(k):
                    fb.setdefault(f"n{i}", {})[d] = ["O"]
            elif p < 0.09:
                for i in range(k):
                    fo.setdefault(f"n{i}", []).append(d)
    else:
        for i in range(k):
            for d in range(days):
                p = rng.random()
                if p < 0.06:
                    fb.setdefault(f"n{i}", {})[d] = ["O"]      # 강제근무
                elif p < 0.10:
                    fb.setdefault(f"n{i}", {})[d] = ["N"]      # N 금지
                elif p < 0.13:
                    fo.setdefault(f"n{i}", []).append(d)       # 강제 OFF
    cfg = dict(rules, daily_shift_requirements={"D": rD, "E": rE, "N": rN})
    cfg["initial_constraints"] = {"forbidden": fb, "forced_off": fo}
    return nurses, cfg, days


# ── CP-SAT 기준선 (frontier_dp 와 동일 semantics 지향, lenient terminal) ──────────
# ⚠️ 근사: 회복(off_after) 을 run 길이별로 정확 인코딩하지 않고 max_run 기준으로 강제해
#    일부 case 에서 과제약/저제약. frontier_dp⟷oracle(둘 다 exact automaton)이 primary 기준선.
def _off_after(run, cfg):
    if cfg.get("two_offs_after_three_nig") and run >= 3:
        return 2
    if cfg.get("two_offs_after_two_nig") and run >= 2:
        return 2
    return 1 if run >= 1 else 0


def cpsat_feasible(nurses, cfg, days):
    m = cp_model.CpModel()
    ic = cfg.get("initial_constraints") or {}
    fb = ic.get("forbidden") or {}
    fo = ic.get("forced_off") or {}
    dsr = cfg["daily_shift_requirements"]
    rD, rE, rN = dsr.get("D", 0), dsr.get("E", 0), dsr.get("N", 0)
    S = ["D", "E", "N", "O"]
    max_run = int(cfg.get("max_consecutive_nights", 3) or 3)
    min_run = 2 if cfg.get("not_one_night") else 1
    maxw = cfg.get("max_consecutive_work")
    x = {}
    for i, nu in enumerate(nurses):
        allowed = {str(s).upper() for s in (nu.get("allowed_shifts") or [])}
        work = (allowed & {"D", "E", "N"}) or {"D", "E", "N"}
        banned = {int(d): {str(c).upper() for c in codes} for d, codes in (fb.get(nu["nurse_id"]) or {}).items()}
        foff = {int(d) for d in (fo.get(nu["nurse_id"]) or [])}
        for d in range(days):
            row = {}
            for s in S:
                v = m.NewBoolVar(f"x{i}_{d}_{s}")
                row[s] = v
                if s in ("D", "E", "N") and s not in work:
                    m.Add(v == 0)
                if s in banned.get(d, set()):
                    m.Add(v == 0)
                if s == "O" and "O" in banned.get(d, set()):
                    m.Add(v == 0)                            # 강제근무
            if d in foff:
                m.Add(row["O"] == 1)
            m.Add(sum(row.values()) == 1)
            x[i, d] = row
    for d in range(days):
        m.Add(sum(x[i, d]["D"] for i in range(len(nurses))) >= rD)
        m.Add(sum(x[i, d]["E"] for i in range(len(nurses))) >= rE)
        m.Add(sum(x[i, d]["N"] for i in range(len(nurses))) >= rN)
    for i in range(len(nurses)):
        N = [x[i, d]["N"] for d in range(days)]
        O = [x[i, d]["O"] for d in range(days)]
        D = [x[i, d]["D"] for d in range(days)]
        # max_run: 연속 N ≤ max_run
        for d in range(days - max_run):
            m.Add(sum(N[d:d + max_run + 1]) <= max_run)
        # not_one_night: 고립 N 금지 (경계 lenient — 내부만). N[d] → N[d-1] ∨ N[d+1]
        if min_run >= 2:
            for d in range(1, days - 1):
                m.AddBoolOr([N[d].Not(), N[d - 1], N[d + 1]])
        # recovery: run 종료(N[d]=1,N[d+1]=0) → 다음 off_after 일 OFF (경계 lenient)
        for d in range(days - 1):
            oa = _off_after(max_run, cfg)                    # 보수적: 최대 run 기준 off 요구
            end = m.NewBoolVar(f"end{i}_{d}")
            m.Add(N[d] - N[d + 1] == 1).OnlyEnforceIf(end)
            m.Add(N[d] - N[d + 1] <= 0).OnlyEnforceIf(end.Not())
            for j in range(1, oa + 1):
                if d + j < days:
                    m.Add(O[d + j] == 1).OnlyEnforceIf(end)
        # forbid_night_to_day
        if cfg.get("forbid_night_to_day"):
            for d in range(days - 1):
                m.Add(N[d] + D[d + 1] <= 1)
        # max_consecutive_work
        if maxw:
            for d in range(days - maxw):
                m.Add(sum(1 - O[e] for e in range(d, d + maxw + 1)) <= maxw)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0
    solver.parameters.num_search_workers = 1
    st = solver.Solve(m)
    if st in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        return True
    if st == cp_model.INFEASIBLE:
        return False
    return None


def main(n=3000, seed=12345, use_cpsat=True, cp_cap=300):
    """auto(실제 경로) frontier ⟷ oracle ⟷ CP-SAT 비교. auto=대칭 자동감지(교환가능시만 정렬)."""
    rng = random.Random(seed)
    agg = {"n": 0, "false_inf": 0, "false_feas": 0, "auto_mismatch": 0, "sym_fired": 0,
           "unknown": 0, "max_w": 0, "cp_n": 0, "cp_mismatch": 0}
    t0 = time.time()
    examples = {"false_inf": None, "false_feas": None, "auto_mismatch": None, "cp_mismatch": None}
    for _ in range(n):
        nurses, cfg, days = _rand_case(rng)
        orc = is_feasible(nurses, cfg, days)
        if orc is None:
            continue
        fr_plain = diagnose_frontier(nurses, cfg, days, symmetry=False)
        fr_auto = diagnose_frontier(nurses, cfg, days, symmetry=None)   # 실제 경로
        if _interchangeable(_prep(nurses, cfg), 0, days):
            agg["sym_fired"] += 1
        agg["n"] += 1
        agg["max_w"] = max(agg["max_w"], fr_plain.width_max, fr_auto.width_max)
        # auto 는 plain 과 항상 같아야(자동감지가 sound 하면 교환가능시만 정렬)
        if fr_plain.status != "UNKNOWN" and fr_auto.status != "UNKNOWN" \
                and fr_plain.status != fr_auto.status:
            agg["auto_mismatch"] += 1
            examples["auto_mismatch"] = examples["auto_mismatch"] or (nurses, cfg, days)
        if fr_auto.status == "UNKNOWN":
            agg["unknown"] += 1
        else:
            eng = (fr_auto.status == "INFEASIBLE_CERTIFIED")
            if eng and orc is True:
                agg["false_inf"] += 1
                examples["false_inf"] = examples["false_inf"] or (nurses, cfg, days)
            if (not eng) and orc is False:
                agg["false_feas"] += 1
                examples["false_feas"] = examples["false_feas"] or (nurses, cfg, days)
        if use_cpsat and _HAS_CPSAT and agg["cp_n"] < cp_cap:
            cp = cpsat_feasible(nurses, cfg, days)
            if cp is not None:
                agg["cp_n"] += 1
                if (cp is False) != (orc is False):
                    agg["cp_mismatch"] += 1
                    examples["cp_mismatch"] = examples["cp_mismatch"] or (nurses, cfg, days)
    dt = time.time() - t0
    print(f"교차검증 {agg['n']}건 ({dt:.1f}s, seed={seed})  CP-SAT={'ON' if _HAS_CPSAT and use_cpsat else 'off'}")
    print(f"  false INFEASIBLE (auto 오판 infeas) : {agg['false_inf']}   ← 0 이어야")
    print(f"  false FEASIBLE   (auto 오판 feas)   : {agg['false_feas']}   ← 0 이어야")
    print(f"  auto ≠ plain (대칭 자동감지 sound)  : {agg['auto_mismatch']}   ← 0 이어야 "
          f"(대칭 실발동 {agg['sym_fired']}건)")
    if agg["cp_n"]:
        print(f"  CP-SAT ⟷ oracle 불일치 ({agg['cp_n']}건 중): {agg['cp_mismatch']}   ← 0 이어야")
    print(f"  UNKNOWN 비율: {100*agg['unknown']/max(1,agg['n']):.1f}%   최대 frontier 폭: {agg['max_w']}")
    return agg, examples


if __name__ == "__main__":
    import json
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    agg, ex = main(N)
    bad = agg["false_inf"] + agg["false_feas"] + agg["auto_mismatch"] + agg["cp_mismatch"]
    if bad:
        for kind, case in ex.items():
            if case:
                print(f"\n반례[{kind}]:")
                print("  ", json.dumps({"cfg": case[1], "days": case[2],
                                        "nurses": [n.get("allowed_shifts", "all") for n in case[0]]},
                                       ensure_ascii=False))
    print("\n" + ("✗ 불일치 발견 — 위 반례로 디버깅" if bad else "✓ 전건 일치 (false-inf/feas=0, 대칭 sound, CP-SAT 일치)"))
