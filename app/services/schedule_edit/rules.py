"""근무표 규칙을 '칸 지시자 선형 부등식'으로 정의한다 — 모델과 검증기가 같은 정의를 쓴다.

부등식 하나 = `const + Σ coef·[칸(nurse, day) ∈ codes] <= cap`. 값이 cap 을 넘은 만큼이 위반량이다.
  · 검증: 근무표에 값을 넣어 위반량을 잰다(`measure`).
  · 모델: 같은 식을 CP-SAT 에 건다. 단 상한은 `max(cap, 지금 근무표의 값)` —
    지키던 건 계속 지키고, 이미 어긴 건 더 나빠지지만 않게 한다(사용자 결정 ③).
    이 상대화가 하드락 '완화'가 아닌 이유: 지금 근무표에 없던 위반은 하나도 새로 생길 수 없다.

규칙 원문은 생성 엔진 `cp_sat/fallback_lex.py` 다(줄 번호는 각 함수 docstring). 화면 판정과의
일치는 `screen_rules.py` 가 따로 본다.

일(day) 번호: 이번 달 0..days-1, 전월 꼬리는 음수(-1 = 전월 말일). 칸 값은 대표코드
D/E/N/M/O/W 또는 None(근무표에 칸 없음·관할 밖) — None 이 낀 규칙은 엔진처럼 건너뛴다.

켜고 끄는 설정이 없는 규칙: 연속 근무 상한·월 나이트 상한은 엔진도 항상 건다(값이 비면 엔진 기본값
5일·15회 · CSB:454·466). 나머지 하드락은 설정이 켜졌을 때만 건다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple

from services.schedule_edit.context import EditContext

Grid = Dict[str, List[Optional[str]]]

WORK = frozenset({"D", "E", "N", "M", "W"})
OFF = frozenset({"O"})
NIGHT = frozenset({"N"})
#: 원티드 보호 규칙 종류 — 요청한 칸에서는 면제된다
WISH = "원티드"


@dataclass(frozen=True)
class Term:
    nurse_id: str
    day: int
    codes: FrozenSet[str]
    coef: int = 1


@dataclass(frozen=True)
class Ineq:
    """`const + Σ terms <= cap`. key 는 (규칙, 간호사|None, 일|None, 부가) — 위반 위치."""

    key: Tuple
    terms: Tuple[Term, ...]
    const: int
    cap: int


@dataclass(frozen=True)
class TeamCover:
    """팀 최소 — 그날 그 근무에 서로 다른 팀을 min(요구, 팀 수)개 채운다(엔진 team_constraints:120)."""

    key: Tuple
    day: int
    code: str
    teams: Tuple[Tuple[Tuple[str, ...], int], ...]   # ((팀원들, 팀 최소), …)
    target: int


# ── 칸 읽기 ─────────────────────────────────────────────────────────────────


def cell(ctx: EditContext, grid: Grid, nurse_id: str, day: int) -> Optional[str]:
    """이번 달은 grid, 음수 일은 전월 꼬리. 범위 밖은 None."""
    if 0 <= day < ctx.days:
        return grid.get(nurse_id, [None] * ctx.days)[day]
    tail = ctx.tail.get(nurse_id) or []
    idx = len(tail) + day
    if day < 0 and 0 <= idx < len(tail):
        value = tail[idx]
        return None if value in (None, "-") else value
    return None


def value_of(ctx: EditContext, grid: Grid, ineq: Ineq) -> int:
    return ineq.const + sum(
        t.coef for t in ineq.terms if cell(ctx, grid, t.nurse_id, t.day) in t.codes)


def team_present(ctx: EditContext, grid: Grid, cover: TeamCover) -> int:
    present = 0
    for members, need in cover.teams:
        if sum(1 for n in members if cell(ctx, grid, n, cover.day) == cover.code) >= need:
            present += 1
    return present


def measure(ctx: EditContext, grid: Grid, rules: "RuleSet") -> Dict[Tuple, int]:
    """위반 위치 → 위반량(0 인 건 뺀다). 품질 규칙(soft)은 `measure_soft` 가 따로 잰다."""
    out = _excess(ctx, grid, rules.ineqs)
    for cover in rules.covers:
        deficit = cover.target - team_present(ctx, grid, cover)
        if deficit > 0:
            out[cover.key] = deficit
    return out


def measure_soft(ctx: EditContext, grid: Grid, rules: "RuleSet") -> Dict[Tuple, int]:
    return _excess(ctx, grid, rules.soft)


def _excess(ctx: EditContext, grid: Grid, ineqs: List["Ineq"]) -> Dict[Tuple, int]:
    out: Dict[Tuple, int] = {}
    for ineq in ineqs:
        excess = value_of(ctx, grid, ineq) - ineq.cap
        if excess > 0:
            out[ineq.key] = excess
    return out


def new_violations(before: Dict[Tuple, int], after: Dict[Tuple, int]) -> Dict[Tuple, Tuple[int, int]]:
    """위치별로 위반량이 늘어난 것 → (전, 후)."""
    return {k: (before.get(k, 0), v) for k, v in after.items() if v > before.get(k, 0)}


@dataclass
class RuleSet:
    ineqs: List[Ineq]
    covers: List[TeamCover]
    #: 품질 규칙 — 지키지 못해도 되지만 지금보다 나빠진 만큼을 줄인다(바뀌는 칸 수 다음 순위)
    soft: List[Ineq] = field(default_factory=list)


def build_rules(ctx: EditContext) -> RuleSet:
    """근무표 하나에 걸리는 규칙 전부. 요청당 1회."""
    ineqs: List[Ineq] = []
    for nid in ctx.nurse_ids:
        for builder in _NURSE_RULES:
            ineqs.extend(builder(ctx, nid))
    ineqs.extend(_coverage(ctx))
    ineqs.extend(_grade(ctx))
    ineqs.extend(_mutex(ctx))
    soft = [i for nid in ctx.nurse_ids for builder in _SOFT_RULES for i in builder(ctx, nid)]
    soft.extend(_coverage_over(ctx))
    return RuleSet(ineqs=ineqs, covers=list(_team_covers(ctx)), soft=soft)


# ── 간호사 단위: 연속·전환 ──────────────────────────────────────────────────


def _known(ctx: EditContext, nurse_id: str, days: Sequence[int]) -> bool:
    """칸이 다 있는가 — 관할 밖·빈칸이 끼면 엔진이 그 창을 건너뛴다(FL:2392)."""
    grid = ctx.main
    return all(cell(ctx, grid, nurse_id, d) is not None for d in days)


def _first(ctx: EditContext, nurse_id: str) -> int:
    return -len(ctx.tail.get(nurse_id) or [])


def _windows(ctx: EditContext, nurse_id: str, size: int, cross_month: bool = True) -> Iterator[List[int]]:
    """길이 size 창 — 이번 달에 걸치는 것만. cross_month 면 전월 꼬리부터 잇는다."""
    start = _first(ctx, nurse_id) if cross_month else 0
    for s in range(start, ctx.days - size + 1):
        window = list(range(s, s + size))
        if window[-1] >= 0 and _known(ctx, nurse_id, window):
            yield window


def _consec_work(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """연속 근무 ≤ K — K+1 일 창마다 OFF 1 이상(하드락 #1 · FL:2388)."""
    k = int(ctx.config.get("max_conseq_work") or 5)
    for w in _windows(ctx, nid, k + 1):
        yield Ineq(("연속근무", nid, w[0], k), tuple(Term(nid, d, WORK) for d in w), 0, k)


def _night_streak(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """연속 N ≤ L(3, three_seq_nig 꺼지면 2) — 하드락 #3 · FL:2541."""
    lim = 3 if ctx.config.get("three_seq_nig") else 2
    for w in _windows(ctx, nid, lim + 1):
        yield Ineq(("연속나이트", nid, w[0], lim), tuple(Term(nid, d, NIGHT) for d in w), 0, lim)


def _transition_pairs(ctx: EditContext) -> List[Tuple[str, FrozenSet[str], FrozenSet[str]]]:
    pairs = []
    if ctx.config.get("banned_day_after_eve"):
        # 하드락 #2 — 설정 하나가 ND·ED·NE 를 함께 켠다(CSB:599). W(대표코드 없는 근무)도 D 처럼(FL:2160)
        pairs += [("ND", NIGHT, frozenset({"D", "W"})), ("ED", frozenset({"E"}), frozenset({"D", "W"})),
                  ("NE", NIGHT, frozenset({"E"}))]
    if ctx.use_mid:
        # M 은 전날 D·O·M 일 때만, M→D 금지(FL:2147-2159)
        pairs += [("xM", frozenset({"E", "N", "W"}), frozenset({"M"})), ("MD", frozenset({"M"}), frozenset({"D"}))]
    return pairs


def _transitions(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    pairs = _transition_pairs(ctx)
    for w in _windows(ctx, nid, 2):
        for name, a, b in pairs:
            yield Ineq(("전환금지", nid, w[1], name), (Term(nid, w[0], a), Term(nid, w[1], b)), 0, 1)


def _one_night(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """1N 금지 — N 인 날은 앞뒤 중 하나가 N(하드락 #7 · FL:2185). 월 N 상한 1 인 사람은 면제."""
    if not ctx.config.get("not_one_night") or nid in ctx.single_n_ok:
        return
    for d in range(ctx.days):
        if cell(ctx, ctx.main, nid, d) is None:
            continue
        terms = [Term(nid, d, NIGHT)]
        for nb in (d - 1, d + 1):
            if cell(ctx, ctx.main, nid, nb) is not None:
                terms.append(Term(nid, nb, NIGHT, -1))
        if len(terms) > 1:
            yield Ineq(("1N", nid, d, None), tuple(terms), 0, 0)


def _recovery(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """3N·2N 뒤 OFF 2일(하드락 #4·#5). 말일을 넘는 회복은 엔진도 안 본다."""
    three = bool(ctx.config.get("two_offs_after_three_nig"))
    two = bool(ctx.config.get("two_offs_after_two_nig"))
    if three:
        yield from _three_nights(ctx, nid)
    if two:
        yield from _two_nights(ctx, nid)
    if two and not three:
        yield from _screen_two_nights(ctx, nid)


def _three_nights(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """N(d-2..d) 이면 d+1·d+2 OFF — 셋째 날부터 본다(FL:2867 `T0+2`)."""
    for d in range(_first(ctx, nid) + 2, ctx.days - 2):
        for k in (1, 2):
            if d + k >= 0 and _known(ctx, nid, (d - 2, d - 1, d, d + k)):
                terms = (Term(nid, d - 2, NIGHT), Term(nid, d - 1, NIGHT), Term(nid, d, NIGHT),
                         Term(nid, d + k, OFF, -1))
                yield Ineq(("3N후OFF", nid, d, k), terms, 1, 3)


def _two_nights(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """N(d-1)·N(d)·¬N(d+1) 이면 d+1·d+2 OFF — 둘째 날부터(FL:2984 `T0+1`). 3N 의 끝도 여기서 잡힌다."""
    for d in range(_first(ctx, nid) + 1, ctx.days - 2):
        for k in (1, 2):
            if d + k >= 0 and _known(ctx, nid, (d - 1, d, d + 1, d + k)):
                terms = (Term(nid, d - 1, NIGHT), Term(nid, d, NIGHT), Term(nid, d + 1, NIGHT, -1),
                         Term(nid, d + k, OFF, -1))
                yield Ineq(("2N후OFF", nid, d, k), terms, 2, 3)


def _screen_two_nights(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """화면의 2N 뒤 OFF — N 두 칸이 이어지면 그 시작마다 다음 이틀 OFF(useRosterValidation.ts:716).

    엔진보다 엄격하다(NNN 도 첫 NN 뒤가 N 이라 위반). 3N 뒤 OFF 가 켜지면 화면이 이 검사를 건너뛰어
    그때는 안 건다. 둘째 OFF 날이 이번 달 밖이면 화면도 생략한다(말일 예외).
    """
    for i in range(_first(ctx, nid), ctx.days - 3):
        for k in (2, 3):
            days = (i, i + 1, i + k)
            if i + k >= 0 and _known(ctx, nid, days):
                terms = (Term(nid, i, NIGHT), Term(nid, i + 1, NIGHT), Term(nid, i + k, OFF, -1))
                yield Ineq(("2N후OFF(화면)", nid, i, k), terms, 1, 2)


def _n2n_gap(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """N 블록 간격 하한 — 블록이 d 에 끝나면 d+2…d+gap-1 에 N 금지(FL:3025). N 전담은 제외."""
    gap = int(ctx.n2n_gap or 0)
    if gap < 4 or nid in ctx.n_only:
        return
    for d in range(_first(ctx, nid), ctx.days - 1):
        if not _known(ctx, nid, (d, d + 1)):
            continue
        for k in range(2, gap):
            dk = d + k
            if 0 <= dk < ctx.days and cell(ctx, ctx.main, nid, dk) is not None:
                terms = (Term(nid, d, NIGHT), Term(nid, d + 1, NIGHT, -1), Term(nid, dk, NIGHT))
                yield Ineq(("N간격", nid, dk, d), terms, 1, 2)


def _same_shift(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """같은 근무 k+1 연속 금지(기본 k=3 · FL:2430). 그 근무 전담자는 제외. 월 안에서만 본다."""
    k = int(ctx.same_shift_k or 0)
    if k <= 0:
        return
    for code in ("D", "E", "N"):
        if ctx.single_shift.get(nid) == code:
            continue
        for w in _windows(ctx, nid, k + 1, cross_month=False):
            yield Ineq(("같은근무연속", nid, w[0], code),
                       tuple(Term(nid, d, frozenset({code})) for d in w), 0, k)


def _isolated_work(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """고립근무 금지 — O 근무 O(근무는 N·W 제외 · FL:2485). 월 안에서만."""
    if not ctx.isolated_work_hard:
        return
    for d in range(1, ctx.days - 1):
        if _known(ctx, nid, (d - 1, d, d + 1)):
            terms = (Term(nid, d - 1, OFF), Term(nid, d + 1, OFF), Term(nid, d, frozenset({"D", "E", "M"})))
            yield Ineq(("고립근무", nid, d, None), terms, 0, 2)


# ── 간호사 단위: 칸·월 합계 ─────────────────────────────────────────────────


def _cell_rules(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """허용 근무형·기피(FL:1649·2635)·휴가 전날 N 금지(FL:2230)·주말휴무(FL:1495)."""
    for d in range(ctx.days):
        codes = ctx.forbidden.get((nid, d))
        if codes:
            yield Ineq(("금지근무", nid, d, None), (Term(nid, d, frozenset(codes)),), 0, 0)
        if (nid, d) in ctx.ban_n_before:
            yield Ineq(("휴무전날N", nid, d, None), (Term(nid, d, NIGHT),), 0, 0)
        if nid in ctx.weekend_off and cell(ctx, ctx.main, nid, d) is not None:
            if d in ctx.weekend_days:
                yield Ineq(("주말휴무", nid, d, "주말근무"), (Term(nid, d, OFF, -1),), 1, 0)
            else:
                yield Ineq(("주말휴무", nid, d, "평일휴무"), (Term(nid, d, OFF),), 0, 0)


def _month_count(ctx: EditContext, nid: str, codes: FrozenSet[str], skip_vacation: bool = False) -> Tuple[Term, ...]:
    return tuple(Term(nid, d, codes) for d in range(ctx.days)
                 if not (skip_vacation and (nid, d) in ctx.vacation))


def _month_totals(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """월 N 상한(하드락 #6 · FL:2607)·개인 월 한도(ML:76)·순수 OFF 수(FL:3321)."""
    from services.constraints.monthly_limit_constraints import _norm_bounds
    yield Ineq(("월나이트", nid, None, None), _month_count(ctx, nid, NIGHT), 0,
               int(ctx.config.get("max_nig_per_month") or 15))
    limit = ctx.monthly_limits.get(nid) or {}
    for prefix, codes, skip_vac in (("d", frozenset({"D"}), False), ("e", frozenset({"E"}), False),
                                    ("n", NIGHT, False), ("o", OFF, True)):
        lo, hi = _norm_bounds(limit.get(f"{prefix}_min"), limit.get(f"{prefix}_max"),
                              limit.get(f"{prefix}_exact"))
        terms = _month_count(ctx, nid, codes, skip_vac)
        yield from _bounds(("개인한도", nid, None, prefix), terms, lo, hi)
    if nid in ctx.n_only or nid in ctx.weekend_off:
        return  # N 전담은 OFF 수를 N 규칙이 정하고, 주말휴무자는 칸 규칙이 이미 정한다(FL:3414)
    lo, hi = ctx.off_bounds.get(nid, (None, None))
    yield from _bounds(("OFF수", nid, None, None), _month_count(ctx, nid, OFF, True), lo, hi)


def _bounds(key: Tuple, terms: Tuple[Term, ...], lo: Optional[int], hi: Optional[int]) -> Iterator[Ineq]:
    if lo is not None and lo > 0:
        yield Ineq(key + ("하한",), tuple(Term(t.nurse_id, t.day, t.codes, -1) for t in terms), 0, -int(lo))
    if hi is not None:
        yield Ineq(key + ("상한",), terms, 0, int(hi))


def _wishes(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """간호사 원티드(희망)가 지금 반영된 칸은 계속 반영 — 바뀌는 칸 수보다 먼저 지킨다(사용자 결정 11-02).

    지금 반영되지 않은 원티드는 걸지 않는다(나빠질 수 없다). 수간호사가 그 칸 자체를 바꿔 달라고
    요청하면 요청이 이긴다 — 모델·검증이 `WISH` 종류를 요청 칸에서 면제한다.
    """
    for (n, d), codes in sorted(ctx.wishes.items()):
        if n == nid and ctx.main[n][d] in codes:
            code_set = frozenset(codes)
            yield Ineq((WISH, n, d, "".join(sorted(code_set))), (Term(n, d, code_set, -1),), 0, -1)


_NURSE_RULES = (_consec_work, _night_streak, _transitions, _one_night, _recovery, _n2n_gap,
                _same_shift, _isolated_work, _cell_rules, _month_totals, _wishes)


# ── 품질 규칙(soft): 엔진이 벌점으로만 다루는 것 ────────────────────────────


def _patterns(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """N-O-D · N-O-E · E-O-D 패턴(FL:3119 `nod_noe`, 기본 켜짐) — 전월 꼬리부터 잇는다."""
    if not ctx.config.get("nod_noe", True):
        return
    shapes = (("N-O-D", NIGHT, frozenset({"D"})), ("N-O-E", NIGHT, frozenset({"E"})),
              ("E-O-D", frozenset({"E"}), frozenset({"D"})))
    for w in _windows(ctx, nid, 3):
        for name, a, c in shapes:
            terms = (Term(nid, w[0], a), Term(nid, w[1], OFF), Term(nid, w[2], c))
            yield Ineq(("패턴", nid, w[0], name), terms, 0, 2)


def _isolated_off(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """근무 사이 OFF 하루(FL:2014 `sequential_offs`). 휴가·공가가 끼면 엔진처럼 건너뛴다."""
    if not ctx.config.get("sequential_offs", True):
        return
    for d in range(1, ctx.days - 1):
        days = (d - 1, d, d + 1)
        if _known(ctx, nid, days) and not any((nid, x) in ctx.vacation for x in days):
            terms = (Term(nid, d - 1, WORK), Term(nid, d, OFF), Term(nid, d + 1, WORK))
            yield Ineq(("고립OFF", nid, d, None), terms, 0, 2)


def _long_off(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """연속 OFF 상한(max_conseq_off, 기본 3 · CSB:617 벌점)."""
    k = int(ctx.config.get("max_conseq_off") or 3)
    for w in _windows(ctx, nid, k + 1, cross_month=False):
        yield Ineq(("연속OFF", nid, w[0], k), tuple(Term(nid, d, OFF) for d in w), 0, k)


def _weekly_offs(ctx: EditContext, nid: str) -> Iterator[Ineq]:
    """1일부터 7일씩 끊은 주마다 OFF 2개(two_offs_per_week · FL:2750 벌점)."""
    if not ctx.config.get("two_offs_per_week"):
        return
    for start in range(0, ctx.days - 6, 7):
        week = list(range(start, start + 7))
        if _known(ctx, nid, week):
            yield Ineq(("주2회OFF", nid, start, None), tuple(Term(nid, d, OFF, -1) for d in week), 0, -2)


_SOFT_RULES = (_patterns, _isolated_off, _long_off, _weekly_offs)


def _coverage_over(ctx: EditContext) -> Iterator[Ineq]:
    """최대 인원이 없는 날 필요 인원을 넘는 근무(엔진은 off_first 가 꺼지면 벌점 · FL:1908).

    off_first 면 엔진이 하드로 막아 `_coverage` 의 인원최대가 이미 건다 — 여기선 안 건다.
    """
    if ctx.config.get("off_first"):
        return
    for d in range(ctx.days):
        people = _counted(ctx, d)
        for code, need in (ctx.cov_min[d] if d < len(ctx.cov_min) else {}).items():
            need = int(need or 0)
            cap = int((ctx.cov_max[d] if d < len(ctx.cov_max) else {}).get(code, 0) or 0)
            if need > 0 and cap == 0 and code in ("D", "E", "N"):
                terms = tuple(Term(n, d, frozenset({code})) for n in people)
                yield Ineq(("인원초과", None, d, code), terms, 0, need)


# ── 날짜 단위: 인원·등급·팀·상호배제 ────────────────────────────────────────


def _counted(ctx: EditContext, day: int) -> List[str]:
    """그날 인원에 세는 사람 — 프리셉티(설정 꺼짐)·빈칸 제외(FL:1811)."""
    return [n for n in ctx.nurse_ids
            if (n, day) not in ctx.cov_exclude and ctx.main[n][day] is not None]


def _coverage(ctx: EditContext) -> Iterator[Ineq]:
    """일별 최소·최대(FL:1775). 최대 없는 D/E/N 초과는 엔진도 벌점만이라 여기 안 건다."""
    off_first = bool(ctx.config.get("off_first"))
    for d in range(ctx.days):
        people = _counted(ctx, d)
        for code, need in (ctx.cov_min[d] if d < len(ctx.cov_min) else {}).items():
            need = int(need or 0)
            cap = int((ctx.cov_max[d] if d < len(ctx.cov_max) else {}).get(code, 0) or 0)
            terms = tuple(Term(n, d, frozenset({code})) for n in people)
            if need > 0:
                yield Ineq(("인원최소", None, d, code), tuple(Term(t.nurse_id, d, t.codes, -1) for t in terms),
                           0, -need)
            upper = _coverage_upper(code, need, cap, off_first)
            if upper is not None:
                yield Ineq(("인원최대", None, d, code), terms, 0, upper)


def _coverage_upper(code: str, need: int, cap: int, off_first: bool) -> Optional[int]:
    if cap > 0:
        return cap
    if need == 0 and code in ("D", "E", "N", "M"):
        return 0          # 요구 0 은 금지(최대가 있으면 0~최대) — FL:1871
    if code == "M" or off_first:
        return need       # M 은 최대 없으면 최소가 상한, off_first 는 남는 인원을 OFF 로 — FL:1855·1903
    return None


def _grade(ctx: EditContext) -> Iterator[Ineq]:
    """등급 최소 — grade≤g 누적 인원(엔진은 벌점, 여기선 '나빠지지 않게' · GC:575)."""
    population = Counter(g for n, g in ctx.grade_of.items() if g is not None and n in ctx.main)
    if not population or not ctx.grade_min:
        return
    for code, per_grade in ctx.grade_min.items():
        targets = _cascade(per_grade or {}, population)
        for d in range(ctx.days):
            need = int((ctx.cov_min[d] if d < len(ctx.cov_min) else {}).get(code, 0) or 0)
            people = _counted(ctx, d)
            for grade, target in targets.items():
                eligible = [n for n in people if ctx.grade_of.get(n) is not None and ctx.grade_of[n] <= grade]
                if eligible and min(target, need) > 0:
                    # 그날 그 근무 요구가 0 이면 등급 요구도 없다(M 은 대부분의 날 0)
                    terms = tuple(Term(n, d, frozenset({code}), -1) for n in eligible)
                    yield Ineq(("등급최소", None, d, f"{code}≤{grade}"), terms, 0, -min(target, need))


def _cascade(per_grade: Dict, population: Counter) -> Dict[int, int]:
    """인원 0 인 등급의 요구는 인원이 있는 상위 등급으로 올린다(lns `_cascade_grade_targets` 와 같다)."""
    existing = sorted(population)
    out: Dict[int, int] = {}
    for raw_grade, raw_count in per_grade.items():
        try:
            grade, count = int(raw_grade), int(raw_count)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        target = grade if population.get(grade, 0) > 0 else next((g for g in existing if g > grade), grade)
        out[target] = max(out.get(target, 0), count)
    return out


def _mutex(ctx: EditContext) -> Iterator[Ineq]:
    """상호배제 — 같은 날 같은 근무 금지(엔진은 벌점 · fallback_objectives:595).

    양방향으로 저장돼 a→b·b→a 가 따로 오므로 짝별로 날짜를 합친 뒤 한 번만 건다.
    """
    days_by_pair: Dict[Tuple[str, str], set] = {}
    for a, info in ctx.mutex.items():
        b = str(info.get("partner_id") or "")
        if b in ctx.main and str(a) in ctx.main:
            pair = tuple(sorted((str(a), b)))
            days_by_pair.setdefault(pair, set()).update(int(d) for d in info.get("days") or ())
    for (a, b), days in sorted(days_by_pair.items()):
        for d in sorted(days):
            for code in ("D", "E", "N", "M"):
                terms = (Term(a, d, frozenset({code})), Term(b, d, frozenset({code})))
                yield Ineq(("상호배제", a, d, code), terms, 0, 1)


def _team_covers(ctx: EditContext) -> Iterator[TeamCover]:
    if not ctx.team_min:
        return
    for d in range(ctx.days):
        people = set(_counted(ctx, d))
        for code, need in (ctx.cov_min[d] if d < len(ctx.cov_min) else {}).items():
            teams = []
            for team_id, mins in ctx.team_min.items():
                min_t = int(mins.get(code, 0) or 0)
                members = tuple(n for n in people if ctx.team_of.get(n) == team_id)
                if min_t > 0 and len(members) >= min_t:
                    teams.append((members, min_t))
            target = min(int(need or 0), len(teams))
            if target > 0:
                yield TeamCover(("팀최소", None, d, code), d, code, tuple(teams), target)
