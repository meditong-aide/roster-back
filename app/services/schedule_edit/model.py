"""창(window) 안의 바꿀 수 있는 칸만 변수로 둔 소형 CP-SAT 모델.

생성 엔진(fallback_lex)을 쓰지 않는다 — 그쪽은 빈 달을 채우는 모델이라 전처리·빌드만 수 초이고
외부 기준 근무표를 받을 입구가 없다. 여기선 지금 근무표를 상수로 두고 창 안의 칸만 푼다.

규칙은 `rules.py` 의 부등식을 그대로 건다. 상한은 `max(cap, 지금 값)` — 새 위반 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from ortools.sat.python import cp_model

from services.schedule_edit import distribution as dist
from services.schedule_edit.context import EditContext
from services.schedule_edit.requests import AvoidCell, EditRequest, SetCell
from services.schedule_edit.rules import (
    WISH, Grid, Ineq, RuleSet, Term, TeamCover, cell, team_present, value_of,
)

Cell = Tuple[str, int]

#: CP-SAT 작업자 수 — 채팅 요청마다 운영 API 서버에서 돈다. 8 이면 요청 하나가 코어를 다 잡는다.
#: (실측 dev 72건: 풀이 대부분 1초 미만이라 작업자를 줄여도 응답 시간보다 CPU 점유가 먼저 문제다)
SOLVER_WORKERS = 4
SOLVER_SEED = 0


def new_solver(seconds: float):
    """모든 풀이가 같은 작업자 수·씨앗을 쓰게 한 곳에서 만든다."""
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.05, seconds)
    solver.parameters.num_workers = SOLVER_WORKERS
    solver.parameters.random_seed = SOLVER_SEED
    return solver


@dataclass
class EditModel:
    ctx: EditContext
    base: Grid
    free: Set[Cell]
    codes: Tuple[str, ...]
    model: Any = field(default_factory=cp_model.CpModel)
    x: Dict[Tuple[str, int, str], Any] = field(default_factory=dict)
    changed: Dict[Cell, Any] = field(default_factory=dict)
    affected: Dict[str, Any] = field(default_factory=dict)
    #: 품질 악화량 항목(품질 규칙 넘김·사람별 근무 수 변화) — 바뀌는 칸 수 다음 순위로 줄인다
    quality_terms: List[Any] = field(default_factory=list)
    #: 변경 발생 기간 = 마지막 바뀐 날 - 처음 바뀐 날(바뀐 날이 하루면 0)
    span: Any = 0

    def ind(self, term: Term):
        """칸이 term.codes 중 하나인가 — 자유 칸이면 변수 합, 아니면 0/1 상수."""
        if (term.nurse_id, term.day) in self.free:
            return sum(self.x[term.nurse_id, term.day, c] for c in self.codes if c in term.codes)
        return 1 if cell(self.ctx, self.base, term.nurse_id, term.day) in term.codes else 0

    def touches(self, terms: Iterable[Term]) -> bool:
        return any((t.nurse_id, t.day) in self.free for t in terms)

    def total_changes(self):
        return sum(self.changed.values())

    def total_affected(self):
        return sum(self.affected.values())

    def total_quality(self):
        return sum(self.quality_terms)

    def loss(self):
        """주 loss = 2 × 바뀐 칸 + 1 × 변경 기간(일) — 칸 1개 : 기간 1일 = 1 : 0.5(사용자 결정 11-02)."""
        return CHANGE_COEF * self.total_changes() + SPAN_COEF * self.span


def edit_codes(ctx: EditContext) -> Tuple[str, ...]:
    return ("D", "E", "N", "M", "O") if ctx.use_mid else ("D", "E", "N", "O")


def free_cells(ctx: EditContext, days: Iterable[int]) -> Set[Cell]:
    day_set = set(days)
    return {(n, d) for n in ctx.nurse_ids for d in day_set if 0 <= d < ctx.days and ctx.is_free(n, d)}


#: 주 loss 계수 — 정수로 두려고 2배 해 둔다(칸 1 : 기간 0.5)
CHANGE_COEF = 2
SPAN_COEF = 1


def build_model(ctx: EditContext, rules: RuleSet, requests: List[EditRequest],
                free: Set[Cell], max_changes: int, allow: Optional[Dict[Tuple, int]] = None) -> EditModel:
    """allow: 사용자가 레버로 허용한 넘김 {규칙 위치 key: 양} — 그 위치만 상한을 그만큼 올린다."""
    em = EditModel(ctx=ctx, base=ctx.main, free=free, codes=edit_codes(ctx))
    add_cell_vars(em)
    waived = requested_cells(requests)
    for ineq in rules.ineqs:
        if not is_waived(ineq.key, waived):
            _add_ineq(em, ineq, (allow or {}).get(ineq.key, 0))
    for cover in rules.covers:
        add_team_cover(em, cover)
    add_requests(em, requests)
    _add_quality(em, rules)
    add_span(em)
    em.model.Add(em.total_changes() <= max_changes)
    return em


def add_span(em: EditModel) -> None:
    """변경 발생 기간 — 바뀐 칸이 있는 날 중 처음과 마지막의 거리."""
    m, top = em.model, em.ctx.days - 1
    days = sorted({d for _n, d in em.free})
    if not days:
        return
    first, last = m.NewIntVar(0, top, "span_first"), m.NewIntVar(0, top, "span_last")
    for d in days:
        touched = m.NewBoolVar(f"day_changed_{d}")
        for (n, dd), ch in em.changed.items():
            if dd == d:
                m.Add(touched >= ch)
        m.Add(first <= d).OnlyEnforceIf(touched)
        m.Add(last >= d).OnlyEnforceIf(touched)
    span = m.NewIntVar(0, top, "span")
    m.Add(span >= last - first)
    em.span = span


def requested_cells(requests: List[EditRequest]) -> Set[Cell]:
    return {(r.nurse_id, r.day) for r in requests}


def is_waived(key: Tuple, waived: Set[Cell]) -> bool:
    """수간호사가 직접 바꿔 달라고 한 칸의 원티드 보호는 면제한다(요청이 이긴다)."""
    return key[0] == WISH and (key[1], key[2]) in waived


#: 사람별 근무 수가 1 바뀔 때의 품질 악화 — 나이트 몰림을 더 무겁게 본다
BALANCE_WEIGHT = {"N": 3, "D": 1, "E": 1, "M": 1, "O": 1}
#: 가로축 분배 지표(OFF·N 격차, D/E 불균형)가 1 나빠질 때 — 품질 항목 중 가장 무겁게(엔진 lex 패스)
DISTRIBUTION_WEIGHT = 10


def _count(em: EditModel, nid: str, codes: FrozenSet[str], skip_vacation: bool = False):
    return sum(em.ind(Term(nid, d, codes)) for d in range(em.ctx.days)
               if not (skip_vacation and (nid, d) in em.ctx.vacation))


def _add_distribution(em: EditModel) -> None:
    """가로축 분배 지표가 지금보다 나빠진 만큼을 품질 악화로 센다(`distribution.py`)."""
    ctx, touched = em.ctx, {n for n, _ in em.free}
    base = dist.metrics(ctx, em.base)
    off_members = dist.range_members(ctx)
    if touched & set(off_members) and len(off_members) >= 2:
        counts = [_count(em, n, frozenset({"O"}), skip_vacation=True) for n in off_members]
        _add_range_excess(em, counts, base["off_range"], "off")
    n_members = dist.night_range_members(ctx)
    if touched & set(n_members) and len(n_members) >= 2:
        counts = [_count(em, n, frozenset({"N"})) for n in n_members]
        _add_range_excess(em, counts, base["n_range"], "night")
    de_members = [n for n in dist.de_members(ctx) if n in touched]
    if de_members and dist.de_enabled(ctx):
        _add_de_excess(em, de_members, base["de"])


def _add_range_excess(em: EditModel, counts: List, base_range: int, tag: str) -> None:
    m, top = em.model, em.ctx.days
    hi, lo = m.NewIntVar(0, top, f"{tag}_hi"), m.NewIntVar(0, top, f"{tag}_lo")
    for c in counts:
        m.Add(hi >= c)
        m.Add(lo <= c)
    worse = m.NewIntVar(0, top, f"{tag}_worse")
    m.Add(worse >= hi - lo - base_range)
    em.quality_terms.append(DISTRIBUTION_WEIGHT * worse)


def _add_de_excess(em: EditModel, members: List[str], base_de: int) -> None:
    """바뀌는 사람의 |D-E| 초과분만 변수로 — 안 바뀌는 사람 몫은 상수라 base 에서 빼고 더한다."""
    m, tol, top = em.model, dist.de_tolerance(em.ctx), em.ctx.days
    fixed_part = base_de - sum(_de_excess_of(em.ctx, n, em.base, tol) for n in members)
    excesses = []
    for n in members:
        gap = _count(em, n, frozenset({"D"})) - _count(em, n, frozenset({"E"}))
        exc = m.NewIntVar(0, top, f"de_{n}")
        m.Add(exc >= gap - tol)
        m.Add(exc >= -gap - tol)
        excesses.append(exc)
    worse = m.NewIntVar(0, top * len(members), "de_worse")
    m.Add(worse >= fixed_part + sum(excesses) - base_de)
    em.quality_terms.append(DISTRIBUTION_WEIGHT * worse)


def _de_excess_of(ctx: EditContext, nid: str, grid: Grid, tol: int) -> int:
    row = grid[nid]
    return max(0, abs(sum(1 for c in row if c == "D") - sum(1 for c in row if c == "E")) - tol)


def _add_quality(em: EditModel, rules: RuleSet) -> None:
    """품질 악화량 — 품질 규칙(N-O-D 등)이 지금보다 나빠진 만큼 + 사람별 D/E/N/OFF 수 변화.

    엔진이 고르게 나눠 둔 근무표라, 사람별 근무 수를 그대로 두는 것이 균형을 지키는 가장 단순한 방법이다.
    """
    _add_distribution(em)
    for ineq in rules.soft:
        if em.touches(ineq.terms):
            slack = em.model.NewIntVar(0, len(ineq.terms) + abs(ineq.cap) + 1, f"soft_{len(em.quality_terms)}")
            expr = ineq.const + sum(t.coef * em.ind(t) for t in ineq.terms)
            em.model.Add(expr <= max(ineq.cap, value_of(em.ctx, em.base, ineq)) + slack)
            em.quality_terms.append(slack)
    for n in sorted({n for n, _ in em.free}):
        days = [d for (m, d) in em.free if m == n]
        for c in em.codes:
            delta = sum(em.x[n, d, c] for d in days) - sum(1 for d in days if em.base[n][d] == c)
            gap = em.model.NewIntVar(0, len(days), f"bal_{n}_{c}")
            em.model.AddAbsEquality(gap, delta)
            em.quality_terms.append(BALANCE_WEIGHT.get(c, 1) * gap)


def add_cell_vars(em: EditModel) -> None:
    m = em.model
    for n, d in sorted(em.free):
        for c in em.codes:
            em.x[n, d, c] = m.NewBoolVar(f"x_{n}_{d}_{c}")
        m.AddExactlyOne(em.x[n, d, c] for c in em.codes)
        base = em.base[n][d]
        em.changed[n, d] = 1 - em.x[n, d, base] if base in em.codes else 1
    for n in sorted({n for n, _ in em.free}):
        flag = m.NewBoolVar(f"aff_{n}")
        cells = [em.changed[c] for c in em.changed if c[0] == n]
        for ch in cells:
            m.Add(flag >= ch)
        m.Add(flag <= sum(cells))   # 바뀐 칸이 없으면 0 — 다른 후보 조건이 거짓으로 채워지지 않게
        em.affected[n] = flag


def _add_ineq(em: EditModel, ineq: Ineq, allowance: int = 0) -> None:
    if not em.touches(ineq.terms):
        return
    base = value_of(em.ctx, em.base, ineq)
    expr = ineq.const + sum(t.coef * em.ind(t) for t in ineq.terms)
    em.model.Add(expr <= max(ineq.cap, base) + allowance)


def add_team_cover(em: EditModel, cover: TeamCover) -> None:
    members = [n for team, _ in cover.teams for n in team]
    if not any((n, cover.day) in em.free for n in members):
        return
    flags = []
    for idx, (team, need) in enumerate(cover.teams):
        flag = em.model.NewBoolVar(f"team_{cover.day}_{cover.code}_{idx}")
        count = sum(em.ind(Term(n, cover.day, frozenset({cover.code}))) for n in team)
        em.model.Add(count >= need * flag)
        flags.append(flag)
    em.model.Add(sum(flags) >= min(cover.target, team_present(em.ctx, em.base, cover)))


def add_requests(em: EditModel, requests: List[EditRequest]) -> None:
    for req in requests:
        if isinstance(req, SetCell):
            em.model.Add(em.x[req.nurse_id, req.day, req.code] == 1)
        elif isinstance(req, AvoidCell):
            for c in req.codes:
                if (req.nurse_id, req.day, c) in em.x:
                    em.model.Add(em.x[req.nurse_id, req.day, c] == 0)


def read_solution(em: EditModel, solver) -> Grid:
    """풀이 → 한 달 대표코드 격자(바뀌지 않은 칸은 지금 값)."""
    grid = {n: list(row) for n, row in em.base.items()}
    for n, d in em.free:
        grid[n][d] = next(c for c in em.codes if solver.Value(em.x[n, d, c]))
    return grid


def changed_cells(em: EditModel, grid: Grid) -> List[Cell]:
    return sorted((n, d) for n, d in em.free if grid[n][d] != em.base[n][d])


def forbid_same_people(em: EditModel, people: Set[str]) -> None:
    """앞 후보와 '바뀐 사람 집합'이 달라야 한다(다양한 대안 · Hebrard AAAI 2005 류의 해밍 조건)."""
    if any(n not in em.affected for n in people):
        return   # 이 범위에서 못 바꾸는 사람이 묶음에 있다 — 같은 묶음이 나올 수 없다(일부 묶음까지 막지 않게)
    inside = [1 - em.affected[n] for n in people]
    outside = [em.affected[n] for n in em.affected if n not in people]
    em.model.Add(sum(inside) + sum(outside) >= 1)


def subcode_changes(em: EditModel) -> List[Any]:
    """하위코드(Dㅇ 등) 칸을 바꾸는 비용 — 대표코드 칸보다 비싸게 본다(정보가 사라진다)."""
    out = []
    for (n, d), ch in em.changed.items():
        raw = str(em.ctx.raw[n][d] or "")
        rep = str(em.ctx.rep_code.get(em.base[n][d], "") or "")
        if raw and raw.upper() != rep.upper():
            out.append(ch)
    return out
