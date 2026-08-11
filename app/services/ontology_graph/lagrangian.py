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

from dataclasses import dataclass, field
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
    targets: list[dict] = field(default_factory=list)  # 행동카드용: [{nurse_id,name,family,detail}]


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
        cert = (f"간호사가 부족해요 (하루 {daily}명 필요, {num_nurses}명)"
                if num_nurses < daily else
                "근무 요구가 너무 많아요") + " — 인원을 늘리거나 필요 인원을 줄이세요."
        return InfeasibilityExplanation("coverage_shortage", None, {}, cert, arithmetic)
    if off_floor and off_budget > 0 and excess > 0:
        cert = "쉬는 날 요구가 많아 근무를 다 못 채워요 — 월 OFF 일수를 줄이면 돼요."
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
        cert = f"특정 간호사의 {_ko}가 근무와 겹쳐 안 돼요 — 그 간호사 {_ko}를 조정하세요."
        return InfeasibilityExplanation("personal_overconstraint", top, fam_mass, cert, arithmetic)
    if top:
        cert = "근무 규칙(야간 회복·연속·전이)이 서로 겹쳐 배치가 안 돼요 — 규칙 하나를 완화하세요."
        return InfeasibilityExplanation("coupled_sequence", top, fam_mass, cert, arithmetic)
    return InfeasibilityExplanation(
        "unknown", None, {},
        "지금 설정 조합으로는 만들 수 없어요 — 여러 규칙이 얽혀 있어요. 아래 항목을 확인하세요.", arithmetic)


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


def _night_rules(config: dict) -> tuple[int, int, int]:
    """야간 시퀀스 규칙 → (최대연속N, 회복트리거런, 최소런). 전이함수의 유일한 규칙 출처.

    · 최대연속N: 3N2OFF→3, 2N2OFF→2, max_consecutive_nights 중 최소.
    · 회복트리거런: run 이 이 값 이상으로 끝나면 **2 OFF** 필요(2N2OFF→2, 3N2OFF→3).
    · 최소런: not_one_night 이면 2(고립 1N 금지), 아니면 1.
    """
    two2 = bool(config.get("two_offs_after_two_nig"))
    three2 = bool(config.get("two_offs_after_three_nig"))
    caps: list[int] = []
    if three2:
        caps.append(3)
    if two2:
        caps.append(2)
    for k in ("max_consecutive_nights", "max_consec_nights"):
        v = config.get(k)
        try:
            if v is not None and int(v) > 0:
                caps.append(int(v))
        except (TypeError, ValueError):
            pass
    max_run = min(caps) if caps else 6
    recovery_trigger = 2 if two2 else (3 if three2 else 99)   # run>=trigger → 2 OFF
    min_run = 2 if bool(config.get("not_one_night")) else 1
    return max_run, recovery_trigger, min_run


def per_nurse_night_feasible(banned_off: set, forced_off: set, num_days: int,
                             config: dict) -> bool:
    """N전담 1명의 {N,O} 사슬에 **유효 배열이 존재하는가** — 상태 전이 그래프 도달성.

    노드 상태 = (r=현재 N 연속길이, k=회복 OFF 잔여 0/1). 엣지 = 하루 전이.
    강제: banned-OFF→그날 N만, forced_off→그날 O만. 규칙(_night_rules)은 전이 제약으로만.
    경로가 하나도 없으면 그 간호사는 개인 infeasible(증명). ①연속초과 ②회복막힘 ③고립1N
    및 미래 패턴을 **열거 없이** 한 번에 판정한다.
    """
    max_run, rec_trig, min_run = _night_rules(config)
    banned = {int(d) for d in banned_off}
    forced = {int(d) for d in forced_off}
    states: set[tuple[int, int]] = {(0, 0)}      # (r, k)
    for d in range(int(num_days)):
        must_n = d in banned                     # O 금지 → 반드시 N
        must_o = d in forced                     # 강제 OFF → 반드시 O
        if must_n and must_o:
            return False                         # 셀 자체 모순
        nxt: set[tuple[int, int]] = set()
        for (r, k) in states:
            if not must_n:                       # O 선택
                if r == 0:
                    nxt.add((0, max(0, k - 1)))  # 회복 소진
                elif r >= min_run:               # 런 종료(고립 아님)
                    nxt.add((0, 1 if r >= rec_trig else 0))
                # r < min_run → 고립 N → 무효(스킵)
            if not must_o:                       # N 선택
                if k == 0 and r + 1 <= max_run:  # 회복빚 없고 연속상한 이내
                    nxt.add((r + 1, 0))
        if not nxt:
            return False                         # 어느 상태도 진행 불가 → infeasible
        states = nxt
    return bool(states)                          # 월말 열린 회복빚은 허용(경계)


