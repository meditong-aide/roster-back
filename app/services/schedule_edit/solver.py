"""수정 요청 → 최소 변경 후보(최대 3개). 저장하지 않는다(읽기 전용).

순서(사용자 결정 ① · 11-02 보강 · 사전식):
  1. 요청을 지킨다(하드). 설정·하드락은 지금보다 나빠지지 않게(하드). 간호사 원티드가 반영된 칸도
     여기에 든다 — 칸 수보다 먼저 지킨다(요청한 칸은 예외).
  2. 바뀌는 칸 수를 최소로 — d*.
  3. d* 를 묶고 품질 악화를 최소로 — N-O-D·고립 OFF·연속 OFF·주 2회 OFF 가 새로 생긴 만큼과
     사람별 D/E/N/OFF 수 변화(나이트 3배) → 바뀌는 사람 수 → 하위코드 칸.
  가중합 하나로 섞지 않는다 — 섞으면 최소 변경이 보장되지 않는다(야마나시 병원 ASP · ICLP 2025).

창은 요청한 가장 이른 날부터 **뒤로만** +3일 → +7일 → 말일 순으로 넓힌다(Wickert·Smet·Vanden Berghe
2019). 그 앞 날짜는 바꾸지 않는다 — 이미 근무했거나 알려 준 근무와 달라지면 안 된다(사용자 지시 11-02).
오늘 이전 날짜를 대상으로 한 요청은 받지 않는다. 셋 다 규칙을 모두
지키고 칸 수 상한(MAX_CHANGES)을 건 같은 모델이다. 한 달에서도 불가가 증명되면 `explain` 이 같은 상한
안에서 무엇을 넘겨야 하는지 알려 준다. 하드락은 어느 단계에서도 풀지 않는다.

요청 하나에 시간 예산(TIME_BUDGET)을 둔다 — 채팅 응답이 CloudFront 30초 안에 들어와야 한다.
예산을 다 쓰면 그때까지 찾은 후보만 낸다.

후보마다 두 번 더 본다(모델과 별개):
  · 한 달 전체를 `rules.measure` 로 다시 재서 새 위반 0 — 어긋나면 코드 결함이라 ERROR 로그.
  · 화면 판정(`screen_check`)으로 새 빨간 칸 0 — 어긋나면 그 후보만 버리고 다음 대안을 찾는다.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from ortools.sat.python import cp_model

from services.schedule_edit import distribution as dist
from services.schedule_edit.context import (
    EditContext, pure_off_count, released_wanted, with_released, with_vacation,
)
from services.schedule_edit.explain import direct_conflicts, explain_month
from services.schedule_edit.levers import LEVER_SECONDS, find_levers, lever_allow
from services.schedule_edit.model import (
    CHANGE_COEF, SPAN_COEF, EditModel, build_model, forbid_same_people, free_cells, grid_relief, is_waived,
    new_solver,
    read_solution,
    requested_cells, subcode_changes,
)
from services.schedule_edit.requests import (
    AvoidCell, BalanceTarget, CountTarget, EditRequest, LeaveCell, SetCell, ShapeTarget, WishTarget, balance_counts,
    first_open_day, is_satisfied, request_problem, shape_count, wish_targets,
)
from services.schedule_edit.rules import Grid, RuleSet, build_rules, measure, measure_soft, new_violations
from services.schedule_edit.screen_check import ScreenBaseline, screen_regressions

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

WINDOW_STEPS: Tuple[Optional[int], ...] = (3, 7, None)   # None = 한 달 전체
SOLVE_SECONDS = 2.0
MONTH_SECONDS = 6.0
EXPLAIN_SECONDS = 6.0
TIME_BUDGET = 20.0
MAX_CHANGES = 20
#: 고르게 맞추기는 여러 사람을 함께 옮긴다 — 칸 수 상한을 넓힌다(아래 무게 순위는 칸 40·사람 수에서도 지켜진다)
BALANCE_MAX_CHANGES = 40
#: 대안 후보는 최소 loss 보다 이만큼까지 더 써도 된다(칸 2개어치)
EXTRA_LOSS = 4
#: 사전식 순서를 무게로 흉내 낸다 — 주 loss(칸·기간) > 품질 > 사람 수 > 하위코드. 앞 순위 1 이 뒤 순위 전부보다 무겁게:
#:   사람 수 × 10 + 하위코드 ≤ 20 × 10 + 20 < 1,000(품질 1) — 바뀐 사람·하위코드 칸은 칸 상한(MAX_CHANGES)을 못 넘는다
#:   품질 악화량 ≤ 22칸 × 칸당 최대 약 8(근무 수 변화 2 × 나이트 3 + 품질 규칙) + 분배 지표 3종 × 10 × 수십
#:              < 10,000 → × 1,000 < 10,000,000(loss 1)
LOSS_WEIGHT = 10_000_000
QUALITY_WEIGHT = 1_000


@dataclass
class CellChange:
    nurse_id: str
    name: str
    day: int
    before: Optional[str]
    after: str
    before_main: Optional[str]
    after_main: str
    #: 휴가 코드로 바뀐 칸(생성 후처리의 연차전환) — 대표코드는 O 그대로지만 순수 OFF 수에서는 빠진다
    vacation: bool = False


@dataclass
class Candidate:
    rank: int
    changes: List[CellChange]
    affected: List[str]
    optimal: bool
    window: Tuple[int, int]
    #: 바뀐 사람별 근무 수 {nurse_id: {"D": (전, 후), …}}
    counts: Dict[str, Dict[str, Tuple[int, int]]] = field(default_factory=dict)
    #: 변경 발생 기간(처음·마지막 바뀐 날, 0부터)과 주 loss(2×칸 + 기간)
    span: Tuple[int, int] = (0, 0)
    loss: int = 0
    #: 이 후보로 새로 생기는 품질 문제 문장(규칙 위반은 아니다)
    quality: List[str] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return len(self.changes)


@dataclass
class EditResult:
    status: str               # ok · already · invalid · infeasible · timeout
    candidates: List[Candidate] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: 불가일 때 넘겨야 하는 규칙 종류(`rules` 의 key[0])
    blocking: List[str] = field(default_factory=list)
    #: 레버 — 무엇을 손해 보면 무엇을 얻는지(`levers.find_levers` 가 채운다)
    levers: List[Any] = field(default_factory=list)
    #: 후보를 낸 곳 — None 이면 솔버, 그 밖은 생성 후처리(`postprocess.KINDS` · 예: annual_leave_swap)
    source: Optional[str] = None
    #: 답 본문에 더할 줄(참고가 아닌 결과 — 원티드 반영의 '못 넣는 원티드와 이유')
    extra: List[str] = field(default_factory=list)


@dataclass
class _Run:
    """요청 하나를 푸는 동안 같이 들고 다니는 재료."""

    ctx: EditContext
    rules: RuleSet
    requests: List[EditRequest]
    before: Dict
    soft_before: Dict
    screen: Optional[ScreenBaseline]
    max_candidates: int
    deadline: float
    #: 바꿀 수 있는 첫 날 = 요청한 가장 이른 날. 그 앞은 이미 확정·근무한 날로 본다.
    start: int = 0
    #: 사용자가 레버로 허용한 넘김 {규칙 위치 key: 양}
    allow: Dict[Tuple, int] = field(default_factory=dict)
    #: 바뀌는 칸 수 상한(고르게 맞추기면 넓다)
    max_changes: int = MAX_CHANGES
    avoid: List[Set[str]] = field(default_factory=list)
    hint: Optional[Grid] = None

    def seconds(self, cap: float) -> float:
        return max(0.0, min(cap, self.deadline - time.monotonic()))


def propose_edits(ctx: EditContext, requests: Sequence[EditRequest], *, rules: Optional[RuleSet] = None,
                  screen: Optional[ScreenBaseline] = None, max_candidates: int = 3,
                  time_budget: float = TIME_BUDGET, today: Optional[date] = None,
                  allow: Optional[Dict[Tuple, int]] = None, avoid_people: Sequence[FrozenSet[str]] = (),
                  hint: Optional[Grid] = None, explain: bool = True) -> EditResult:
    """allow: 사용자가 고른 레버의 넘김 허용량(하드락이 아닌 규칙만 — `levers.LEVER_FAMILIES`).
    avoid_people: 같은 대화에서 넘긴 후보의 '바뀐 사람 묶음' — 다시 내밀지 않는다.
    hint: 고른 후보 격자 — 이어지는 요청의 시작 해(CP-SAT 힌트, 해의 정당성과 무관).
    explain=False: 불가여도 이유·레버를 찾지 않는다(개수 요청의 메우는 방식 미리 풀기처럼 여러 번 돌릴 때)."""
    requests = list(requests)
    open_from = first_open_day(ctx, today or datetime.now(KST).date())
    problems = [p for p in (request_problem(ctx, r, open_from) for r in requests) if p]
    if problems:
        return EditResult("invalid", reasons=problems, notes=list(ctx.notes))
    if all(is_satisfied(ctx, r) for r in requests):
        return EditResult("already", notes=list(ctx.notes))
    ctx, rules = _release(ctx, rules, requests)
    leave = {(r.nurse_id, r.day): r.code for r in requests if isinstance(r, LeaveCell)}
    wished = _wished_night_before(ctx, leave, requests)
    if wished:
        return EditResult("invalid", reasons=wished, notes=list(ctx.notes))
    ctx, rules, requests = _with_leave(ctx, rules, requests, leave)
    run = _Run(ctx, rules, requests, measure(ctx, ctx.main, rules), measure_soft(ctx, ctx.main, rules),
               screen, max_candidates,
               time.monotonic() + time_budget, start=min(r.day for r in requests),
               allow=lever_allow(allow),
               avoid=[set(p) for p in avoid_people], hint=hint, max_changes=_max_changes(requests))
    status = cp_model.UNKNOWN
    # 개수 요청은 어느 날을 바꿀지 정해지지 않았다 — 좁은 창부터 넓힐 근거가 없어 기간 전체로 바로 푼다
    steps = ((None,) if any(isinstance(r, (CountTarget, BalanceTarget, WishTarget, ShapeTarget)) for r in requests)
             else WINDOW_STEPS)
    for step in steps:
        days = _window_days(run, step)
        em = build_model(ctx, rules, requests, free_cells(ctx, days), run.max_changes, run.allow)
        _carry_over(em, run)
        first = (WISH_FIRST_SECONDS if any(isinstance(r, WishTarget) for r in requests) else MONTH_SECONDS) \
            if step is None else SOLVE_SECONDS
        found, status = _search(run, em, (days[0], days[-1]), first)
        if found and step is not None:
            found = _wider(run, found) or found
        if found:
            # 후보가 있어도 바꾸는 칸이 많으면 — 설정 하나를 이번만 넘겨 더 적게 바꿀 수 있는지(그때만 카드로)
            shortcuts = _shortcuts(run, found) if explain else []
            return EditResult("ok", candidates=[_as_leave(c, leave) for c in found] if leave else found,
                              notes=list(ctx.notes), levers=shortcuts)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return EditResult("infeasible", notes=list(ctx.notes), reasons=[
            "찾은 방법이 모두 근무표 화면에 새 위반을 만들어 후보로 내지 않았습니다."])
    if not explain:
        return EditResult("infeasible" if status == cp_model.INFEASIBLE else "timeout", notes=list(ctx.notes),
                          reasons=[f"지금 규칙을 모두 지키면서는 방법을 찾지 못했습니다(바꾸는 칸 {run.max_changes}개까지 살펴봄)."
                                   if status == cp_model.INFEASIBLE else "시간 안에 방법을 찾지 못했습니다."])
    return _no_candidate(run, proven=status == cp_model.INFEASIBLE)


#: 좁은 창에서 찾은 뒤 한 달 전체에 더 나은 안이 있는지 보는 첫 풀이 상한(초)
WIDER_SECONDS = 2.0


def _wider(run: "_Run", found: List["Candidate"]) -> Optional[List["Candidate"]]:
    """좁은 창에서 찾은 안보다 주 loss(2×칸 + 기간)가 작은 안이 한 달 전체에 있으면 그것으로 — 좁은 창부터 푸는 건
    빨리 찾으려는 것이지 더 나은 안을 버리려는 게 아니다(4차 평가 11-03: 김태희 17일 OFF 가 17~23일 창에서 18칸,
    한 달 전체로는 7칸). 시간이 없거나 더 나은 안이 없으면 None."""
    if run.seconds(WIDER_SECONDS) < 0.5:
        return None
    days = _window_days(run, None)
    em = build_model(run.ctx, run.rules, run.requests, free_cells(run.ctx, days), run.max_changes, run.allow)
    _carry_over(em, run)
    em.model.Add(em.loss() <= min(c.loss for c in found) - 1)
    better, _status = _search(run, em, (days[0], days[-1]), WIDER_SECONDS)
    return better or None


#: 가장 적게 바꾸는 안이 이 칸 수 이상일 때만 '설정을 이번만 넘기면 더 적게'를 찾는다 — 작은 안은 줄일 여지가 적고
#: 찾는 시간(최대 SHORTCUT_SECONDS)만 든다(사용자 제안 11-03 · 4차 평가 'OFF 하나에 18칸')
SHORTCUT_MIN_CHANGES = 5
SHORTCUT_SECONDS = 5.0
#: 레버를 찾고 나서 답을 만들 몫으로 남길 시간(초)
SHORTCUT_RESERVE = 1.5


def _shortcuts(run: "_Run", found: List["Candidate"]) -> List[Any]:
    """후보보다 바꾸는 칸이 적어지는 레버(레버 대상 설정만 · 하드락은 넘기지 않는다). 줄어들지 않으면 빈 목록 —
    고르게 맞추기는 칸 상한이 레버 모델(20칸)보다 넓어 비교가 맞지 않아 찾지 않는다."""
    best = min(found, key=lambda c: (c.changed_count, c.loss))
    if best.changed_count < SHORTCUT_MIN_CHANGES or any(isinstance(r, (BalanceTarget, WishTarget, ShapeTarget))
                                                        for r in run.requests):
        return []
    seconds = run.seconds(SHORTCUT_SECONDS + SHORTCUT_RESERVE) - SHORTCUT_RESERVE
    if seconds < 1.0:
        return []
    return find_levers(run.ctx, run.requests, best, rules=run.rules, allow=run.allow, max_levers=2,
                       seconds=seconds, screen=run.screen, avoid_people=run.avoid)


def _max_changes(requests: Sequence[EditRequest]) -> int:
    if any(isinstance(r, WishTarget) for r in requests):
        return WISH_MAX_CHANGES
    if any(isinstance(r, ShapeTarget) for r in requests):
        return SHAPE_MAX_CHANGES
    base = BALANCE_MAX_CHANGES if any(isinstance(r, BalanceTarget) for r in requests) else MAX_CHANGES
    # 지정한 칸 자체가 많으면(버전 되돌리기 · 11-04) 그 칸 + 메울 몫 — 평소 요청(칸 몇 개)은 그대로
    cells = sum(1 for r in requests if isinstance(r, (SetCell, LeaveCell)))
    return max(base, cells + CELL_EXTRA_CHANGES) if cells > MANY_CELLS else base


#: 지정한 칸이 이보다 많으면 칸 상한을 그 칸 수 + CELL_EXTRA_CHANGES 로(그 칸 말고 메우기 몫)
MANY_CELLS = 5
CELL_EXTRA_CHANGES = 10


#: 원티드 반영의 바뀌는 칸 상한 — 넣을 원티드 칸 자체가 수십 개라 평소(20)보다 넓게
WISH_MAX_CHANGES = 60
#: 원티드 반영에서 '그 수 이상 넣으며 가장 적게 바꾸기' 첫 풀이 상한(초) — 가장 많이 넣은 해를 시작 해로 받아 첫 해는 바로
#: 나오고, 남는 시간은 칸 줄이기에 쓴다. 평소(6초)대로면 응답 시간 안에 못 넣는 이유·레버까지 못 냈다(dev 전원 37개 실측)
WISH_FIRST_SECONDS = 4.0


def max_wish_count(ctx: EditContext, requests: Sequence[EditRequest], *, rules: Optional[RuleSet] = None,
                   today: Optional[date] = None, seconds: float = MONTH_SECONDS,
                   allow: Optional[Dict[Tuple, int]] = None,
                   avoid_people: Sequence[FrozenSet[str]] = ()) -> Tuple[Optional[int], bool, int, Optional[Grid]]:
    """넣을 수를 정하지 않은 원티드 반영 — 규칙을 모두 지키며 칸 상한 안에서 넣을 수 있는 가장 많은 원티드 수.
    (수, 더 넣을 수 없다고 증명했나, 대상 수, 그 해의 격자). 시간 안에 하나도 못 찾으면 수는 None.
    격자는 다음 탐색(그 수 이상 넣으며 가장 적게 바꾸기)의 시작 해로 쓴다 — 없으면 전원 반영에서 시간 안에 못 찾았다(dev 실측)."""
    requests = list(requests)
    req = next((r for r in requests if isinstance(r, WishTarget) and r.at_least is None), None)
    if req is None:
        return None, False, 0, None
    total = len(wish_targets(ctx, req))
    open_from = first_open_day(ctx, today or datetime.now(KST).date())
    if not total or any(request_problem(ctx, r, open_from) for r in requests):
        return (0 if not total else None), True, total, None
    pctx, prules, preqs = prepare(ctx, rules, requests)
    start = min(r.day for r in preqs)
    em = build_model(pctx, prules, preqs, free_cells(pctx, range(start, pctx.days)), WISH_MAX_CHANGES, lever_allow(allow))
    for people in avoid_people:            # 넘긴 후보의 사람 묶음은 후보 탐색처럼 빼고 센다(검토 11-03 M2)
        forbid_same_people(em, set(people))
    em.model.Maximize(em.wish)
    solver = new_solver(seconds)
    status = solver.Solve(em.model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, status == cp_model.INFEASIBLE, total, None
    return int(round(solver.Value(em.wish))), status == cp_model.OPTIMAL, total, read_solution(em, solver)


#: 근무 모양 고치기의 바뀌는 칸 상한 — 병동 전체의 하루짜리 OFF 처럼 모양 하나를 고치는 데 칸 2~3개가 든다
SHAPE_MAX_CHANGES = 40


def min_shape_count(ctx: EditContext, requests: Sequence[EditRequest], *, target: Optional[ShapeTarget] = None,
                    rules: Optional[RuleSet] = None, today: Optional[date] = None, seconds: float = MONTH_SECONDS,
                    allow: Optional[Dict[Tuple, int]] = None,
                    avoid_people: Sequence[FrozenSet[str]] = ()) -> Tuple[Optional[int], bool, int, Optional[Grid]]:
    """줄일 수를 정하지 않은 근무 모양(개수 종류 · target, 없으면 첫 것) — 규칙을 모두 지키며 칸 상한 안에서 줄일 수 있는
    가장 작은 개수. (수, 더 줄일 수 없다고 증명했나, 지금 개수, 그 해의 격자) — `max_wish_count` 와 같은 틀. 격자는 다음
    탐색(그 수 이하로 가장 적게 바꾸기)의 시작 해. 시간 안에 못 찾으면 수는 None. 아직 정하지 않은 다른 모양 요청은
    지금보다 늘지만 않게 묶인다(검토 11-04 M2 — 한 식을 덮어써 엉뚱한 모양을 줄였다)."""
    requests = list(requests)
    req = target or next((r for r in requests if isinstance(r, ShapeTarget) and r.at_most is None), None)
    if req is None or req.at_most is not None:
        return None, False, 0, None
    open_from = first_open_day(ctx, today or datetime.now(KST).date())
    if any(request_problem(ctx, r, open_from) for r in requests):
        return None, True, shape_count(ctx, ctx.main, req), None
    pctx, prules, preqs = prepare(ctx, rules, requests)
    now = shape_count(pctx, pctx.main, req)                  # 함께 온 휴가 요청도 풀 때와 같은 재료로(검토 L1)
    if not now:
        return 0, True, 0, None
    start = min(r.day for r in preqs)
    em = build_model(pctx, prules, preqs, free_cells(pctx, range(start, pctx.days)), SHAPE_MAX_CHANGES,
                     lever_allow(allow))
    for people in avoid_people:
        forbid_same_people(em, set(people))
    em.model.Minimize(em.shape[req])
    solver = new_solver(seconds)
    status = solver.Solve(em.model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, status == cp_model.INFEASIBLE, now, None
    return int(round(solver.Value(em.shape[req]))), status == cp_model.OPTIMAL, now, read_solution(em, solver)


def min_balance_spread(ctx: EditContext, requests: Sequence[EditRequest], *, rules: Optional[RuleSet] = None,
                       today: Optional[date] = None, seconds: float = MONTH_SECONDS,
                       allow: Optional[Dict[Tuple, int]] = None) -> Tuple[Optional[int], bool]:
    """차이를 정하지 않은 고르게 맞추기 — 규칙을 모두 지키며 칸 상한 안에서 줄일 수 있는 가장 작은 최대−최소 차이.

    (차이, 더 줄일 수 없다고 증명했나). 지금 차이−1 부터 한 단계씩 '되는지만' 짧게 풀어 내려간다 — 차이를 목적식으로
    한 번에 줄이면 실측(dev 3병동 N)에서 6초 안에 개선을 못 찾았다. 하나도 못 줄이면 (None, 증명 여부).
    산술 하한(합을 사람 수로 나눈 나머지가 있으면 1, 없으면 0)보다 아래는 풀지 않는다.
    """
    requests = list(requests)
    req = next((r for r in requests if isinstance(r, BalanceTarget) and r.spread is None and r.target is None), None)
    if req is None:
        return None, False
    open_from = first_open_day(ctx, today or datetime.now(KST).date())
    counts = list(balance_counts(ctx, ctx.main, req).values())
    if not counts or any(request_problem(ctx, r, open_from) for r in requests):
        return None, False
    ctx, rules, requests = prepare(ctx, rules, requests)          # 함께 온 휴가 요청도 풀 때와 같은 재료로(검토 L2)
    now = max(counts) - min(counts)
    floor = 0 if sum(counts) % len(counts) == 0 else 1
    deadline = time.monotonic() + seconds
    start = min(r.day for r in requests)
    found, proven = None, False
    for spread in range(now - 1, floor - 1, -1):
        left = deadline - time.monotonic()
        if left < 0.3:
            break
        tried = [dataclasses.replace(r, spread=spread) if r is req else r for r in requests]
        em = build_model(ctx, rules, tried, free_cells(ctx, range(start, ctx.days)), BALANCE_MAX_CHANGES,
                         lever_allow(allow))
        status = new_solver(min(SOLVE_SECONDS, left)).Solve(em.model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            found = spread
            proven = spread == floor
            continue
        proven = status == cp_model.INFEASIBLE
        break
    return found, proven


def prepare(ctx: EditContext, rules: Optional[RuleSet],
            requests: Sequence[EditRequest]) -> Tuple[EditContext, RuleSet, List[EditRequest]]:
    """풀 때와 같은 재료·규칙·요청 — 직접 짚은 확정 원티드를 풀고, 휴가 요청 칸은 휴가로 넣어 OFF 지정으로 바꾼다.
    바꾸기 전 산술 점검(`precheck`)이 솔버와 같은 식으로 센다."""
    ctx, rules = _release(ctx, rules, requests)
    leave = {(r.nurse_id, r.day): r.code for r in requests if isinstance(r, LeaveCell)}
    return _with_leave(ctx, rules, list(requests), leave)


def _with_leave(ctx: EditContext, rules: Optional[RuleSet], requests: List[EditRequest],
                leave: Dict[Tuple[str, int], str]) -> Tuple[EditContext, RuleSet, List[EditRequest]]:
    if leave:
        # 연차·휴가 칸 — 그 칸을 휴가로 넣은 재료로 OFF 를 지정해 푼다(순수 OFF·패턴 판정이 휴가로 본다 · 빠진 근무는
        # 다른 사람이 메운다). 후보에서 그 칸만 휴가 코드로 바꾼다(`_as_leave`)
        ctx = with_vacation(ctx, leave)
        rules = None
        requests = [SetCell(r.nurse_id, r.day, "O") if isinstance(r, LeaveCell) else r for r in requests]
        if ctx.config.get("ban_night_before_fixed_off"):
            # 휴가 전날 N 금지(설정) — 지금 전날이 N 이면 그 N 을 빼야 한다(기준선 위반으로 묻히지 않게 · 검토 M1).
            # 바꿀 수 없는 전날 N 은 요청 검사가 미리 거절한다
            requests += [AvoidCell(n, d - 1, frozenset({"N"})) for n, d in leave
                         if d >= 1 and (n, d - 1) not in leave and ctx.main[n][d - 1] == "N"]
    return ctx, rules or build_rules(ctx), requests


def _release(ctx: EditContext, rules: Optional[RuleSet], requests: Sequence[EditRequest]):
    """직접 짚어 바꿔야 하는 칸의 확정 원티드만 푼 재료와 규칙. 풀 칸이 없으면 그대로(규칙도 다시 만들지 않는다).
    이미 그렇게 된 칸은 풀지 않는다 — 기간 OFF 안의 원래 OFF 원티드를 풀면 그 '전날 N 금지'가 빠진다(검토 11-03)."""
    direct = requested_cells(ctx, list(requests))
    out = with_released(ctx, direct)
    if out is ctx:
        return ctx, rules
    return out, (None if released_wanted(ctx, direct) else rules)     # 고정을 풀었을 때만 규칙을 다시 만든다


def _wished_night_before(ctx: EditContext, leave: Dict[Tuple[str, int], str],
                         requests: Sequence[EditRequest]) -> List[str]:
    """휴가 전날 N 금지(설정)로 바꿔야 할 전날 N 이 원티드로 받은 칸이면 — 짚지 않은 원티드 칸은 바꾸지 않는다(사용자 지시
    11-03). 그 나이트도 함께 짚으면 푼다."""
    if not ctx.config.get("ban_night_before_fixed_off"):
        return []
    named = requested_cells(ctx, list(requests))
    return [f"{ctx.names.get(n, n)} {d}일 나이트는 원티드가 반영된 칸이라 {d + 1}일에 휴가를 넣으려면 그 나이트도 바꿔야 합니다"
            f"(휴가 전날 나이트 금지 설정). 바꿔도 되면 '{d}일 나이트도 빼줘'라고 함께 말씀해 주세요."
            for n, d in sorted(leave) if d >= 1 and (n, d - 1) not in leave and (n, d - 1) not in named
            and ctx.main[n][d - 1] == "N" and "N" in ctx.wishes.get((n, d - 1), ())]


def _as_leave(cand: Candidate, leave: Dict[Tuple[str, int], str]) -> Candidate:
    """휴가 요청 칸은 OFF 로 풀었다 — 저장할 코드는 휴가 코드, 재확인은 그 칸을 휴가로 센다(CellChange.vacation)."""
    for ch in cand.changes:
        code = leave.get((ch.nurse_id, ch.day))
        if code is not None:
            ch.after, ch.vacation = code, True
    return cand


def _window_days(run: _Run, step: Optional[int]) -> List[int]:
    """요청한 가장 이른 날부터 **뒤로만** 넓힌다 — 그 앞 날짜는 바꾸지 않는다(규칙 판정에는 상수로 들어간다)."""
    last = run.ctx.days - 1
    hi = last if step is None else min(last, max(getattr(r, "last_day", r.day) for r in run.requests) + step)
    return list(range(run.start, hi + 1))


def _carry_over(em: EditModel, run: _Run) -> None:
    """대화에서 이어지는 것 — 넘긴 사람 묶음은 금지, 고른 후보는 시작 해."""
    for people in run.avoid:
        forbid_same_people(em, people)
    if run.hint is not None:
        for (n, d, c), var in em.x.items():
            em.model.AddHint(var, 1 if run.hint[n][d] == c else 0)


def _search(run: _Run, em: EditModel, window: Tuple[int, int], first_seconds: float):
    """창 하나 — d* 를 구하고, d* 로 첫 후보, d*+EXTRA 안에서 바뀐 사람이 다른 대안."""
    em.model.Minimize(em.loss())
    status, solver = _solve(em, run.seconds(first_seconds))
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], status
    best = int(round(solver.ObjectiveValue()))
    optimal = status == cp_model.OPTIMAL
    fallback = read_solution(em, solver)
    tight, loose = _change_bounds(em, best)
    em.model.AddAssumptions([tight])
    em.model.Minimize(QUALITY_WEIGHT * em.total_quality() + 10 * em.total_affected() + sum(subcode_changes(em)))
    found = _collect(run, em, window, loose, optimal)
    if not found:
        # 사람 수를 줄이는 다듬기가 시간 안에 안 끝났다(한 달 크기에서 실측) — 첫 풀이(최소 칸 수) 해를 낸다
        first = _candidate(run, fallback, 1, window, optimal)
        found = [first] if first is not None else []
    return found, status


AVOIDED_ONLY = ("넘기신 후보와 같은 사람들을 바꾸는 방법 말고는 찾지 못했습니다. 넘긴 후보를 다시 보시려면 "
                "요청을 새로 해 주세요.")


def _no_candidate(run: _Run, proven: bool) -> EditResult:
    """후보가 없을 때 — 한 달 전체가 불가로 증명됐으면 이유를, 아니면 시간 초과를 알린다."""
    notes = list(run.ctx.notes)
    if not proven:
        return EditResult("timeout", notes=notes, reasons=[
            "시간 안에 방법을 찾지 못했습니다. 요청을 나눠서 다시 시도해 주세요."])
    days = _window_days(run, None)
    head = (f"지금 규칙을 모두 지키면서는 이 요청을 들어줄 방법을 찾지 못했습니다(바꾸는 칸 {run.max_changes}개까지 "
            "살펴봄).")
    direct = direct_conflicts(run.ctx, run.rules, run.requests, days)
    if direct:
        # 바꿀 수 없는 칸과 바로 부딪친다 — 풀이로 이유를 찾을 필요가 없다(하드락이라 넘김 안내도 없다)
        return EditResult("infeasible", notes=notes, reasons=[head] + [text for _f, text in direct],
                          blocking=sorted({family for family, _t in direct}))
    why = explain_month(run.ctx, run.rules, run.requests, run.max_changes, run.seconds(EXPLAIN_SECONDS), days=days,
                        allow=run.allow)
    if why.status == "found" and run.avoid:
        # 이유 찾기는 넘긴 묶음을 모른다 — 규칙을 다 지키는 해가 있는데 후보가 없었다면 넘긴 묶음 때문이다
        return EditResult("infeasible", notes=notes, reasons=[AVOIDED_ONLY])
    levers = []
    if why.status == "bends" and run.seconds(LEVER_SECONDS) > 0.5:
        # 이유만이 아니라 '이걸 허용하면 가능' 을 고를 수 있게 — 레버 대상(하드락 아님) 넘김만
        levers = find_levers(run.ctx, run.requests, None, rules=run.rules, allow=run.allow,
                             max_levers=2, seconds=run.seconds(LEVER_SECONDS), screen=run.screen,
                             avoid_people=run.avoid)
    return EditResult("infeasible", notes=notes, blocking=why.families, levers=levers,
                      reasons=[head] + why.sentences(run.ctx.names, run.max_changes, list_bends=not levers))


def _collect(run: _Run, em: EditModel, window: Tuple[int, int], loose, first_optimal: bool) -> List[Candidate]:
    """후보를 모은다. 화면에서 나빠지는 해는 버리고 다음 대안을 찾는다(시도는 후보 수의 2배까지).

    버린 해의 '바뀐 사람 집합'도 다시 시도하지 않는다 — 같은 사람으로 칸만 바꾼 해는 대개 같은 이유로 걸린다.
    """
    out: List[Candidate] = []
    for _ in range(run.max_candidates * 2):
        if len(out) >= run.max_candidates or run.seconds(SOLVE_SECONDS) <= 0:
            break
        status, solver = _solve(em, run.seconds(SOLVE_SECONDS))
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        grid = read_solution(em, solver)
        people = {n for n, d in em.free if grid[n][d] != em.base[n][d]}
        if not people:
            break
        cand = _candidate(run, grid, len(out) + 1, window,
                          first_optimal and not out and status == cp_model.OPTIMAL)
        if cand is not None:
            out.append(cand)
        forbid_same_people(em, people)
        _switch_to_alternatives(em, loose)
    return out


def _change_bounds(em: EditModel, best: int):
    """주 loss 상한 둘 — 가정으로 켜고 끈다(한 번 Add 하면 뒤 단계에서 못 푼다)."""
    tight, loose = em.model.NewBoolVar("tight"), em.model.NewBoolVar("loose")
    em.model.Add(em.loss() <= best).OnlyEnforceIf(tight)
    em.model.Add(em.loss() <= best + EXTRA_LOSS).OnlyEnforceIf(loose)
    return tight, loose


def _switch_to_alternatives(em: EditModel, loose) -> None:
    """두 번째 후보부터는 최소 loss + EXTRA 까지 허용하고, 그 안에서 다시 loss 가 작은 순으로 고른다."""
    em.model.ClearAssumptions()
    em.model.AddAssumptions([loose])
    em.model.Minimize(LOSS_WEIGHT * em.loss() + QUALITY_WEIGHT * em.total_quality()
                      + 10 * em.total_affected() + sum(subcode_changes(em)))


def _solve(em: EditModel, seconds: float):
    solver = new_solver(seconds)
    return solver.Solve(em.model), solver


def _candidate(run: _Run, grid: Grid, rank: int, window: Tuple[int, int],
               optimal: bool) -> Optional[Candidate]:
    ctx = run.ctx
    cells = sorted((n, d) for n in ctx.nurse_ids for d in range(ctx.days) if grid[n][d] != ctx.main[n][d])
    if not cells:
        return None
    worse = rule_regressions(ctx, run.rules, grid, run.requests, run.allow, run.before)
    if worse:
        # 모델과 검증기가 같은 부등식을 쓰므로 여기 오면 코드 결함이다 — 후보로 내지 않는다
        logger.error("[schedule_edit] 모델 해가 검증에서 새 위반: %s", worse[:5])
        return None
    changes = [cell_change(ctx, n, d, grid[n][d]) for n, d in cells]
    regress = screen_problems(run.screen, changes, {**run.allow, **grid_relief(ctx, run.rules, run.requests, grid)})
    if regress:
        logger.info("[schedule_edit] 화면에서 나빠져 후보 제외: %s", regress[:3])
        return None
    people = sorted({c.nurse_id for c in changes})
    days = sorted({d for _n, d in cells})
    span = days[-1] - days[0]
    return Candidate(rank=rank, changes=changes, affected=people, optimal=optimal, window=window,
                     counts={n: _counts(ctx, n, grid) for n in people},
                     quality=_quality_notes(run, grid), span=(days[0], days[-1]),
                     loss=CHANGE_COEF * len(changes) + SPAN_COEF * span)


def rule_regressions(ctx: EditContext, rules: RuleSet, grid: Grid, requests: Sequence[EditRequest],
                     allow: Dict[Tuple, int], before: Optional[Dict] = None) -> List[Tuple]:
    """엔진 기준으로 지금보다 나빠진 곳(요청 칸의 원티드·허용 레버만큼은 뺀다)."""
    waived = requested_cells(ctx, list(requests))
    relief = grid_relief(ctx, rules, list(requests), grid)    # '그날 인원 줄이기' 로 실제 빠진 날만
    base = before if before is not None else measure(ctx, ctx.main, rules)
    return sorted((k, v) for k, v in new_violations(base, measure(ctx, grid, rules)).items()
                  if not is_waived(k, waived) and v[1] > v[0] + allow.get(k, 0) + relief.get(k, 0))


def screen_problems(screen: Optional[ScreenBaseline], changes: Sequence["CellChange"],
                    allow: Dict[Tuple, int]) -> List[Tuple]:
    """화면 기준으로 새로 생기거나 심해지는 빨간 칸(허용 레버의 인원 넘김은 뺀다)."""
    if screen is None:
        return []
    return [v.key for v in screen_regressions(screen, ((c.nurse_id, c.day, c.after) for c in changes))
            if not _allowed_on_screen(v, allow)]


def verify_candidate(ctx: EditContext, rules: RuleSet, candidate: "Candidate", requests: Sequence[EditRequest], *,
                     allow: Optional[Dict[Tuple, int]] = None, screen: Optional[ScreenBaseline] = None,
                     today: Optional[date] = None) -> List[str]:
    """적용 직전 재확인 — 요청 충족·지난 날짜·엔진 기준·화면 기준. 문제가 없으면 빈 목록.

    후보를 찾은 뒤 자정을 넘기면 그사이 지난 날이 된 칸을 바꾸는 후보가 될 수 있어 날짜도 다시 본다.
    """
    allow = lever_allow(allow)
    ctx, rules, grid, after_ctx, after_rules = _after(ctx, rules, candidate, requests)
    open_from = first_open_day(ctx, today or datetime.now(KST).date())
    found = [f"{c.day + 1}일은 이미 지난 날짜입니다" for c in candidate.changes if c.day < open_from][:1]
    # 묶인 칸(확정 원티드·파견 등)이 바뀌었으면 — 직접 짚은 확정 원티드 칸은 위에서 풀었다
    found += [f"{c.day + 1}일은 '{ctx.frozen[(c.nurse_id, c.day)]}' 칸입니다" for c in candidate.changes
              if not ctx.is_free(c.nurse_id, c.day)][:1]
    found += [f"요청 미충족 {r}" for r in requests if not is_satisfied(ctx, r, grid)]
    # 휴가 요청 칸에 정말 그 휴가 코드가 들어갔는가(검토 L6)
    found += [f"요청 미충족 {r}" for r in requests if isinstance(r, LeaveCell) and not any(
        (c.nurse_id, c.day, c.after) == (r.nurse_id, r.day, r.code) for c in candidate.changes)]
    base = measure(ctx, ctx.main, rules)
    found += [f"{k[0]} {k}" for k, _v in rule_regressions(after_ctx, after_rules, grid, requests, allow, before=base)]
    screen_allow = {**allow, **grid_relief(ctx, rules, list(requests), grid)}
    return found + [f"화면 {k}" for k in screen_problems(screen, candidate.changes, screen_allow)]


def _after(ctx: EditContext, rules: Optional[RuleSet], candidate: "Candidate", requests: Sequence[EditRequest]):
    """적용 판정 재료 — (풀 때 재료, 규칙, 적용한 격자, 적용 뒤를 셀 재료, 규칙).
    휴가로 바뀐 칸은 대표코드가 O 그대로라 그대로 세면 순수 OFF 하한 위반을 놓친다(Codex 11-03) —
    그 칸을 휴가로 넣은 재료로 규칙을 다시 세어, 지금(원래 재료) 값과 견준다."""
    ctx, rules = _release(ctx, rules, requests)
    rules = rules or build_rules(ctx)
    grid = apply_candidate(ctx, candidate)
    vacation = {(c.nurse_id, c.day) for c in candidate.changes if c.vacation}
    if not vacation:
        return ctx, rules, grid, ctx, rules
    after_ctx = with_vacation(ctx, vacation)
    return ctx, rules, grid, after_ctx, build_rules(after_ctx)


def before_after(ctx: EditContext, rules: Optional[RuleSet], candidate: "Candidate",
                 requests: Sequence[EditRequest]) -> Tuple[RuleSet, Dict[Tuple, int], Dict[Tuple, int]]:
    """(풀 때 규칙, 지금 근무표의 위반량, 이 후보의 위반량) — 적용 전 확인과 규칙 질문(`rule_check`)이 같이 쓴다."""
    ctx, rules, grid, after_ctx, after_rules = _after(ctx, rules, candidate, requests)
    return rules, measure(ctx, ctx.main, rules), measure(after_ctx, grid, after_rules)


def candidate_regressions(ctx: EditContext, rules: Optional[RuleSet], candidate: "Candidate",
                          requests: Sequence[EditRequest]) -> Dict[Tuple, Tuple[int, int]]:
    """이 후보로 지금보다 나빠지는 규칙 위치 전부 {위치: (전, 후)} — 고른 레버·그날 인원 요청·직접 짚은 원티드로
    허용한 것도 빼지 않는다(적용 전 확인에서 '무엇을 감수하는지' 보여 줄 것 · 사용자 지시 11-03)."""
    _rules, before, after = before_after(ctx, rules, candidate, requests)
    return new_violations(before, after)


def _allowed_on_screen(v, allow: Dict[Tuple, int]) -> bool:
    """레버로 허용한 인원 넘김은 화면 미달·초과로도 나온다 — 같은 위치면 받아들인다."""
    family = {"manpower_shortage": "인원최소", "manpower_excess": "인원최대"}.get(v.type)
    return family is not None and (family, None, v.day, v.shift) in allow


def _quality_notes(run: _Run, grid: Grid) -> List[str]:
    return quality_notes(run.ctx, run.rules, grid, run.soft_before)


def quality_notes(ctx: EditContext, rules: RuleSet, grid: Grid, soft_before: Optional[Dict] = None) -> List[str]:
    """이 근무표로 나빠지는 품질 — 가로축 분배 지표 먼저, 그다음 새로 생기는 근무 모양(N-O-D 등)."""
    if soft_before is None:
        soft_before = measure_soft(ctx, ctx.main, rules)
    before, after = dist.metrics(ctx, ctx.main), dist.metrics(ctx, grid)
    out = [f"{dist.METRIC_LABEL[k]} {before[k]}→{after[k]}" for k in before if after[k] > before[k]]
    worse = new_violations(soft_before, measure_soft(ctx, grid, rules))
    for key in sorted(worse, key=str):
        family, nurse, day, extra = (list(key) + [None] * 4)[:4]
        label = SOFT_LABEL.get(family, family)
        who = f"{ctx.names.get(nurse, nurse)} " if nurse else ""
        if family == "인원초과":
            out.append(f"{day + 1}일 {extra} {label}")            # '11일 E 필요 인원보다 많음'
        else:
            out.append(f"{who}{day + 1}일 {label}" + (f" {extra}" if family == "패턴" else ""))
    return out


SOFT_LABEL = {"패턴": "근무 순서", "고립OFF": "하루짜리 OFF", "연속OFF": "연속 OFF 상한 초과",
              "주2회OFF": "주 OFF 2회 미만", "인원초과": "필요 인원보다 많음"}


def cell_change(ctx: EditContext, nurse_id: str, day: int, after_main: str) -> CellChange:
    return CellChange(
        nurse_id=nurse_id, name=ctx.names.get(nurse_id, nurse_id), day=day,
        before=ctx.raw[nurse_id][day], after=str(ctx.rep_code.get(after_main, after_main)),
        before_main=ctx.main[nurse_id][day], after_main=after_main,
    )


def _counts(ctx: EditContext, nurse_id: str, grid: Grid) -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = {}
    for code in ("D", "E", "N", "M"):
        b = sum(1 for m in ctx.main[nurse_id] if m == code)
        a = sum(1 for m in grid[nurse_id] if m == code)
        if a or b:
            out[code] = (b, a)
    out["OFF"] = (pure_off_count(ctx, nurse_id, ctx.main[nurse_id]),
                  pure_off_count(ctx, nurse_id, grid[nurse_id]))
    return out


def apply_candidate(ctx: EditContext, candidate: Candidate) -> Grid:
    """후보를 적용한 격자(대표코드). 저장은 하지 않는다."""
    grid = {n: list(row) for n, row in ctx.main.items()}
    for ch in candidate.changes:
        grid[ch.nurse_id][ch.day] = ch.after_main
    return grid
