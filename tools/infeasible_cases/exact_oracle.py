"""독립 exact feasibility oracle — 우리 진단기와 무관한 ground-truth 판정기.

작은 인스턴스({D,E,N,O}, ≤~6명·≤~10일)에서 **정확한** 근무표 실현가능성을 backtracking 으로
판정한다. 우리 진단 스택(N/notN 오토마톤·max-flow)의 **완전성 검증용 기준선**이자,
baseline_v2 에서 QuickXplain/우리가 공유할 **독립 oracle** 이다.

우리 스택과의 결정적 차이(=hard-residual 이 생기는 지점):
  · 회복(recovery)은 **실제 OFF** 여야 한다. 우리 N/notN 오토마톤은 notN(=D/E/O)이면
    회복을 만족한다고 **관대**하게 본다 → 회복 OFF 가 D/E 커버리지와 충돌하는 결합은
    우리 스택엔 안 보이지만 이 oracle 엔 보인다.
  · D/E/N 을 구분해 일별 커버리지를 각각 강제(관대 모델은 N 만 봄).
  · (옵션) 야간→주간 금지 전이, 최대 연속근무.

모델은 현실적이되 우리 완전판정기가 아니다(의도). 인스턴스를 작게 유지해 exact 하게 판정.
"""

from __future__ import annotations

import sys
from itertools import product

_HERE = __file__.rsplit("/", 1)[0]
sys.path.insert(0, _HERE + "/../../app")

from services.ontology_graph.lagrangian import _night_rules, _nurse_attr  # noqa: E402


def _off_after(run: int, config: dict) -> int:
    """야간 run 종료 후 요구되는 **연속 OFF 일수**(실제 OFF)."""
    if config.get("two_offs_after_three_nig") and run >= 3:
        return 2
    if config.get("two_offs_after_two_nig") and run >= 2:
        return 2
    return 1 if run >= 1 else 0            # 최소 1 OFF (야간 후 휴식)


def _req(config: dict, shift: str) -> int:
    dsr = config.get("daily_shift_requirements") or {}
    if isinstance(dsr, dict) and dsr:
        return int(dsr.get(shift, 0) or 0)
    if shift == "N":
        return int(config.get("nig_req", 0) or 0)
    return 0


def _prep(nurses: list, config: dict):
    ic = config.get("initial_constraints") or {}
    fb = ic.get("forbidden") or {}
    fo = ic.get("forced_off") or {}
    out = []
    for nu in nurses:
        nid = str(_nurse_attr(nu, "nurse_id"))
        allowed = {str(x).strip().upper() for x in (_nurse_attr(nu, "allowed_shifts") or [])}
        work = (allowed & {"D", "E", "N"}) or {"D", "E", "N"}
        banned = {int(d): {str(c).strip().upper() for c in (codes or [])}
                  for d, codes in (fb.get(nid) or {}).items()}
        out.append({"nid": nid, "work": work, "banned": banned,
                    "foff": {int(d) for d in (fo.get(nid) or [])},
                    "grade": int(_nurse_attr(nu, "grade") or 1)})
    return out


def _options(n: dict, state: tuple, day: int, config: dict, max_run: int, min_run: int):
    """간호사 n 의 (state=(r,k,w,prev)) 에서 day 에 가능한 shift 코드들."""
    r, k, w, prev = state
    banned = n["banned"].get(day, set())
    must_work = "O" in banned                 # OFF 금지 = 강제 근무
    forced_off = day in n["foff"]
    max_work = config.get("max_consecutive_work")
    if k > 0:                                 # 회복 OFF 빚 → 반드시 OFF
        return [] if (must_work or forced_off) else ["O"]
    if forced_off:
        if r > 0 and r < min_run:
            return []                         # 짧은 run 을 OFF 로 강제종료 불가
        return [] if must_work else ["O"]
    if r > 0:                                 # 야간 run 진행 중
        opts = []
        if r + 1 <= max_run and "N" in n["work"] and "N" not in banned \
                and not (max_work and w + 1 > max_work):
            opts.append("N")
        if r >= min_run and not must_work:    # run 종료 → 회복 OFF 시작(D/E 로는 종료 불가)
            opts.append("O")
        return opts
    opts = []                                 # r==0, run 밖
    for s in ("D", "E", "N"):
        if s in n["work"] and s not in banned and not (max_work and w + 1 > max_work):
            opts.append(s)
    if not must_work and "O" not in banned:
        opts.append("O")
    if config.get("forbid_night_to_day") and prev == "N" and "D" in opts:
        opts.remove("D")
    return opts