def detect_banned_off_conflict(nurses: list, config: dict, num_days: int) -> list[dict]:
    """금지 원티드(banned-OFF) 개인 모순 — per-nurse 상태DAG 도달성(솔버 불필요).

    banned_wanted 로 OFF 가 금지된 날 = **강제근무**. N전담(allowed={N})이면 그 날 반드시 N.
      · 셀 모순(clash): banned-OFF ∩ forced_off (N전담 아니어도 물리 모순).
      · 시퀀스 모순: N전담의 {N,O} 배열에 유효 경로 없음(연속·회복·고립 패턴 전부).
    post-solve 진단이라 오탐이 표 생성을 막지 않는다.

    반환: [{nurse_id, name, family, banned_off_days, forced_off_days, clash_days,
            allowed_shifts, is_night_only, reason}]  (모순 간호사만).
    """
    ic = config.get("initial_constraints") or {}
    fo = ic.get("forced_off") or {}
    fb = ic.get("forbidden") or {}
    by_nid = {str(_nurse_attr(nu, "nurse_id")): nu for nu in nurses}
    out: list[dict] = []
    for nid, day_map in (fb or {}).items():
        banned_off = {int(d) for d, codes in (day_map or {}).items()
                      if "O" in {str(c).strip().upper() for c in (codes or [])}}
        if not banned_off:
            continue
        nu = by_nid.get(str(nid))
        allowed = [str(x).strip().upper() for x in (_nurse_attr(nu, "allowed_shifts") or [])]
        work_opts = (set(allowed) & {"D", "E", "N"}) if allowed else {"D", "E", "N"}
        n_only = work_opts == {"N"}
        forced_off = {int(d) for d in (fo.get(nid) or [])}
        clash = sorted(banned_off & forced_off)
        seq_bad = n_only and not per_nurse_night_feasible(banned_off, forced_off, num_days, config)
        if not (clash or seq_bad):
            continue
        out.append({
            "nurse_id": str(nid), "name": _nurse_attr(nu, "name") or str(nid),
            "family": "banned_wanted", "banned_off_days": sorted(banned_off),
            "forced_off_days": sorted(forced_off), "clash_days": clash,
            "allowed_shifts": sorted(work_opts), "is_night_only": n_only,
            "reason": "clash" if clash else "sequence_infeasible",
        })
    return out


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

    # ── 0a) 금지근무(banned-OFF) × 강제OFF 모순 (증명, 최우선) ──────────────────
    #   OFF 금지(강제근무)가 야간회복 강제OFF 와 겹치면 근무∧휴무 동시 → 물리적 모순.
    #   N전담이 대표 사례. 전역 규칙으론 못 고침 → 그 간호사 금지해제/근무유형추가로 유도.
    _bw = detect_banned_off_conflict(nurses, config, num_days)
    if _bw:
        def _bwlabel(t):
            days = t.get("clash_days") or t.get("banned_off_days") or []
            return f"{t['name']} {days[0] + 1}일" if days else t["name"]
        _bnames = "; ".join(_bwlabel(t) for t in _bw[:3])
        _bmore = f" 외 {len(_bw) - 3}명" if len(_bw) > 3 else ""
        cert = (f"{_bnames}{_bmore} — N전담 간호사에게 금지된 근무(OFF 금지)가 몰려 "
                f"야간 회복 규칙을 지킬 배열이 없어요. 그 간호사 금지근무를 풀거나 "
                f"근무유형(D/E)을 추가하면 돼요.")
        return InfeasibilityExplanation(
            "personal_infeasible", "banned_wanted", {}, cert,
            {"banned_conflicts": len(_bw)}, targets=_bw)

    # ── 0) per-nurse 산술 모순 (λ·solver 불필요, 가장 명백 → 최우선) ────────────
    #   n_exact/n_min > max_nig(개인 야간 요구 > 상한), 강제 근무하한 합 > 가용 근무일
    #   (weekend-off 면 주말 제외). 이게 "13 > 7" 같은 즉시 infeasible 을 이름으로 짚는다.
    personal_conflicts: list[str] = []
    personal_targets: list[dict] = []
    for i, nu in enumerate(nurses):
        nid = _nurse_attr(nu, "nurse_id")
        nm = _nurse_attr(nu, "name") or nid or f"n{i}"
        n_floor = _nurse_attr(nu, "n_exact", "n_min")
        if n_floor is not None and max_nig is not None:
            try:
                if int(n_floor) > max_nig:
                    _al = [str(x).strip().upper() for x in (_nurse_attr(nu, "allowed_shifts") or [])]
                    _n_only = ((set(_al) & {"D", "E", "N"}) == {"N"}) if _al else False
                    _msg = f"{nm} 야간 {int(n_floor)}회 필요, 상한 {max_nig}회"
                    _tgt = {
                        "nurse_id": nid, "name": nm, "family": "monthly_limit",
                        "detail": f"야간 요구 {int(n_floor)} > 월 상한 {max_nig}",
                        "current": int(n_floor), "cap": max_nig}
                    if _n_only:
                        # 야간전담은 off=일수−나이트. 상한으로 낮추면 나머지가 강제 OFF 가 되어
                        # 그만큼 야간 공급이 줄어 되레 커버리지가 악화될 수 있다(카드가 알도록 표식).
                        _tgt["is_night_only"] = True
                        _tgt["off_after_cap"] = int(num_days) - int(max_nig)
                        _msg += f"(야간전담 — 상한으로 낮추면 {int(num_days) - int(max_nig)}일 강제 OFF)"
                    personal_conflicts.append(_msg)
                    personal_targets.append(_tgt)
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
            _wk = "(주말휴무)" if i in wk_nurses else ""
            personal_conflicts.append(
                f"{nm}{_wk} 근무 {floor_sum}일 필요, 가능 {avail}일")
            personal_targets.append({
                "nurse_id": nid, "name": nm,
                "family": ("weekend_off" if i in wk_nurses else "monthly_limit"),
                "detail": f"강제 근무 {floor_sum} > 가용 근무일 {avail}"})

    if personal_conflicts:
        _more = f" 외 {len(personal_conflicts) - 3}명" if len(personal_conflicts) > 3 else ""
        # 야간전담이 끼면 '내리기'가 아니라 '월 야간 상한 올리기'가 정답이라 문구도 그에 맞춘다.
        _has_no = any(t.get("is_night_only") for t in personal_targets)
        _suffix = (" — 야간전담이라 월 야간 상한을 올려 그만큼 서게 하면 돼요."
                   if _has_no else " — 그 간호사 설정만 바꾸면 돼요.")
        cert = "; ".join(personal_conflicts[:3]) + _more + _suffix
        return InfeasibilityExplanation(
            "personal_infeasible", "monthly_limit", {}, cert,
            {"personal_conflicts": len(personal_conflicts)}, targets=personal_targets)

    # ── 0c) 주말 커버리지 집계 (주말휴무자 과다 → 주말 요일 공급 부족, arithmetic 증명) ──
    #   주말휴무자는 주말 강제OFF. 주말 하루 가용 = 전체 − 주말휴무자. 이게 주말 요구(daily)보다
    #   적으면 그 주말은 못 채운다(필요조건, sound). 값싼 산술(전체 인원만 봄)의 사각지대를 메운다.
    #   presolve max-flow 가 계산하던 신호를 사용자 원인으로 승격. year/month 없으면 주말 미식별로 skip.
    _daily_req_sum = sum(int(v or 0) for v in req.values())
    if wk_nurses and wk_day_cnt > 0 and _daily_req_sum > 0:
        _avail_wk = len(nurses) - len(wk_nurses)
        if _avail_wk < _daily_req_sum:
            _need = _daily_req_sum - _avail_wk       # 주말 매일 +1 공급 위해 풀 최소 인원
            # 과해제 방지로 필요 인원만 담되, 누구를 담을지에 근거를 준다.
            #   ★ 예전엔 sorted(인덱스)[:n] 이라 **간호사 목록에 실린 순서**로 앞에서
            #     잘랐다(연차·업무량과 무관). probe 가 재solve 로 culprit 을 찾던 자리를
            #     대신하는 값이라, 최소한 "주말 커버에 쓸모가 큰 순"으로 정렬한다.
            #   설 수 있는 근무 코드가 많은 사람 → 해제 효과가 크다. 동률은 nurse_id 로
            #   고정해 실행마다 답이 흔들리지 않게 한다(결정론).
            def _wk_rank(i: int) -> tuple:
                _al = _nurse_attr(nurses[i], "allowed_shifts") or []
                return (-len(list(_al)), str(_nurse_attr(nurses[i], "nurse_id") or i))

            _rel_idxs = sorted(wk_nurses, key=_wk_rank)[:_need]
            _wk_tgts = [{
                "nurse_id": _nurse_attr(nurses[i], "nurse_id"),
                "name": _nurse_attr(nurses[i], "name") or _nurse_attr(nurses[i], "nurse_id") or f"n{i}",
                "family": "weekend_off",
                # 이 선택은 '제안'이다. 실제 누구를 풀지는 수간호사가 정한다.
                "selection": "suggested"} for i in _rel_idxs]
            cert = (f"주말 휴무자가 많아 주말 근무를 못 채워요 "
                    f"(주말 하루 {_daily_req_sum}명 필요, 가용 {_avail_wk}명) — "
                    f"주말 휴무를 최소 {_need}명 해제하면 돼요.")
            return InfeasibilityExplanation(
                "coverage_shortage", "weekend_off", {}, cert,
                {"weekend_demand": _daily_req_sum, "weekend_available": _avail_wk,
                 "release_needed": _need}, targets=_wk_tgts)

    # ── 0d) 일자별 가동 팀 (그날 안 도는 팀을 빼면 가용 < 요구, arithmetic 증명) ──
    #   비가동 팀 인원은 그날 강제 OFF 다. 그날 쓸 수 있는 사람 = 가동 팀 소속 + 팀 미배정.
    #   이게 그날 요구 합보다 적으면 그 날은 못 채운다(필요조건, sound).
    #   주말휴무(0c)와 같은 축이지만 원인·해법이 달라 따로 짚는다 — 여기서 뭉개면
    #   "주말 휴무를 푸세요" 같은 엉뚱한 안내가 나간다.
    _daily_active = config.get("daily_active_teams_by_day")
    if _daily_active:
        _by_day = config.get("daily_shift_requirements_by_day")
        for _d in range(int(num_days)):
            if _d >= len(_daily_active):
                break
            _act = _daily_active[_d]
            if _act is None:
                continue  # 미설정 = 전 팀 가동
            _act_set = {str(t) for t in _act}
            _avail = [
                i for i, nu in enumerate(nurses)
                if (lambda t: t in (None, "", 0) or str(t) in _act_set)(
                    _nurse_attr(nu, "team_id"))
            ]
            if isinstance(_by_day, list) and _d < len(_by_day) and _by_day[_d]:
                _need_d = sum(int(v or 0) for v in (_by_day[_d] or {}).values())
            else:
                _need_d = sum(int(v or 0) for v in req.values())
            if _need_d <= 0 or len(_avail) >= _need_d:
                continue
            _off_teams = sorted(
                {str(_nurse_attr(nurses[i], "team_id"))
                 for i in range(len(nurses))
                 if _nurse_attr(nurses[i], "team_id") not in (None, "", 0)
                 and str(_nurse_attr(nurses[i], "team_id")) not in _act_set}
            )
            _tgts = [{
                "nurse_id": _nurse_attr(nurses[i], "nurse_id"),
                "name": _nurse_attr(nurses[i], "name") or _nurse_attr(nurses[i], "nurse_id"),
                "family": "daily_team",
                "day": _d + 1,
                "team_id": str(_nurse_attr(nurses[i], "team_id")),
            } for i in range(len(nurses))
                if _nurse_attr(nurses[i], "team_id") not in (None, "", 0)
                and str(_nurse_attr(nurses[i], "team_id")) not in _act_set]
            cert = (f"{_d + 1}일에 가동하도록 지정한 팀만으로는 근무를 못 채워요 "
                    f"(필요 {_need_d}명, 그날 근무 가능 {len(_avail)}명) — "
                    f"쉬게 한 팀({', '.join(_off_teams)}) 중 하나를 그날 다시 가동하면 돼요.")
            return InfeasibilityExplanation(
                "coverage_shortage", "daily_team", {}, cert,
                {"day": _d + 1, "need": _need_d, "available": len(_avail),
                 "inactive_teams": _off_teams}, targets=_tgts)

    # ── 0b) 다인 N-커버리지 결합 (조인트 frontier DP, bounded-treewidth) ──────────
    #   "각자는 되는데 같이는 야간을 못 채움" — 단일 상태DAG 로는 못 보는 결합. N-pool 이
    #   작을 때 정확 판정(sound). infeasible 이면 병목 간호사까지 격리한다.
    try:
        from services.ontology_graph.joint_coverage import detect_joint_night_infeasible
        _jc = detect_joint_night_infeasible(nurses, config, int(num_days))
    except Exception:
        _jc = None
    if _jc:
        _cn = ", ".join(c["name"] for c in _jc["culprits"][:3])
        cert = (f"야간 가능 인원 {len(_jc['pool_ids'])}명으로는 하루 야간 {_jc['demand_n']}명을 "
                f"매일 채울 수 없어요(회복 규칙상 동시에 다 못 섬). 야간 가능 인원을 늘리거나 "
                f"야간 필요 인원을 줄이면 돼요." + (f" [병목: {_cn}]" if _cn else ""))
        return InfeasibilityExplanation(
            "coverage_shortage", "night_coverage", {}, cert,
            {"n_pool": len(_jc["pool_ids"]), "demand_n": _jc["demand_n"]},
            targets=_jc["culprits"])

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

    exp = explain_infeasibility(
        len(nurses), int(num_days), req,
        off_floor=_effective_off_floor(nurses, config),
        night_cap=int(config.get("max_nig_per_month") or 0) or None,
        recovery=bool(config.get("two_offs_after_two_nig")),
        ban_n2d=bool(config.get("ban_n_to_d", True)),
        max_consec=int(mc) if mc else None,
        weekend_off_nurses=(wk_nurses or None), weekend_days=wk_days,
        night_cap_by_nurse=(ncap_by or None), iters=iters)

    # personal_overconstraint(λ가 개인 family 지목) → 해당 간호사들을 target 으로 채움
    if exp.classification == "personal_overconstraint" and not exp.targets:
        def _tg(i):
            return {"nurse_id": _nurse_attr(nurses[i], "nurse_id"),
                    "name": _nurse_attr(nurses[i], "name") or _nurse_attr(nurses[i], "nurse_id") or f"n{i}",
                    "family": exp.top_family}
        idxs = wk_nurses if exp.top_family == "weekend_off" else set(ncap_by)
        exp.targets = [_tg(i) for i in sorted(idxs)]
    return exp
