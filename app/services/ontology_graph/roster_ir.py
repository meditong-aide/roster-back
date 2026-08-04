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
실측(242건): round-trip 0 · graph⟷oracle 0 · **CP-SAT⟷oracle 0**(공통 automaton 으로 야간
시퀀스 의미 exact 일치). 이 differential 이 실제 코어 버그(회복빚+강제OFF `_options` 오사망)를
발견·수정하게 함.
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


def model_signature(nurses: list, config: dict, num_days: int) -> str:
    """모델 **구조** 시그니처 — 활성 hard rule 종류+핵심 파라미터+horizon 의 해시.

    CellDomain(개인 셀 제약)은 입력이므로 제외(input_hash 가 담당). 이 시그니처는 "graph 와
    production 이 같은 hard model 을 비교하는가"를 확인하는 데 쓴다(다르면 false-cert 확정 불가).
    """
    import hashlib
    ir = parse_to_ir(nurses, config, num_days)
    parts = [f"days={num_days}"]
    for r in sorted((r for r in ir.rules if not isinstance(r, CellDomainRule)),
                    key=lambda x: type(x).__name__):
        fields = {k: v for k, v in vars(r).items()}
        parts.append(type(r).__name__ + ":" + repr(sorted(fields.items())))
    # 간호사 work-set 프로필(정렬 멀티셋) — 야간전담 등 공급구조도 모델 일부
    works = sorted(repr(sorted(s.allowed)) for s in ir.nurses)
    parts.append("works=" + repr(works))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


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


# ── Backend 2: IR → CP-SAT — 공통 shift automaton(AddAutomaton)으로 exact 컴파일 ──
def compile_cpsat(ir: RosterConstraintIR, time_limit: float = 5.0):
    """IR → CP-SAT feasibility. 야간 시퀀스는 graph 와 **동일한 공통 automaton**(build_shift_
    automaton, _options/_step 기반)을 AddAutomaton 으로 컴파일 → 두 backend 의미 exact 일치.
    커버리지는 label 채널링으로 부과. 미지원 규칙은 IR unsupported 로 이미 격리됨.
    """
    try:
        from ortools.sat.python import cp_model
    except Exception:
        return None
    from services.ontology_graph.frontier_dp import build_shift_automaton

    nurses, config = compile_graph(ir)                 # IR → 엔진 config(automaton 유도용)
    _, triples, start, finals = build_shift_automaton(config)
    L = {"D": 0, "E": 1, "N": 2, "O": 3}
    days = ir.days
    specs = {s.nurse_id: (s.allowed or frozenset({"D", "E", "N"})) for s in ir.nurses}
    cells = {(r.nurse_id, r.day): r for r in ir.rules if isinstance(r, CellDomainRule)}
    cov = next((r for r in ir.rules if isinstance(r, CoverageRule)), CoverageRule(0, 0, 0))

    m = cp_model.CpModel()
    lab: dict = {}
    isN: dict = {}
    isD: dict = {}
    isE: dict = {}
    for s in ir.nurses:
        i = s.nurse_id
        labels = []
        for d in range(days):
            lv = m.NewIntVar(0, 3, f"lab_{i}_{d}")
            # per-cell 허용 label(work·banned·forced_off) 제한
            cell = cells.get((i, d))
            banned = cell.banned if cell else frozenset()
            if cell and cell.forced_off:
                allowed = {"O"}
            else:
                allowed = {c for c in ("D", "E", "N") if c in specs[i] and c not in banned}
                if "O" not in banned:
                    allowed.add("O")
            for c in ("D", "E", "N", "O"):
                if c not in allowed:
                    m.Add(lv != L[c])
            labels.append(lv)
            bN = m.NewBoolVar(f"N_{i}_{d}")
            bD = m.NewBoolVar(f"D_{i}_{d}")
            bE = m.NewBoolVar(f"E_{i}_{d}")
            for b, val in ((bN, 2), (bD, 0), (bE, 1)):
                m.Add(lv == val).OnlyEnforceIf(b)
                m.Add(lv != val).OnlyEnforceIf(b.Not())
            isN[i, d], isD[i, d], isE[i, d] = bN, bD, bE
        m.AddAutomaton(labels, start, finals, triples)   # 공통 automaton = 야간 시퀀스 exact
        lab[i] = labels
    for d in range(days):                                # 커버리지
        m.Add(sum(isD[s.nurse_id, d] for s in ir.nurses) >= cov.reqD)
        m.Add(sum(isE[s.nurse_id, d] for s in ir.nurses) >= cov.reqE)
        m.Add(sum(isN[s.nurse_id, d] for s in ir.nurses) >= cov.reqN)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 1
    st = solver.Solve(m)
    if st in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        return True
    if st == cp_model.INFEASIBLE:
        return False
    return None
