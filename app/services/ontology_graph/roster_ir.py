"""Canonical Constraint IR — 하나의 명세에서 두 backend(graph·CP-SAT)를 컴파일.

피드백 핵심: 같은 업무 규칙을 CP-SAT 코드와 graph 코드에 **손으로 두 번** 구현하면 "솔버를
또 만들었나" 비판을 부른다. 대신 규칙을 IR 로 **한 번만** 정의하고, 각 backend compiler 가
그 IR 을 해석한다. 규칙 추가 = IR Rule 하나 정의 → compiler 들이 해석.

범위(최소): 현재 exact 지원 제약만 IR 로.
  NightRecoveryRule · NotOneNightRule · MaxConsecutiveNightsRule · MaxConsecutiveWorkRule ·
  TransitionBanRule · CoverageRule · CellDomainRule · NurseSpec

parse_to_ir(nurses, config)  → RosterConstraintIR (미지원은 unsupported 에 격리)
compile_graph(ir)            → (nurses, config)  그래프 엔진 입력
compile_cpsat(ir)            → bool | None        CP-SAT feasibility (독립 backend)

differential test: 같은 IR 에서 graph·CP-SAT·oracle 결과가 일치해야(불일치=인코딩/변환 버그).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.ontology_graph.lagrangian import _nurse_attr
from services.ontology_graph.scope_manifest import unmodeled_active


# ── IR 규칙 (frozen; 파라미터를 한 곳에서만 관리) ───────────────────────────────
@dataclass(frozen=True)
class NurseSpec:
    nurse_id: str
    allowed: frozenset          # {} = 전부(D/E/N), 아니면 subset


@dataclass(frozen=True)
class NightRecoveryRule:
    trigger_run: int            # 이 길이 이상 야간 run 이면
    required_off: int           # 이만큼 실제 OFF 필요


@dataclass(frozen=True)
class NotOneNightRule:
    pass                        # 고립 1야간 금지(min run 2)


@dataclass(frozen=True)
class MaxConsecutiveNightsRule:
    limit: int


@dataclass(frozen=True)
class MaxConsecutiveWorkRule:
    limit: int


@dataclass(frozen=True)
class TransitionBanRule:
    from_shift: str
    to_shift: str


@dataclass(frozen=True)
class CoverageRule:
    reqD: int
    reqE: int
    reqN: int                   # per-day 균일 커버리지(현재 모델)


@dataclass(frozen=True)
class CellDomainRule:
    nurse_id: str
    day: int
    banned: frozenset           # 금지 코드(O=강제근무, N=N금지 등)
    forced_off: bool


@dataclass
class RosterConstraintIR:
    nurses: list                # NurseSpec
    days: int
    rules: list                 # 위 Rule 들
    unsupported: list = field(default_factory=list)


def parse_to_ir(nurses: list, config: dict, num_days: int) -> RosterConstraintIR:
    """운영 config/nurses → IR. 지원 제약만 Rule 로, 미지원은 unsupported 로 격리."""
    specs = []
    for nu in nurses:
        allowed = {str(x).strip().upper() for x in (_nurse_attr(nu, "allowed_shifts") or [])}
        specs.append(NurseSpec(str(_nurse_attr(nu, "nurse_id")),
                               frozenset(allowed & {"D", "E", "N"})))
    rules: list = []
    if config.get("two_offs_after_three_nig"):
        rules.append(NightRecoveryRule(3, 2))
    if config.get("two_offs_after_two_nig"):
        rules.append(NightRecoveryRule(2, 2))
    if config.get("not_one_night"):
        rules.append(NotOneNightRule())
    mcn = config.get("max_consecutive_nights") or config.get("max_consec_nights")
    if mcn:                                          # 명시된 경우만(미설정=엔진 기본 6, 주입 금지)
        rules.append(MaxConsecutiveNightsRule(int(mcn)))
    if config.get("max_consecutive_work"):
        rules.append(MaxConsecutiveWorkRule(int(config["max_consecutive_work"])))
    if config.get("forbid_night_to_day"):
        rules.append(TransitionBanRule("N", "D"))
    dsr = config.get("daily_shift_requirements") or {}
    rules.append(CoverageRule(int(dsr.get("D", 0) or 0), int(dsr.get("E", 0) or 0),
                              int(dsr.get("N", 0) or int(config.get("nig_req", 0) or 0))))
    ic = config.get("initial_constraints") or {}
    fb, fo = ic.get("forbidden") or {}, ic.get("forced_off") or {}
    cells: dict = {}
    for nid, dm in fb.items():
        for d, codes in dm.items():
            cells[(str(nid), int(d))] = (frozenset(str(c).strip().upper() for c in (codes or [])),
                                         cells.get((str(nid), int(d)), (frozenset(), False))[1])
    for nid, days in fo.items():
        for d in days:
            b, _ = cells.get((str(nid), int(d)), (frozenset(), False))
            cells[(str(nid), int(d))] = (b, True)
    for (nid, d), (banned, foff) in cells.items():
        rules.append(CellDomainRule(nid, d, banned, foff))
    return RosterConstraintIR(specs, num_days, rules,
                              unsupported=unmodeled_active(nurses, config))


# ── Backend 1: IR → 그래프 엔진 입력 ────────────────────────────────────────
def compile_graph(ir: RosterConstraintIR):
    """IR → (nurses, config). 그래프 엔진(diagnose_frontier/solve_hybrid)이 소비."""
    config: dict = {"daily_shift_requirements": {"D": 0, "E": 0, "N": 0}}
    fb: dict = {}
    fo: dict = {}
    for r in ir.rules:
        if isinstance(r, NightRecoveryRule):
            config["two_offs_after_three_nig" if r.trigger_run >= 3
                   else "two_offs_after_two_nig"] = True
        elif isinstance(r, NotOneNightRule):
            config["not_one_night"] = True
        elif isinstance(r, MaxConsecutiveNightsRule):
            config["max_consecutive_nights"] = r.limit
        elif isinstance(r, MaxConsecutiveWorkRule):
            config["max_consecutive_work"] = r.limit
        elif isinstance(r, TransitionBanRule) and (r.from_shift, r.to_shift) == ("N", "D"):
            config["forbid_night_to_day"] = True
        elif isinstance(r, CoverageRule):
            config["daily_shift_requirements"] = {"D": r.reqD, "E": r.reqE, "N": r.reqN}
        elif isinstance(r, CellDomainRule):
            if r.banned:
                fb.setdefault(r.nurse_id, {})[r.day] = sorted(r.banned)
            if r.forced_off:
                fo.setdefault(r.nurse_id, []).append(r.day)
    config["initial_constraints"] = {"forbidden": fb, "forced_off": fo}
    nurses = [{"nurse_id": s.nurse_id, "name": s.nurse_id, "grade": 1, "team_id": "A",
               **({"allowed_shifts": sorted(s.allowed)} if s.allowed else {})}
              for s in ir.nurses]
    return nurses, config


# ── Backend 2: IR → CP-SAT (독립 backend, IR 규칙을 직접 해석) ────────────────
def compile_cpsat(ir: RosterConstraintIR, time_limit: float = 5.0):
    """IR → CP-SAT feasibility. 규칙 객체를 직접 읽어 모델 생성(파라미터 재정의 없음)."""
    try:
        from ortools.sat.python import cp_model
    except Exception:
        return None
    days = ir.days
    ids = [s.nurse_id for s in ir.nurses]
    work_of = {s.nurse_id: (s.allowed or frozenset({"D", "E", "N"})) for s in ir.nurses}
    cov = next((r for r in ir.rules if isinstance(r, CoverageRule)), CoverageRule(0, 0, 0))
    recs = [r for r in ir.rules if isinstance(r, NightRecoveryRule)]
    # max_run 은 _night_rules 와 동일 유도: 회복규칙 trigger_run + 명시 MaxConsecutiveNights 의 최소
    caps = [r.trigger_run for r in recs]
    caps += [r.limit for r in ir.rules if isinstance(r, MaxConsecutiveNightsRule)]
    max_run = min(caps) if caps else 6
    min_run = 2 if any(isinstance(r, NotOneNightRule) for r in ir.rules) else 1
    off_after = max((r.required_off for r in recs), default=1) if recs else 1
    maxw = next((r.limit for r in ir.rules if isinstance(r, MaxConsecutiveWorkRule)), None)
    ntod = any(isinstance(r, TransitionBanRule) and (r.from_shift, r.to_shift) == ("N", "D")
               for r in ir.rules)
    cells = {(r.nurse_id, r.day): r for r in ir.rules if isinstance(r, CellDomainRule)}

    m = cp_model.CpModel()
    x = {}
    for i in ids:
        for d in range(days):
            row = {}
            cell = cells.get((i, d))
            banned = cell.banned if cell else frozenset()
            for s in ("D", "E", "N", "O"):
                v = m.NewBoolVar(f"x_{i}_{d}_{s}")
                row[s] = v
                if s in ("D", "E", "N") and s not in work_of[i]:
                    m.Add(v == 0)
                if s in banned:
                    m.Add(v == 0)
                if s == "O" and "O" in banned:
                    m.Add(v == 0)
            if cell and cell.forced_off:
                m.Add(row["O"] == 1)
            m.Add(sum(row.values()) == 1)
            x[i, d] = row
    for d in range(days):
        m.Add(sum(x[i, d]["D"] for i in ids) >= cov.reqD)
        m.Add(sum(x[i, d]["E"] for i in ids) >= cov.reqE)
        m.Add(sum(x[i, d]["N"] for i in ids) >= cov.reqN)
    for i in ids:
        N = [x[i, d]["N"] for d in range(days)]
        D = [x[i, d]["D"] for d in range(days)]
        O = [x[i, d]["O"] for d in range(days)]
        for d in range(days - max_run):
            m.Add(sum(N[d:d + max_run + 1]) <= max_run)
        if min_run >= 2:
            for d in range(1, days - 1):
                m.AddBoolOr([N[d].Not(), N[d - 1], N[d + 1]])
        for d in range(days - 1):
            end = m.NewBoolVar(f"end_{i}_{d}")
            m.Add(N[d] - N[d + 1] == 1).OnlyEnforceIf(end)
            m.Add(N[d] - N[d + 1] <= 0).OnlyEnforceIf(end.Not())
            for j in range(1, off_after + 1):
                if d + j < days:
                    m.Add(O[d + j] == 1).OnlyEnforceIf(end)
        if ntod:
            for d in range(days - 1):
                m.Add(N[d] + D[d + 1] <= 1)
        if maxw:
            for d in range(days - maxw):
                m.Add(sum(1 - O[e] for e in range(d, d + maxw + 1)) <= maxw)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 1
    st = solver.Solve(m)
    if st in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        return True
    if st == cp_model.INFEASIBLE:
        return False
    return None
