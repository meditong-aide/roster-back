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
from services.ontology_graph.blame import score_blame
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
    base = s if s else set(WORK)
    # fixed_shift(고정근무): 그 간호사는 그 시프트만 공급 → 다른 시프트 공급 0(시프트축 관계 반영).
    fx = nu.get("fixed_shift")
    if fx:
        fxs = normalize_allowed_shift_codes([fx], use_mid=use_mid) & set(WORK)
        if fxs:  # 유효 work 시프트 고정일 때만 제한(OFF/비근무 고정은 무시)
            base = (base & fxs) or fxs
    return base


def _active_day_set(nu: Dict[str, Any], year: int, month: int, days: int) -> set:
    """joining/resignation_date 로 이 달의 활성 day 인덱스(0-based) 집합.
    월중 입사·퇴사자는 활성 범위 밖에서 공급 0 → max-flow 가 부분월 가용을 정확히 본다.
    (완화 대상 아님 — 정확도용.)
    """
    lo, hi = 0, days - 1

    def _d(x):
        return x.date() if hasattr(x, "date") else x

    jd = nu.get("joining_date")
    if jd is not None:
        j = _d(jd)
        if (j.year, j.month) == (year, month):
            lo = max(lo, int(j.day) - 1)
        elif (j.year, j.month) > (year, month):
            return set()  # 이 달 이후 입사 → 이 달 비활성
    rd = nu.get("resignation_date")
    if rd is not None:
        r = _d(rd)
        if (r.year, r.month) == (year, month):
            hi = min(hi, int(r.day) - 1)
        elif (r.year, r.month) < (year, month):
            return set()  # 이 달 이전 퇴사 → 이 달 비활성
    return set(range(lo, hi + 1)) if lo <= hi else set()


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
    # [관계 모델링] 주말휴무자는 주말 요일에 공급 0(강제OFF)로 요일별 반영 → max-flow 가
    #   주말 커버리지 부족을 구조적으로 계산(누가·몇 명 풀어야 하는지). 월 capacity 는 전역
    #   off_days 로 이미 반영되므로 workdays_by 는 안 건드림(주말은 요일축에서만 뺀다).
    _wk_day_set = {d for d in range(days) if calendar.weekday(year, month, d + 1) >= 5}

    # [관계 모델링] 일자별 가동 팀도 주말휴무와 같은 축이다 — 그날 안 도는 팀의 인원은
    #   공급 0(강제OFF). 여기에 반영해야 max-flow 가 "그 팀을 뺐더니 이 시프트가 몇 명
    #   모자란다"를 구조적으로 계산하고, blame·recommend_actions 까지 자동으로 흐른다.
    #   daily_active_teams_by_day[d] 가 None 이면 미설정 → 전 팀 가동(제약 없음).
    _daily_active = config_data.get("daily_active_teams_by_day")

    def _team_off_on(day_idx: int, team_id: Any) -> bool:
        """그날 이 팀이 비가동인가. 팀 미배정(None)은 대상 아님."""
        if not _daily_active or day_idx >= len(_daily_active):
            return False
        active = _daily_active[day_idx]
        if active is None:
            return False
        if team_id in (None, "", 0):
            return False
        return str(team_id) not in {str(t) for t in active}

    supplies: List[NurseSupply] = []
    workdays_by: Dict[str, int] = {}
    night_cap_by: Dict[str, int] = {}
    g_nurses: List[Dict[str, Any]] = []
    for nu in nurses_data:
        nid = str(nu.get("nurse_id"))
        elig = _eligible(nu, use_mid)
        _is_wko = bool(nu.get("is_weekend_off"))
        _act = _active_day_set(nu, year, month, days)  # 부분월 가용(입퇴사)
        # 공급 있는 날 = 활성일 AND (주말휴무자면 주말 제외) AND (그날 팀이 가동).
        #   나머지 날은 공급 0.
        _tid = nu.get("team_id")
        _ebd = {d: (set(elig)
                    if (d in _act
                        and not (_is_wko and d in _wk_day_set)
                        and not _team_off_on(d, _tid))
                    else set())
                for d in range(days)}
        supplies.append(NurseSupply(nurse_id=nid, grade=nu.get("grade"),
                                    team_id=nu.get("team_id"), eligible_by_day=_ebd))
        # 월 capacity 도 활성일로 상한(부분월 입퇴사자는 근무 가능일 자체가 적다).
        workdays_by[nid] = min(workdays, len(_act))
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

    # 문제 노드 랭킹(blame 전파) — "무엇/누구/어느 그룹이 가장 큰 병목인가" 결정론·설명가능.
    # advisory: 실패해도 진단 전체를 막지 않는다.
    problem_ranking: Dict[str, Any] = {}
    try:
        blame = score_blame(graph)

        def _sd(s) -> Dict[str, Any]:
            return {"node_id": s.node_id, "label": s.label, "blame": s.blame,
                    "reasons": [{"label": lbl, "pct": pct} for _, lbl, _, pct in s.reasons]}

        problem_ranking = {
            "top_constraints": [_sd(s) for s in blame.top_constraints[:5]],
            "top_groups": [_sd(s) for s in blame.top_groups[:5]],
            "top_objects": [_sd(s) for s in blame.top_objects[:5]],
            "converged": blame.converged,
        }
    except Exception as _blame_exc:  # noqa: BLE001
        print(f"[Presolve] blame 랭킹 실패(무시): {_blame_exc}")

    # [관계 → 인원] 주말 요일별 부족의 최댓값 = 주말휴무를 풀어야 하는 최소 인원(각 해제가
    #   주말 매일 +1 공급). 조합 brute-force 없이 max-flow 로 '몇 명'을 직접 산출.
    #   ★ 두 가지를 뺀다. 안 빼면 원인이 다른 부족을 주말휴무 탓으로 돌려 "주말 휴무를
    #     푸세요" 라는 엉뚱한 안내가 나간다.
    #     · 주말휴무자가 아예 없으면 풀 대상이 없다 → 0.
    #     · 일자별 가동 팀이 지정된 날의 부족은 그 지정이 원인이다(별도 family).
    _wko_exists = any(bool(nu.get("is_weekend_off")) for nu in nurses_data)
    _wk_pure = {
        d for d in _wk_day_set
        if not (_daily_active and d < len(_daily_active) and _daily_active[d] is not None)
    }
    weekend_release_needed = (
        max((per.day_shortage.get(d, 0) for d in _wk_pure), default=0)
        if _wko_exists else 0
    )

    # ── 개인 제약 압박 감지 (제약모순형 병목: 커버리지 max-flow 의 사각지대) ──
    #   커버리지 부족이 아니어도 개인 제약이 물리적 모순/과부하를 만들면 여기서 잡아
    #   probe 우선 완화군(pressure_families)으로 넘긴다. 콤보를 앞당겨 재solve 수↓.
    #   전체 시프트 한도(d/e/n × exact·min) + 주말휴무 + off 예산을 함께 본다(단일 속성만
    #   보던 사각지대 제거). 각 압박은 실제 완화 레버가 있는 family 로만 매핑한다.
    #   · night_floor_over_cap : N 하한 > config max_nig → 달성 불가 → night_cap
    #   · shift_floor_over_workdays : 강제 하한 합 > 근무가능일 → 과결속 → off_budget
    #   · weekend_off_load : 주말휴무자(주말 전일 강제OFF) → weekend_off
    _wk_days = sum(1 for d in range(days) if calendar.weekday(year, month, d + 1) >= 5)
    pressure_families: List[str] = []
    constraint_flags: List[Dict[str, Any]] = []
    _pf_seen: set = set()

    def _add_pf(f: str) -> None:
        if f not in _pf_seen:
            _pf_seen.add(f)
            pressure_families.append(f)

    def _floor(nu: Dict[str, Any], s: str):
        v = nu.get(f"{s}_exact")
        if v is None:
            v = nu.get(f"{s}_min")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    for nu in nurses_data:
        nid = str(nu.get("nurse_id"))
        # 이 간호사의 강제 OFF 부담(주말휴무 우선; 없으면 전역 off 예산)
        forced_off = 0
        if bool(nu.get("is_weekend_off")):
            forced_off = _wk_days
            _add_pf("weekend_off")
            constraint_flags.append({
                "nurse_id": nid, "type": "weekend_off_load", "forced_off": _wk_days,
                # 관계 분석 결과: 주말 커버리지를 맞추려면 몇 명 풀어야 하는지(0=주말 커버리지엔
                # 여유, 그래도 월 capacity 병목일 수 있어 단일 지목으로 처리).
                "release_needed": weekend_release_needed})
        workdays_n = max(0, days - max(off, forced_off))
        # 시프트별 강제 하한(exact 우선). N 은 config 상한과, 전체 합은 근무가능일과 대조.
        floors = {s: _floor(nu, s) for s in ("d", "e", "n")}
        floors = {s: v for s, v in floors.items() if v is not None}
        n_floor = floors.get("n")
        if n_floor is not None and n_floor > max_nig:
            _add_pf("night_cap")
            constraint_flags.append({
                "nurse_id": nid, "type": "night_floor_over_cap",
                "n_floor": n_floor, "max_nig": max_nig})
        total_floor = sum(floors.values())
        if total_floor > workdays_n:
            _add_pf("off_budget")
            constraint_flags.append({
                "nurse_id": nid, "type": "shift_floor_over_workdays",
                "total_floor": total_floor, "workdays": workdays_n, "floors": floors})

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
        # 제약모순형 병목의 완화군 힌트(커버리지 shortages 와 별개). probe 우선순위에 먹인다.
        "pressure_families": pressure_families,
        "constraint_flags": constraint_flags,
        # 주말 요일별 max-flow 로 산출한, 주말휴무를 풀어야 하는 최소 인원(관계 분석 결과).
        "weekend_release_needed": weekend_release_needed,
        "per_day_bottleneck_cells": len(per.bottleneck_cells),
        # 문제 노드 blame 랭킹 (무엇/누구/어느 그룹) — 설명 근거(reasons) 포함.
        "problem_ranking": problem_ranking,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
    }