def _step(state: tuple, s: str, config: dict, track_w: bool, track_prev: bool):
    r, k, w, prev = state
    w2 = (w + 1) if track_w else 0
    if s == "N":
        return (r + 1, 0, w2, "N" if track_prev else "")
    if s in ("D", "E"):
        return (0, 0, w2, s if track_prev else "")
    # s == "O"  (근무 아님 → 연속근무 리셋)
    if k > 0:
        return (0, k - 1, 0, "")
    if r > 0:                                 # run 종료 → 회복 빚
        return (0, max(0, _off_after(r, config) - 1), 0, "")
    return (0, 0, 0, "")


_BUDGET = 3_000_000


def is_feasible(nurses: list, config: dict, num_days: int):
    """정확 판정(backtracking + memo). True/False, None=탐색예산 초과(대형 인스턴스)."""
    N = _prep(nurses, config)
    max_run, rec_trig, min_run = _night_rules(config)
    reqD, reqE, reqN = _req(config, "D"), _req(config, "E"), _req(config, "N")
    track_w = config.get("max_consecutive_work") is not None
    track_prev = bool(config.get("forbid_night_to_day"))
    seen: set = set()
    budget = [_BUDGET]

    def rec(day: int, states: tuple):
        if day == num_days:
            return True
        key = (day, states)
        if key in seen:
            return False
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        seen.add(key)
        per = [_options(N[i], states[i], day, config, max_run, min_run)
               for i in range(len(N))]
        if any(not o for o in per):
            return False
        hit_budget = False
        for combo in product(*per):
            if sum(c == "D" for c in combo) < reqD:
                continue
            if sum(c == "E" for c in combo) < reqE:
                continue
            if sum(c == "N" for c in combo) < reqN:
                continue
            nxt = tuple(_step(states[i], combo[i], config, track_w, track_prev)
                        for i in range(len(N)))
            res = rec(day + 1, nxt)
            if res is True:
                return True
            if res is None:
                hit_budget = True
        return None if hit_budget else False

    init = tuple((0, 0, 0, "") for _ in N)
    return rec(0, init)


if __name__ == "__main__":
    import glob
    import json
    import os
    from services.ontology_graph.axis_diagnose import multi_axis_diagnose
    print("독립 exact oracle vs 우리 진단 스택 (exact 는 소형 인스턴스 전용)\n")
    for p in sorted(glob.glob(os.path.join(_HERE, "cases", "*.json"))):
        d = json.load(open(p))
        name = os.path.basename(p).replace(".json", "")[:38]
        nd, k = int(d["num_days"]), len(d["nurses"])
        our = multi_axis_diagnose(d["nurses"], d["config"], nd,
                                  d["year"], d["month"]).status
        if nd > 12 or k > 8:                       # exact 범위 밖(대형) → 스킵
            print(f"  oracle={'SKIP-대형':10} 우리={our:22} {name}")
            continue
        feas = is_feasible(d["nurses"], d["config"], nd)
        ol = {True: "FEAS", False: "INFEAS", None: "TIMEOUT"}[feas]
        gap = "  ⟵ GAP(우리가 놓침, VE 대상)" if (feas is False
                                              and our != "INFEASIBLE_CERTIFIED") else ""
        print(f"  oracle={ol:10} 우리={our:22} {name}{gap}")
