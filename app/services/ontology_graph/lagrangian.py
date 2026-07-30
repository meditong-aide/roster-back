"""Lagrangian 승수 → 원리적 blame 신호.

hard/soft 제약을 목적으로 dualize 하고 subgradient 로 승수 λ 를 추정한다.
  L(λ) = min_x [ Σ_i λ_i · violation_i(x) ]         (모든 제약을 penalty 로 완화)
  λ_i ← max(0, λ_i + α_t · violation_i(x*))          (subgradient 상승)

λ_i = 제약 i 의 쌍대압력 = "얼마나 binding/문제인가". infeasible 이면 회피 불가한
irreducible 집합(=MCS)의 λ 가 계속 커져 원인을 지목한다. hand-set weight(blame.py)
를 대체하는 원리적 신호이며, soft(=목적 penalty)도 같은 틀에 자연히 들어온다.

max-flow 가 못 보는 배열 결합도, 부분배정을 실제로 풀어(각 iter solve) 위반을 관측
하므로 잡힌다. LP 완화와 달리 정수해를 풀어 integrality gap 케이스도 압력이 잡힌다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ortools.sat.python import cp_model

from services.ontology_graph.schema import ConstraintNode, OntologyGraph, StateNode

WORK = ("D", "E", "N")
SHIFTS = ("D", "E", "N", "O")


def estimate_multipliers(
    build_relaxed: Callable[[dict[str, float]], tuple[cp_model.CpModel, dict[str, Any]]],
    names: list[str],
    *,
    iters: int = 60,
    alpha0: float = 2.0,
    scale: int = 1000,
    time_per_solve: float = 0.5,
) -> dict[str, float]:
    """subgradient 로 제약별 λ 추정. build_relaxed(λ)->(model, viol_by_name)."""
    lam = {n: 1.0 for n in names}
    for t in range(iters):
        m, viol = build_relaxed(lam)
        m.Minimize(sum(int(round(scale * lam[n])) * viol[n] for n in names))
        s = cp_model.CpSolver()
        s.parameters.max_time_in_seconds = time_per_solve
        s.parameters.num_search_workers = 1
        st = s.Solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        alpha = alpha0 / (1.0 + t)            # 감소 스텝(수렴)
        for n in names:
            lam[n] = max(0.0, lam[n] + alpha * float(s.Value(viol[n])))
    return lam


def relaxable_roster(
    num_nurses: int, num_days: int, req: dict[str, int], *,
    recovery: bool = False, ban_n2d: bool = False, max_consec: int | None = None,
    off_floor: int | None = None, night_cap: int | None = None,
    weekend_off_nurses: set[int] | None = None, weekend_days: set[int] | None = None,
    night_cap_by_nurse: dict[int, int] | None = None,
) -> tuple[Callable, list[str], dict[str, dict]]:
    """미니 로스터의 relaxable(위반변수 포함) 빌더 + 이름 + meta. 결합=커버리지×시퀀스.

    off_floor: 간호사별 최소 OFF 하한(=off_budget 결합). night_cap: 간호사별 월 야간 상한.
    이 둘이 max-flow 가 놓치는 결합(OFF예산·야간cap)을 λ 로 드러낸다.
    """
    specs: list[tuple[str, str, dict, tuple]] = []
    for d in range(num_days):
        for s in WORK:
            need = int(req.get(s, 0))
            if need > 0:
                specs.append(("cov", f"Coverage:{s}:d{d}",
                              {"family": "CoverageMin", "pattern": "coverage",
                               "label": f"d{d+1} {s}≥{need}"}, (d, s, need)))
    if off_floor is not None:
        for n in range(num_nurses):
            specs.append(("off", f"OffFloor:n{n}",
                          {"family": "OffBudget", "pattern": "off_budget",
                           "label": f"n{n} OFF≥{off_floor}"}, (n, off_floor)))
    if night_cap is not None:
        for n in range(num_nurses):
            specs.append(("ncap", f"NightCap:n{n}",
                          {"family": "MonthlyNightCap", "pattern": "night_cap",
                           "label": f"n{n} N≤{night_cap}"}, (n, night_cap)))
    # per-nurse 야간 상한(monthly_limit): n_max/n_exact 개별. night_cap(전역)보다 우선.
    for n, cap in (night_cap_by_nurse or {}).items():
        specs.append(("ncap", f"MonthlyLimitN:n{n}",
                      {"family": "MonthlyLimit", "pattern": "monthly_limit",
                       "label": f"n{n} 월N≤{cap}"}, (n, int(cap))))
    # weekend-off: 지정 간호사는 주말 강제 OFF. 커버리지가 그 주말을 요구하면 압력.
    if weekend_off_nurses and weekend_days:
        for n in weekend_off_nurses:
            for d in weekend_days:
                specs.append(("wkoff", f"WeekendOff:n{n}:d{d}",
                              {"family": "WeekendOff", "pattern": "weekend_off",
                               "label": f"n{n} 주말OFF(d{d+1})"}, (n, d)))
    if recovery:
        for n in range(num_nurses):
            for d in range(num_days - 1):
                specs.append(("rec", f"NightRecovery:n{n}:d{d}",
                              {"family": "NightRecovery", "pattern": "night_recovery",
                               "label": f"n{n} N→OFF(d{d+1})"}, (n, d)))
    if ban_n2d:
        for n in range(num_nurses):
            for d in range(num_days - 1):
                specs.append(("ban", f"TransitionBanN2D:n{n}:d{d}",
                              {"family": "BoundaryTransitionBan", "pattern": "transition_ban",
                               "label": f"n{n} N→D금지(d{d+1})"}, (n, d)))
    if max_consec is not None:
        for n in range(num_nurses):
            for d in range(num_days - max_consec):
                specs.append(("mcw", f"MaxConsec:n{n}:d{d}",
                              {"family": "ConsecutiveWorkLimit", "pattern": "consecutive_work",
                               "label": f"n{n} 연속≤{max_consec}(d{d+1})"}, (n, d, max_consec)))
    names = [sp[1] for sp in specs]
    meta = {sp[1]: sp[2] for sp in specs}

    def build(lam: dict[str, float]):
        m = cp_model.CpModel()
        X = {(n, d, s): m.NewBoolVar(f"x{n}_{d}_{s}")
             for n in range(num_nurses) for d in range(num_days) for s in SHIFTS}
        for n in range(num_nurses):
            for d in range(num_days):
                m.Add(sum(X[(n, d, s)] for s in SHIFTS) == 1)
        viol: dict[str, Any] = {}
        for kind, name, _mta, payload in specs:
            if kind == "cov":
                d, s, need = payload
                v = m.NewIntVar(0, need, f"v_{name}")
                m.Add(sum(X[(n, d, s)] for n in range(num_nurses)) + v >= need)
            elif kind == "off":
                n, floor = payload
                v = m.NewIntVar(0, floor, f"v_{name}")
                m.Add(sum(X[(n, d, "O")] for d in range(num_days)) + v >= floor)  # OFF 부족→위반
            elif kind == "ncap":
                n, cap = payload
                v = m.NewIntVar(0, num_days, f"v_{name}")
                m.Add(sum(X[(n, d, "N")] for d in range(num_days)) - v <= cap)     # N 초과→위반
            elif kind == "wkoff":
                n, d = payload
                v = m.NewBoolVar(f"v_{name}")
                m.Add(1 - X[(n, d, "O")] <= v)                                     # 주말 ¬OFF→위반
            elif kind == "rec":
                n, d = payload
                v = m.NewBoolVar(f"v_{name}")
                m.Add(X[(n, d, "N")] - X[(n, d + 1, "O")] <= v)      # N인데 다음날 ¬OFF → 위반
            elif kind == "ban":
                n, d = payload
                v = m.NewBoolVar(f"v_{name}")
                m.Add(X[(n, d, "N")] + X[(n, d + 1, "D")] - 1 <= v)  # N 다음날 D → 위반
            else:  # mcw
                n, d, k = payload
                v = m.NewBoolVar(f"v_{name}")
                m.Add(1 - sum(X[(n, d + j, "O")] for j in range(k + 1)) <= v)  # 창에 OFF 없음 → 위반
            viol[name] = v
        return m, viol

    return build, names, meta


def lagrangian_to_graph(lambdas: dict[str, float], meta: dict[str, dict],
                        *, eps: float = 1e-6) -> OntologyGraph:
    """λ → 온톨로지 그래프. 제약노드 + λ-seed state(pressures) → score_blame 이 랭킹.

    λ 를 state.evidence['shortage'] 로 seed 하므로 blame.py 가 그대로 소비한다
    (hand-set weight 대신 원리적 쌍대압력이 흐른다)."""
    g = OntologyGraph()
    for name, lam in lambdas.items():
        if lam <= eps:
            continue
        m = meta.get(name, {})
        cid = f"constraint:{name}"
        g.add_node(ConstraintNode(node_id=cid, label=m.get("label", name),
                                  family=m.get("family", "Unknown"), severity="hard",
                                  attrs={"pattern": m.get("pattern")}))
        sid = f"state:lagrangian:{name}"
        g.add_node(StateNode(node_id=sid, label=f"λ={lam:.2f}", state_type="soft_penalty",
                             severity="hard", attrs={"lagrangian": True},
                             evidence={"shortage": lam, "lambda": lam}))
        g.add_edge("requires", cid, sid)
        g.add_edge("pressures", sid, cid)
    return g


def diagnose_by_lagrangian(num_nurses, num_days, req, *, iters=8, **coupling):
    """편의: relaxable 로스터 → λ 추정 → 그래프. (λ dict, graph) 반환."""
    build, names, meta = relaxable_roster(num_nurses, num_days, req, **coupling)
    lam = estimate_multipliers(build, names, iters=iters)
    return lam, lagrangian_to_graph(lam, meta)


# 내부 constraint pattern → undiagnosed_probe RELAX_CATALOG 의 family (완화 레버가 있는 것만).
# coverage 는 완화 대상이 아니므로(수요=불변) 제외 → priority 는 '풀 수 있는' family 만 랭크.
_PATTERN_TO_CATALOG_FAMILY = {
    "off_budget": "off_budget",
    "night_cap": "night_cap",
    "night_recovery": "night_recovery",
    "transition_ban": "transition",
    "consecutive_work": "consecutive",
    "weekend_off": "weekend_off",       # → per-nurse MCS(주말휴무 해제)
    "monthly_limit": "monthly_limit",   # → per-nurse MCS(월 야간 한도)
}


def lambda_priority_families(
    num_nurses: int, num_days: int, req: dict[str, int], *,
    off_floor: int | None = None, night_cap: int | None = None,
    recovery: bool = False, ban_n2d: bool = False, max_consec: int | None = None,
    iters: int = 6, time_per_solve: float = 0.3,
) -> list[str]:
    """λ 로 랭크한 완화 우선 family 리스트 (probe_relaxations 의 priority_families 용).

    max-flow 가 못 보는 결합(OFF예산·야간cap·시퀀스)을 λ 로 실측해, 완화 레버가 있는
    family 를 쌍대압력 순으로 정렬한다. 이번 슬로우다운(off_budget 를 max-flow 가 놓쳐
    night_cap 오조준→전수폴백)을 바로잡는 신호.

    비용: 정확 λ 가 아니라 '순위'만 필요하므로 소수 iter + solve 시간캡으로 충분
    (probe 처럼 몇 번 가볍게 찔러 압력순위만 읽음). 순위는 iter 1~3 에서 이미 안정.
    """
    fam_mass = lambda_family_mass(
        num_nurses, num_days, req, off_floor=off_floor, night_cap=night_cap,
        recovery=recovery, ban_n2d=ban_n2d, max_consec=max_consec,
        iters=iters, time_per_solve=time_per_solve)
    return [f for f, _ in sorted(fam_mass.items(), key=lambda kv: -kv[1])]


def lambda_family_mass(
    num_nurses: int, num_days: int, req: dict[str, int], *,
    off_floor: int | None = None, night_cap: int | None = None,
    recovery: bool = False, ban_n2d: bool = False, max_consec: int | None = None,
    weekend_off_nurses: set[int] | None = None, weekend_days: set[int] | None = None,
    night_cap_by_nurse: dict[int, int] | None = None,
    iters: int = 6, time_per_solve: float = 0.3,
) -> dict[str, float]:
    """완화 family 별 λ 질량(쌍대압력 합). 랭킹·설명 공용."""
    from collections import defaultdict

    build, names, meta = relaxable_roster(
        num_nurses, num_days, req, off_floor=off_floor, night_cap=night_cap,
        recovery=recovery, ban_n2d=ban_n2d, max_consec=max_consec,
        weekend_off_nurses=weekend_off_nurses, weekend_days=weekend_days,
        night_cap_by_nurse=night_cap_by_nurse)
    lam = estimate_multipliers(build, names, iters=iters, time_per_solve=time_per_solve)
    fam_mass: dict[str, float] = defaultdict(float)
    for name, val in lam.items():
        cf = _PATTERN_TO_CATALOG_FAMILY.get(meta[name].get("pattern"))
        if cf and val > 1.0:      # 초기값 1.0 위로 실제 압력 받은 것만
            fam_mass[cf] += round(val, 3)
    return dict(fam_mass)


@dataclass
class InfeasibilityExplanation:
    classification: str                 # policy_overconstraint | coverage_shortage | coupled_sequence | unknown
    top_family: str | None              # λ 최대 압력 family
    lambda_by_family: dict[str, float]
    certificate: str                    # 사람이 읽는 '왜 해가 없나'
    arithmetic: dict[str, int]          # cells/coverage/off_budget/off_floor_sum/excess


def explain_infeasibility(
    num_nurses: int, num_days: int, req: dict[str, int], *,
    off_floor: int | None = None, night_cap: int | None = None,
    recovery: bool = False, ban_n2d: bool = False, max_consec: int | None = None,
    weekend_off_nurses: set[int] | None = None, weekend_days: set[int] | None = None,
    night_cap_by_nurse: dict[int, int] | None = None,
    iters: int = 6, max_model_days: int = 12,
) -> InfeasibilityExplanation:
    """해가 없어도 '왜 없는지' 설명. 결정론 arithmetic + λ 압력랭킹을 결합.

    미션: probe(fix 탐색)가 못 짚는 unrecoverable 에서도 원인을 낸다. 특히 정책 과제약
    (OFF 강제하한 > OFF 여유)을 인원부족과 구분해 '설정 탓'을 지목한다.
    """
    daily = sum(int(v or 0) for v in req.values())
    cells = num_nurses * num_days
    coverage = daily * num_days
    off_budget = cells - coverage
    off_floor_sum = num_nurses * int(off_floor) if off_floor else 0
    excess = off_floor_sum - off_budget
    arithmetic = {"cells": cells, "coverage_demand": coverage, "off_budget": off_budget,
                  "off_floor_sum": off_floor_sum, "excess": max(0, excess)}

    # ── 1) 결정론 arithmetic 우선 (cheap·exact, solve 불필요) ────────────────
    #   인원/셀 부족·정책 과제약은 닫힌형으로 즉시 판정. λ 는 여기서 washing 되므로 안 씀.
    if num_nurses < daily or off_budget < 0:
        cert = (f"인원 부족: 일 근무수요 {daily} > 간호사 {num_nurses}명"
                if num_nurses < daily else
                f"셀 부족: 근무수요 {coverage} > 총 근무셀 {cells}") + " — 자원부족(설정 아님)."
        return InfeasibilityExplanation("coverage_shortage", None, {}, cert, arithmetic)
    if off_floor and off_budget > 0 and excess > 0:
        cert = (f"정책 과제약: OFF 강제하한 {off_floor_sum}(={num_nurses}×{off_floor}) > "
                f"OFF 여유 {off_budget}(=총셀{cells}−근무수요{coverage}) → {excess} 초과. "
                f"인원 부족이 아니라 OFF 정책(off_floor/off_first)을 낮춰야 해소.")
        return InfeasibilityExplanation("policy_overconstraint", "off_budget", {}, cert, arithmetic)

    # ── 2) arithmetic clean → '배열 결합'만 남음. λ 로 시퀀스·weekend·한도 원인 지목 ──
    #   weekend-off/per-nurse 한도가 있으면 요일·개인 정체가 중요 → 압축 안 함(full days).
    _has_personal = bool(weekend_off_nurses) or bool(night_cap_by_nurse)
    days = int(num_days) if _has_personal else min(int(num_days), max_model_days)
    scale = days / float(num_days)
    nc = max(1, round(night_cap * scale)) if night_cap else None
    fam_mass = lambda_family_mass(
        num_nurses, days, req, off_floor=None, night_cap=nc,
        recovery=recovery, ban_n2d=ban_n2d, max_consec=max_consec,
        weekend_off_nurses=weekend_off_nurses,
        weekend_days=(weekend_days if _has_personal else None),
        night_cap_by_nurse=night_cap_by_nurse, iters=iters)
    top = max(fam_mass, key=fam_mass.get) if fam_mass else None
    if top in ("weekend_off", "monthly_limit", "night_cap"):
        # 개인 제약 병목 → per-nurse MCS(주말휴무 해제/월 야간 한도 완화) 경로.
        _ko = {"weekend_off": "주말 휴무", "monthly_limit": "월 야간 한도",
               "night_cap": "야간 상한"}[top]
        cert = (f"개인 제약 병목: '{_ko}'({top}) 압력 최대(λ={fam_mass.get(top)}). "
                f"특정 간호사의 {_ko}가 커버리지와 충돌 → 그 간호사 {_ko} 완화(per-nurse MCS)로 해소.")
        return InfeasibilityExplanation("personal_overconstraint", top, fam_mass, cert, arithmetic)
    if top:
        cert = (f"시퀀스 결합 충돌: '{top}' 압력 최대(λ={fam_mass.get(top)}). "
                f"인원·용량·OFF예산은 되나 배열 규칙(회복·연속·전이)이 서로 모순.")
        return InfeasibilityExplanation("coupled_sequence", top, fam_mass, cert, arithmetic)
    return InfeasibilityExplanation("unknown", None, {},
                                    "명확한 구조적 원인 미검출(개별 셀 충돌·복합 하드 모순 가능).", arithmetic)


def _daily_req(config: dict) -> dict[str, int]:
    dsr = config.get("daily_shift_requirements")
    if isinstance(dsr, dict) and dsr:
        return {s: int(dsr.get(s, 0) or 0) for s in WORK}
    return {"D": int(config.get("day_req", 0) or 0),
            "E": int(config.get("eve_req", 0) or 0),
            "N": int(config.get("nig_req", 0) or 0)}


def lambda_priority_from_config(nurses: list, config: dict, num_days: int,
                                *, iters: int = 6, max_model_days: int = 12) -> list[str]:
    """실 인스턴스(nurses+config) → λ 우선 완화 family. probe 콜사이트용.

    비용 억제: 긴 달은 대표 window(max_model_days)로 압축하고 off_floor/req 를 비례 축소해
    family 압력 랭킹만 뽑는다(정확 λ 가 아니라 '어느 레버가 먼저냐'가 목적).
    """
    n = len(nurses)
    if n == 0 or num_days <= 0:
        return []
    req = _daily_req(config)
    if sum(req.values()) <= 0:
        return []
    days = min(int(num_days), max_model_days)
    scale = days / float(num_days)
    off_raw = int(config.get("off_days") or config.get("standard_personal_off_days") or 0)
    off_floor = max(1, round(off_raw * scale)) if off_raw > 0 else None
    night_cap_raw = int(config.get("max_nig_per_month") or 0)
    night_cap = max(1, round(night_cap_raw * scale)) if night_cap_raw > 0 else None
    mc = config.get("max_conseq_work")
    return lambda_priority_families(
        n, days, req, off_floor=off_floor, night_cap=night_cap,
        recovery=bool(config.get("two_offs_after_two_nig")),
        ban_n2d=bool(config.get("ban_n_to_d", True)),
        max_consec=int(mc) if mc else None, iters=iters)


def _effective_off_floor(nurses: list, config: dict) -> int | None:
    """실제 강제 OFF 하한(간호사당). off_first 자동조정(auto_min)에 근접하도록 config
    off_days 를 기본으로, 없으면 standard_personal_off_days 사용."""
    off_raw = int(config.get("off_days") or config.get("standard_personal_off_days") or 0)
    return off_raw if off_raw > 0 else None


def _nurse_attr(nu, *keys):
    for k in keys:
        v = getattr(nu, k, None) if not isinstance(nu, dict) else nu.get(k)
        if v is not None:
            return v
    return None


def explain_infeasibility_from_config(nurses: list, config: dict, num_days: int,
                                      *, year: int | None = None, month: int | None = None,
                                      iters: int = 6) -> InfeasibilityExplanation:
    """실 인스턴스(nurses+config) → '왜 해가 없나' 설명. probe 게이팅용.

    weekend-off 간호사·per-nurse 야간한도를 nurses 에서 추출해 λ 모델에 넣는다
    (max-flow 가 못 보는 개인 제약 축 → personal_overconstraint 로 지목 → per-nurse MCS).
    """
    import calendar as _cal
    req = _daily_req(config)
    mc = config.get("max_conseq_work")
    max_nig = int(config.get("max_nig_per_month") or 0) or None
    off_floor = _effective_off_floor(nurses, config) or 0

    # weekend-off 간호사(인덱스) + 주말 day_idx
    wk_nurses = {i for i, nu in enumerate(nurses) if bool(_nurse_attr(nu, "is_weekend_off"))}
    wk_day_cnt = 0
    if year and month:
        wk_day_cnt = sum(1 for d in range(int(num_days))
                         if _cal.weekday(int(year), int(month), d + 1) >= 5)

    # ── 0) per-nurse 산술 모순 (λ·solver 불필요, 가장 명백 → 최우선) ────────────
    #   n_exact/n_min > max_nig(개인 야간 요구 > 상한), 강제 근무하한 합 > 가용 근무일
    #   (weekend-off 면 주말 제외). 이게 "13 > 7" 같은 즉시 infeasible 을 이름으로 짚는다.
    personal_conflicts: list[str] = []
    for i, nu in enumerate(nurses):
        nm = _nurse_attr(nu, "name") or _nurse_attr(nu, "nurse_id") or f"n{i}"
        n_floor = _nurse_attr(nu, "n_exact", "n_min")
        if n_floor is not None and max_nig is not None:
            try:
                if int(n_floor) > max_nig:
                    personal_conflicts.append(
                        f"{nm}: 야간 요구 {int(n_floor)} > 월 상한 {max_nig}")
                    continue
            except (TypeError, ValueError):
                pass
        # 강제 근무하한 합 vs 가용 근무일(주말휴무면 주말 제외 + off_floor 차감)
        floor_sum = 0
        for s in ("d", "e", "n"):
            v = _nurse_attr(nu, f"{s}_exact", f"{s}_min")
            try:
                floor_sum += int(v) if v is not None else 0
            except (TypeError, ValueError):
                pass
        avail = int(num_days) - off_floor - (wk_day_cnt if i in wk_nurses else 0)
        if floor_sum > 0 and floor_sum > avail:
            _wk = " (주말휴무)" if i in wk_nurses else ""
            personal_conflicts.append(
                f"{nm}{_wk}: 강제 근무 {floor_sum} > 가용 근무일 {avail}")

    if personal_conflicts:
        cert = ("개인 제약 즉시 모순(집합 무관, 그 간호사 혼자서도 불가): "
                + "; ".join(personal_conflicts[:6])
                + " → 해당 간호사의 야간 요구/근무 하한 또는 상한을 조정해야 함.")
        return InfeasibilityExplanation(
            "personal_infeasible", "monthly_limit", {}, cert,
            {"personal_conflicts": len(personal_conflicts)})
    wk_days: set[int] | None = None
    if wk_nurses and year and month:
        wk_days = {d for d in range(int(num_days))
                   if _cal.weekday(int(year), int(month), d + 1) >= 5}
    # per-nurse 야간 한도(n_exact/n_max) — config 전역보다 개인이 우선
    ncap_by: dict[int, int] = {}
    for i, nu in enumerate(nurses):
        v = _nurse_attr(nu, "n_exact", "n_max")
        try:
            if v is not None and int(v) >= 0:
                ncap_by[i] = int(v)
        except (TypeError, ValueError):
            pass

    return explain_infeasibility(
        len(nurses), int(num_days), req,
        off_floor=_effective_off_floor(nurses, config),
        night_cap=int(config.get("max_nig_per_month") or 0) or None,
        recovery=bool(config.get("two_offs_after_two_nig")),
        ban_n2d=bool(config.get("ban_n_to_d", True)),
        max_consec=int(mc) if mc else None,
        weekend_off_nurses=(wk_nurses or None), weekend_days=wk_days,
        night_cap_by_nurse=(ncap_by or None), iters=iters)
