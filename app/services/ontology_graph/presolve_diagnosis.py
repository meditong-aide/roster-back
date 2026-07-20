"""Pre-solve shortage diagnosis (2단계 구조의 ①).

솔버를 돌리기 전에 max-flow(per-day + 월별)로 어떤 시프트가 얼마나 부족할지, 그리고
'왜'(자격 부족 vs 총 capacity)를 ms 단위로 조기진단한다. 통합 그래프 + recommend_actions
로 **관리자가 바꿀 수 있는** 복구 선택지를 랭킹한다.

원칙(제품 결정): 개인 속성(allowed_shifts / max_nig / weekend_off 등)은 불가침. 그것을
그렇게 설정한 결과 부족이 나면 그게 올바른 구조적 결과다. 따라서 복구 선택지는
관리자 통제 노브(커버리지 수요, 팀/등급 최소 등)만 제시하고, 개인 속성 변경은 절대 넣지 않는다.

이 진단은 blocking 이 아니라 advisory — infeasible 로 막지 않고 "부족 예상 + 이유 + 선택지"만 준다.
실측 부족 수치는 솔버 후 coverage_gaps(③)가 담당한다. 여기 값은 '증명된 하한'(max-flow fill ≥
solver fill 이므로 여기 shortage ≤ 실제 shortage). 단 조합적 원인(2N2OFF/transition)은 못 보므로
과소추정될 수 있고 그 부분은 솔버 MUS 가 보완한다.
"""

from __future__ import annotations

import calendar
import time
from typing import Any, Dict, List

from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes
from services.ontology_graph.builder import UnifiedGraphInput, build_unified_graph
from services.ontology_graph.recommender import recommend_actions
from services.ontology_graph.supply_demand import (
    NurseSupply,
    compute_monthly_supply_demand,
    compute_supply_demand,
)

WORK = ("D", "E", "N")

# 개인 속성 노브(불가침) — 복구 선택지에서 제외할 config_key.
_PERSONAL_ATTR_KEYS = {
    "max_nig_per_month", "max_night_shifts_per_month", "n_max", "n_exact",
    "allowed_shifts", "is_weekend_off", "weekend_off_only_enable",
}


def _demand(config: Dict[str, Any]) -> Dict[str, int]:
    dsr = config.get("daily_shift_requirements")
    if isinstance(dsr, dict) and dsr:
        return {s: int(dsr.get(s, 0) or 0) for s in WORK}
    return {"D": int(config.get("day_req", 0) or 0),
            "E": int(config.get("eve_req", 0) or 0),
            "N": int(config.get("nig_req", 0) or 0)}


def _off_days(config: Dict[str, Any]) -> int:
    if config.get("off_days") is not None:
        return int(float(config.get("off_days") or 0))
    return (int(config.get("global_monthly_off_days", 0) or 0)
            + int(config.get("standard_personal_off_days", 0) or 0))


def _eligible(nu: Dict[str, Any], use_mid: bool) -> set:
    s = normalize_allowed_shift_codes(nu.get("allowed_shifts"), use_mid=use_mid)
    return s if s else set(WORK)


def presolve_shortage_diagnosis(
    nurses_data: List[Dict[str, Any]],
    config_data: Dict[str, Any],
    year: int,
    month: int,
) -> Dict[str, Any]:
    """솔버 전 부족 조기진단. Returns dict(shortages, recovery_options, elapsed_ms)."""
    t0 = time.perf_counter()
    days = calendar.monthrange(year, month)[1]
    use_mid = bool(config_data.get("use_mid", False))
    dem = _demand(config_data)
    off = _off_days(config_data)
    workdays = max(0, days - off)
    max_nig = int(config_data.get("max_nig_per_month", 15) or 15)
    if max_nig <= 0:
        max_nig = 15

    # eligibility + per-nurse 야간 상한
    supplies: List[NurseSupply] = []
    workdays_by: Dict[str, int] = {}
    night_cap_by: Dict[str, int] = {}
    g_nurses: List[Dict[str, Any]] = []
    for nu in nurses_data:
        nid = str(nu.get("nurse_id"))
        elig = _eligible(nu, use_mid)
        supplies.append(NurseSupply(nurse_id=nid, grade=nu.get("grade"),
                                    team_id=nu.get("team_id"),
                                    eligible_by_day={d: set(elig) for d in range(days)}))
        workdays_by[nid] = workdays
        # per-nurse n_exact/n_max 우선, 없으면 전역 max_nig
        pn = None
        for k in ("n_exact", "n_max"):
            v = nu.get(k)
            if v is not None:
                try:
                    iv = int(v)
                    if iv >= 0:
                        pn = iv
                        break
                except (TypeError, ValueError):
                    pass
        night_cap_by[nid] = min(max_nig, pn) if pn is not None else max_nig
        g_nurses.append({"nurse_id": nid, "grade": nu.get("grade"), "team_id": nu.get("team_id")})

    req_by_day = {d: dict(dem) for d in range(days)}
    gi = UnifiedGraphInput(
        nurses=g_nurses, num_days=days, work_shifts=list(WORK),
        requirements_by_day=req_by_day,
        eligible_by_nurse_day={s.nurse_id: {d: set(s.eligible_by_day[d]) for d in range(days)}
                               for s in supplies})

    per = compute_supply_demand(supplies, req_by_day, work_shifts=WORK)
    mon = compute_monthly_supply_demand(
        supplies, {s: dem[s] * days for s in WORK if dem[s] > 0},
        workdays_by_nurse=workdays_by, night_cap_by_nurse=night_cap_by)

    graph = build_unified_graph(gi, supply_demand=per, monthly_supply_demand=mon)
    actions = recommend_actions(graph)

    # 부족 리스트 (시프트별, 이유 부착)
    shortages: List[Dict[str, Any]] = []
    for ms in mon.shifts:
        if ms.shortage <= 0:
            continue
        reason = ("eligibility_shortage" if ms.eligible_nurses < dem.get(ms.shift_code, 0)
                  else "capacity_shortage")
        shortages.append({
            "shift": ms.shift_code,
            "daily_required": dem.get(ms.shift_code, 0),
            "eligible_nurses": ms.eligible_nurses,
            "monthly_required": ms.required,
            "monthly_fillable": ms.filled,
            "monthly_shortage_lower_bound": ms.shortage,   # 증명된 하한
            "reason": reason,
        })

    # 복구 선택지 — 관리자 통제 노브만(개인 속성 제외)
    recovery: List[Dict[str, Any]] = []
    for a in actions:
        if a.config_key in _PERSONAL_ATTR_KEYS:
            continue  # 개인 속성 불가침
        recovery.append({
            "rank": a.rank, "family": a.target_family, "action": a.action_type,
            "config_key": a.config_key, "direction": a.direction,
            "amount": a.delta.get("amount"), "rationale": a.rationale,
        })

    return {
        "shortages": shortages,
        "recovery_options": recovery,
        "per_day_bottleneck_cells": len(per.bottleneck_cells),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
    }
