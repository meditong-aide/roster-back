"""수정 요청의 기본 단위 — 칸 지정·칸 금지·개수. 교환·기간 OFF 는 칸 지정으로 풀어 만든다.

자연어 해석(이름→nurse_id, '나이트'→N, 날짜)은 스킬 층이 한다. 여기는 해석이 끝난 값만 받는다.
day 는 0부터(1일 = 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple, Union

from services.schedule_edit.context import EDIT_MAINS, FIXED_WANTED, EditContext


@dataclass(frozen=True)
class SetCell:
    """이 칸을 이 근무로 — 예) 김민지 14일 OFF."""

    nurse_id: str
    day: int
    code: str


@dataclass(frozen=True)
class AvoidCell:
    """이 칸은 이 근무들이 아니게 — 예) 김민지 14일 나이트 빼기(무엇으로 바꿀지는 솔버가 고른다)."""

    nurse_id: str
    day: int
    codes: FrozenSet[str]


def eul(word: str) -> str:
    """목적격 조사 — 받침이 있으면 '을', 없으면 '를'('연차를' · '공가를' · '생휴를')."""
    last = word[-1:] if word else ""
    if "가" <= last <= "힣":
        return word + ("을" if (ord(last) - 0xAC00) % 28 else "를")
    return word + "을(를)"


@dataclass(frozen=True)
class LeaveCell:
    """이 칸을 휴가·공가 코드로 — 예) 김민주 25일 연차. 쉬는 날이지만 순수 OFF 로 세지 않는다.

    근무일에만 넣는다 — 이미 쉬는 날(O)에 넣으면 순수 OFF 가 줄어 OFF 수 판정이 솔버·재확인에서 어긋난다
    (남는 OFF 를 연차로 바꾸는 것은 '자동 연차전환'). 솔버는 그 칸을 휴가로 넣은 재료로 OFF 를 지정해 풀고,
    후보의 그 칸만 휴가 코드로 바꾼다(`solver.propose_edits`).
    """

    nurse_id: str
    day: int
    code: str


@dataclass(frozen=True)
class CoverTarget:
    """그날 그 근무 인원을 target 명으로 — 예) 15일 E 한 명 더 · 20일 N 에 1등급 넣기(grade).

    누구를 넣고 뺄지는 솔버가 고른다(사람을 짚지 않는다 — 원티드 보호를 면제하지 않는다). grade 가 있으면
    그 등급까지(등급≤grade)인 사람만 센다. 사용자가 그날 인원을 바꿔 달라고 했으니 그날 그 근무의 최소·최대 인원
    설정은 요청한 만큼 넘긴다(`model.coverage_relief` · 등급 요청은 넘기지 않는다).
    """

    day: int
    code: str
    target: int
    grade: Optional[int] = None

    @property
    def nurse_id(self) -> None:
        """사람을 짚지 않는다 — 칸 요청을 거르는 자리((간호사, 날) 비교)를 그대로 지나간다."""
        return None


def cover_members(ctx: EditContext, req: "CoverTarget") -> List[str]:
    """그날 인원에 세는 사람(등급 요청이면 그 등급까지)."""
    from services.schedule_edit.rules import counted_people
    people = counted_people(ctx, req.day)
    if req.grade is None:
        return people
    return [n for n in people if ctx.grade_of.get(n) is not None and ctx.grade_of[n] <= req.grade]


def cover_count(ctx: EditContext, grid: Dict[str, List], req: "CoverTarget") -> int:
    return sum(1 for n in cover_members(ctx, req) if grid[n][req.day] == req.code)


#: 개수 요청의 '상대' 를 어떻게 정할지(사용자 결정 11-02: 선택지를 미리 풀어 보여 주고 고르게 한다).
#:   요청자의 근무가 줄면(N 빼기·OFF 늘리기) 빠지는 근무를 누가 메울지, 늘면(N 더·OFF 빼기) 누구 것을 넘겨받을지.
#:   reduce_coverage = 메우지 않고 그날 인원 줄이기 · add_coverage = 넘겨받지 않고 그날 인원 늘리기(둘 다 그날 인원 설정을
#:   이번만 넘김 — 고른 것이 곧 동의).
TRANSFERS = ("same_grade", "same_team", "fair", "reduce_coverage", "add_coverage", "auto")
WORK_CODES = frozenset({"D", "E", "N", "M"})


@dataclass(frozen=True)
class CountTarget:
    """이 사람의 이 근무 개수를 target 으로(first_day~last_day) — 예) 장원영 N 2개 빼기.

    어느 날을 바꿀지는 솔버가 고른다. transfer 는 상대를 어떻게 정할지(TRANSFERS) — None 이면 아직 정하지 않은
    것(스킬이 선택지를 풀어 보여 주고 묻는다 · 솔버는 auto 와 같게 푼다).
    """

    nurse_id: str
    codes: FrozenSet[str]
    target: int
    first_day: int
    last_day: int
    transfer: Optional[str] = None

    @property
    def day(self) -> int:
        """창·지난 날짜 판정의 기준 — 기간 첫날."""
        return self.first_day

    def days(self) -> range:
        return range(self.first_day, self.last_day + 1)


@dataclass(frozen=True)
class BalanceTarget:
    """여러 사람의 이 근무 수를 고르게 — 예) 'N 근무 수 고르게 맞춰줘'(사용자 결정 11-03).

    한 달 전체 개수로 맞춘다(지난 날·바꿀 수 없는 칸에 이미 있는 근무도 센다). 대상(members)은 그 근무를 할 수
    있는 전원 — N 전담·고정 근무자·그 근무를 할 수 없는 사람은 뺀다(`balance_members`). 누구의 어느 날을 바꿀지는
    솔버가 고른다(사람·칸을 짚지 않는다 — 원티드 보호를 면제하지 않는다).
      · target: 모두 이 수로('OFF 다 9개로'). 주면 spread 는 보지 않는다.
      · spread: 최대−최소 차이 상한. None 이면 아직 정하지 않은 것 — 스킬이 줄일 수 있는 가장 작은 차이를 찾아 채운다
        (`solver.min_balance_spread`).
    """

    codes: FrozenSet[str]
    members: Tuple[str, ...]
    first_day: int
    last_day: int
    spread: Optional[int] = None
    target: Optional[int] = None

    @property
    def nurse_id(self) -> None:
        """사람을 짚지 않는다 — 칸 요청을 거르는 자리((간호사, 날) 비교)를 그대로 지나간다."""
        return None

    @property
    def day(self) -> int:
        """창·지난 날짜 판정의 기준 — 바꿀 수 있는 첫날."""
        return self.first_day


@dataclass(frozen=True)
class WishTarget:
    """반영 안 된 원티드 넣기 — 예) '반영 안 된 원티드 넣어줘' · '장원영 원티드 반영해줘'(사용자 결정 11-03).

    대상 = 기간(first_day~last_day) 안 바꿀 수 있는 칸의 근무·OFF 원티드 중 지금 반영 안 된 것(`wish_targets`).
    nurses 가 비면 전원. 누구의 어느 칸을 어떻게 바꿀지는 솔버가 고른다(이미 반영된 원티드는 계속 지킨다 — 원티드 규칙).
      · at_least: 넣을 개수 하한. None 이면 아직 정하지 않은 것 — 스킬이 규칙을 지키며 넣을 수 있는 가장 많은 수를 찾아
        채운다(`solver.max_wish_count`).
    """

    nurses: Tuple[str, ...]
    first_day: int
    last_day: int
    at_least: Optional[int] = None
    #: 사용자가 기간을 말했는가 — 말하지 않았으면 요청 줄에 날짜를 붙이지 않는다(오늘~말일이 보이면 어색하다)
    ranged: bool = False

    @property
    def nurse_id(self) -> None:
        """사람 하나를 짚지 않는다 — 칸 요청을 거르는 자리를 그대로 지나간다."""
        return None

    @property
    def day(self) -> int:
        return self.first_day


#: 근무 모양 — 사람이 '고쳐 달라'는 것(사용자 결정 11-04 '근무 모양 고치기'). 값 = 답에 쓰는 이름
SHAPE_LABEL = {"isolated_off": "하루짜리 OFF", "isolated_work": "하루 근무(퐁당퐁당)",
               "pattern": "N-O-D·N-O-E·E-O-D 순서", "work_run": "연속 근무", "weekend": "주말 근무"}
#: 그 모양의 개수를 줄이는 종류(나머지는 사람마다 상한)
SHAPE_COUNTED = ("isolated_off", "isolated_work", "pattern")
#: 근무 순서 모양(rules._patterns 와 같다) — (이름, 첫 칸, 셋째 칸)
_SEQUENCES = (("N-O-D", frozenset({"N"}), frozenset({"D"})), ("N-O-E", frozenset({"N"}), frozenset({"E"})),
              ("E-O-D", frozenset({"E"}), frozenset({"D"})))
_WORK = frozenset({"D", "E", "N", "M", "W"})
_OFF = frozenset({"O"})


@dataclass(frozen=True)
class ShapeTarget:
    """근무 모양 고치기 — 예) '하루짜리 OFF 없애줘' · '장원영 연속 근무 너무 길어' · '김태희 주말 근무 너무 많아'.

    kind 는 SHAPE_LABEL 의 키. nurses 가 비면 전원, 기간은 first_day~last_day(바꿀 수 있는 날 안). 누구의 어느 칸을
    바꿀지는 솔버가 고른다(가장 적게 바꾸기 · 하드락·설정은 그대로).
      · isolated_off·isolated_work·pattern: 기간 안 그 모양 개수를 at_most 이하로. 사용자가 수를 말했으면 said=True 로
        그 수를 그대로 쓴다. 말하지 않았으면(said=False) 스킬이 규칙을 지키며 줄일 수 있는 가장 작은 수를 턴마다 다시 찾아
        채운다(`solver.min_shape_count` · None = 아직 정하지 않음).
      · work_run: 사람마다 연속 근무를 at_most 일 이하로 · weekend: 사람마다 기간 안 주말 근무를 at_most 회 이하로
        (둘 다 말하지 않으면 해석이 채운다 — `grounding._shape`).
    바꿀 수 있는 칸이 하나도 없는 모양(지난 날·묶인 칸뿐)은 세지 않는다 — 고칠 수 없다. 주말은 바꿀 수 없는 주말 근무가
    이미 상한을 넘는 사람이면 그 수까지로 본다(`weekend_floor` · 솔버와 판정이 같은 기준).
    """

    kind: str
    nurses: Tuple[str, ...]
    first_day: int
    last_day: int
    at_most: Optional[int] = None
    ranged: bool = False
    said: bool = False

    @property
    def nurse_id(self) -> None:
        """사람 하나를 짚지 않는다 — 칸 요청을 거르는 자리를 그대로 지나간다."""
        return None

    @property
    def day(self) -> int:
        return self.first_day


EditRequest = Union[SetCell, AvoidCell, CountTarget, LeaveCell, CoverTarget, BalanceTarget, WishTarget, ShapeTarget]
#: 칸 하나를 짚지 않는 요청 — 원티드 보호 면제·칸 요청 비교에서 빠진다
AGGREGATE = (CountTarget, CoverTarget, BalanceTarget, WishTarget, ShapeTarget)


def wish_targets(ctx: EditContext, req: "WishTarget") -> List[Tuple[str, int, FrozenSet[str]]]:
    """넣을 원티드 — (간호사, 날, 원하는 근무). 지금 근무표(ctx.main) 기준으로 반영 안 된 것만."""
    who = set(req.nurses) if req.nurses else None
    out = []
    for (n, d), codes in sorted(ctx.wishes.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if (who is not None and n not in who) or not req.first_day <= d <= req.last_day:
            continue
        if ctx.is_free(n, d) and ctx.main[n][d] not in codes:
            out.append((n, d, frozenset(codes)))
    return out


def wish_count(ctx: EditContext, grid: Dict[str, List], targets) -> int:
    return sum(1 for n, d, codes in targets if grid[n][d] in codes)


# ── 근무 모양(ShapeTarget) ──────────────────────────────────────────────────


def shape_people(ctx: EditContext, req: "ShapeTarget") -> List[str]:
    """고칠 사람 — 짚은 사람(없으면 전원) 중 그 기간에 바꿀 수 있는 칸이 있는 사람."""
    who = req.nurses or tuple(ctx.nurse_ids)
    return [n for n in who if n in ctx.main and any(ctx.is_free(n, d) for d in range(req.first_day, req.last_day + 1))]


def _changeable(ctx: EditContext, req: "ShapeTarget", n: str, days) -> bool:
    return any(req.first_day <= d and ctx.is_free(n, d) for d in days)


def shape_instances(ctx: EditContext, req: "ShapeTarget") -> List[Tuple[Tuple[str, int, FrozenSet[str]], ...]]:
    """개수를 줄이는 모양(SHAPE_COUNTED)의 자리 — 칸 셋이 모두 맞으면 그 모양 하나. 가운데(순서는 첫 칸)가 기간 안이고
    바꿀 수 있는 칸이 하나라도 있는 것만. 휴가·공가가 끼면 엔진처럼 하루짜리 OFF 로 보지 않는다(rules._isolated_off)."""
    out = []
    for n in shape_people(ctx, req):
        row = ctx.main[n]
        for d in range(req.first_day, req.last_day + 1):
            if req.kind == "pattern":
                days = (d, d + 1, d + 2)
                if days[-1] >= ctx.days or any(row[x] is None for x in days):
                    continue
                for _name, a, c in _SEQUENCES:
                    if _changeable(ctx, req, n, days):
                        out.append(((n, d, a), (n, d + 1, _OFF), (n, d + 2, c)))
                continue
            days = (d - 1, d, d + 1)
            if days[0] < 0 or days[-1] >= ctx.days or any(row[x] is None for x in days):
                continue
            if not _changeable(ctx, req, n, days):
                continue
            if req.kind == "isolated_off":
                if not any((n, x) in ctx.vacation for x in days):
                    out.append(((n, d - 1, _WORK), (n, d, _OFF), (n, d + 1, _WORK)))
            elif req.kind == "isolated_work":
                out.append(((n, d - 1, _OFF), (n, d, frozenset({"D", "E", "M"})), (n, d + 1, _OFF)))
    return out


def shape_count(ctx: EditContext, grid: Dict[str, List], req: "ShapeTarget") -> int:
    """개수 종류 = 그 모양 개수 · work_run = 사람 중 가장 긴 연속 근무 · weekend = 사람 중 가장 많은 주말 근무."""
    if req.kind in SHAPE_COUNTED:
        return sum(1 for inst in shape_instances(ctx, req) if all(grid[n][d] in codes for n, d, codes in inst))
    values = shape_values(ctx, grid, req)
    return max(values.values()) if values else 0


def shape_values(ctx: EditContext, grid: Dict[str, List], req: "ShapeTarget") -> Dict[str, int]:
    """사람마다 — work_run 은 기간에 걸친 가장 긴 연속 근무(바꿀 수 있는 칸이 하나도 없는 연속은 뺀다 · 솔버도 묶지
    않는다), weekend 는 기간 안 주말 근무 수."""
    out = {}
    for n in shape_people(ctx, req):
        row = grid[n]
        if req.kind == "weekend":
            out[n] = sum(1 for d in weekend_days(ctx, req) if row[d] in _WORK)
            continue
        best = run = 0
        movable = False
        for d in range(ctx.days):
            if row[d] in _WORK:
                run += 1
                movable = movable or (d >= req.first_day and ctx.is_free(n, d))
            else:
                run, movable = 0, False
            if run and movable and d >= req.first_day and d - run + 1 <= req.last_day:
                best = max(best, run)
        out[n] = best
    return out


def weekend_floor(ctx: EditContext, req: "ShapeTarget", nurse_id: str) -> int:
    """바꿀 수 없는 주말 근무 수 — 이보다 낮은 상한은 지킬 수 없어 그 사람은 이 수까지로 본다."""
    return sum(1 for d in weekend_days(ctx, req) if not ctx.is_free(nurse_id, d) and ctx.main[nurse_id][d] in _WORK)


def weekend_days(ctx: EditContext, req: "ShapeTarget") -> List[int]:
    return [d for d in range(req.first_day, req.last_day + 1) if ctx.day_date(d).weekday() >= 5]


def balance_members(ctx: EditContext, codes, open_from: int) -> Tuple[str, ...]:
    """고르게 맞출 사람 — 바꿀 수 있는 날에 그 근무를 할 수 있는 사람. N 전담·고정 근무자는 뺀다(그 근무 수가
    원래 다르다). OFF 를 맞출 때는 바꿀 수 있는 날이 있는 사람."""
    work = set(codes) & WORK_CODES
    out = []
    for n in ctx.nurse_ids:
        if n in ctx.n_only or n in ctx.single_shift:
            continue
        days = [d for d in range(open_from, ctx.days) if ctx.is_free(n, d)]
        if not days:
            continue
        if work and not any(work - ctx.forbidden.get((n, d), set()) for d in days):
            continue
        out.append(n)
    return tuple(out)


def balance_counts(ctx: EditContext, grid: Dict[str, List], req: "BalanceTarget") -> Dict[str, int]:
    """대상마다 한 달 전체 그 근무 수(순수 OFF 기준 · `count_cells`)."""
    return {n: count_cells(ctx, grid, n, req.codes, range(ctx.days)) for n in req.members}


def balance_spread(ctx: EditContext, grid: Dict[str, List], req: "BalanceTarget") -> int:
    counts = balance_counts(ctx, grid, req).values()
    return max(counts) - min(counts) if counts else 0


def work_down(ctx: EditContext, req: CountTarget) -> bool:
    """요청자의 근무가 줄어드는 요청인가 — 근무 개수를 줄이거나 OFF 를 늘림. 아니면 근무가 느는 요청."""
    lower = req.target < count_in(ctx, ctx.main, req)
    return lower if set(req.codes) & WORK_CODES else not lower


def work_targets(req: CountTarget) -> FrozenSet[str]:
    """인원 규칙에 걸리는 근무 — 근무 개수 요청이면 그 근무, OFF 요청이면 모든 근무."""
    return frozenset(req.codes) & WORK_CODES or WORK_CODES


def transfer_label(ctx: EditContext, req: CountTarget, policy: str) -> str:
    """방식 이름 — 근무가 주는지(메우기)·느는지(넘겨받기)에 따라 말이 다르다."""
    down = work_down(ctx, req)
    off = not (set(req.codes) & WORK_CODES)
    if policy == "same_grade":
        return "같은 등급 간호사가 메우기" if down else "같은 등급 간호사의 근무를 넘겨받기"
    if policy == "same_team":
        return "같은 팀 간호사가 메우기" if down else "같은 팀 간호사의 근무를 넘겨받기"
    if policy == "fair":
        if off:
            return "OFF 가 많은 간호사가 메우기" if down else "OFF 가 적은 간호사가 쉬기"
        return "그 근무가 적은 간호사가 메우기" if down else "그 근무가 많은 간호사에게서 넘겨받기"
    if policy == "reduce_coverage":
        return "메우지 않고 그날 인원 줄이기"
    if policy == "add_coverage":
        return "넘겨받지 않고 그날 인원 늘리기"
    return "가장 적게 바꾸기"


def count_cells(ctx: EditContext, grid: Dict[str, List], nurse_id: str, codes, days) -> int:
    """그 사람의 그 근무 개수(그 날들) — OFF 는 휴가·공가 칸을 세지 않는다(순수 OFF · 화면·병동 규칙과 같게 ·
    평가 11-03: '장원영 OFF 11→12' 로 연차까지 셌다). 요청자·상대·미리 풀기 모두 이 함수로 센다(검토 M2).
    휴가 칸은 바꿀 수 없는 칸이라 목표 계산은 그대로 맞는다."""
    skip = "O" in codes
    return sum(1 for d in days if grid[nurse_id][d] in codes and not (skip and (nurse_id, d) in ctx.vacation))


def counts_as(ctx: EditContext, req: CountTarget, day: int) -> bool:
    """그 칸이 이 개수 요청에서 셀 수 있는 칸인가(`count_cells` 와 같은 기준)."""
    return not ("O" in req.codes and (req.nurse_id, day) in ctx.vacation)


def count_in(ctx: EditContext, grid: Dict[str, List], req: CountTarget) -> int:
    return count_cells(ctx, grid, req.nurse_id, req.codes, req.days())


def fixed_count(ctx: EditContext, req: CountTarget, open_from: int = 0) -> int:
    """기간 안에서 못 바꾸는 칸(지난 날·고정 칸)에 이미 있는 그 근무 수 — 목표의 하한."""
    row = ctx.main[req.nurse_id]
    return sum(1 for d in req.days() if row[d] in req.codes and counts_as(ctx, req, d)
               and (d < open_from or not ctx.is_free(req.nurse_id, d)))


def swap(ctx: EditContext, nurse_a: str, nurse_b: str, day: int) -> List[SetCell]:
    """두 사람의 그날 근무를 맞바꾼다."""
    a, b = ctx.main[nurse_a][day], ctx.main[nurse_b][day]
    return [SetCell(nurse_a, day, str(b)), SetCell(nurse_b, day, str(a))]


def off_days(nurse_id: str, days: Iterable[int]) -> List[SetCell]:
    """기간 OFF — 여러 날을 OFF 로."""
    return [SetCell(nurse_id, d, "O") for d in days]


def first_open_day(ctx: EditContext, today: date) -> int:
    """바꿀 수 있는 첫 날(0부터). 오늘 이전은 이미 근무한 날이라 닫는다. 지난 달이면 ctx.days."""
    return min(max((today - date(ctx.year, ctx.month, 1)).days, 0), ctx.days)


def request_problem(ctx: EditContext, req: EditRequest, open_from: int = 0) -> Optional[str]:
    """요청 자체가 성립하지 않는 이유(사람이 읽는 문장). 성립하면 None."""
    if isinstance(req, CoverTarget):
        return _cover_problem(ctx, req, open_from)
    if isinstance(req, BalanceTarget):
        return _balance_problem(ctx, req, open_from)
    if isinstance(req, WishTarget):
        if not 0 <= req.first_day <= req.last_day < ctx.days or req.first_day < open_from:
            return "이 달에 바꿀 수 있는 날이 남아 있지 않습니다."
        return None
    if isinstance(req, ShapeTarget):
        if not 0 <= req.first_day <= req.last_day < ctx.days or req.first_day < open_from:
            return "이 달에 바꿀 수 있는 날이 남아 있지 않습니다."
        if req.kind not in SHAPE_LABEL:
            return "어떤 근무 모양을 고칠지 알아듣지 못했습니다."
        missing = [n for n in req.nurses if n not in ctx.main]
        if missing:
            return f"{missing[0]} 는 이 근무표에 없습니다."
        if not shape_people(ctx, req):
            return "그 기간에는 바꿀 수 있는 칸이 없습니다."
        if req.kind == "work_run" and req.at_most is not None and req.at_most < 1:
            return "연속 근무는 1일 이상으로 말씀해 주세요."
        return None
    if req.nurse_id not in ctx.main:
        return f"{req.nurse_id} 는 이 근무표에 없습니다."
    if isinstance(req, CountTarget):
        return _count_problem(ctx, req, open_from)
    if not 0 <= req.day < ctx.days:
        return f"{req.day + 1}일은 이 달에 없습니다."
    if req.day < open_from:
        return f"{req.day + 1}일은 이미 지난 날짜라 바꿀 수 없습니다."
    name = ctx.names.get(req.nurse_id, req.nurse_id)
    reason = ctx.frozen.get((req.nurse_id, req.day))
    if reason and reason != FIXED_WANTED:     # 확정 원티드는 그 칸을 직접 짚었으니 푼다(`with_released`)
        return f"{name} {req.day + 1}일은 '{reason}' 칸이라 바꿀 수 없습니다."
    if isinstance(req, LeaveCell):
        if req.code not in ctx.leave_codes:
            return f"'{req.code}' 는 이 병동의 휴가 코드가 아닙니다."
        if ctx.main[req.nurse_id][req.day] in (None, "O"):
            return (f"{name} {req.day + 1}일은 이미 쉬는 날이라 {eul(ctx.leave_codes[req.code])} 넣지 않습니다. "
                    "근무일을 골라 주세요(남는 OFF 를 연차로 바꾸려면 '자동 연차전환'이라고 말씀해 주세요).")
        before = req.day - 1
        if (ctx.config.get("ban_night_before_fixed_off") and before >= 0 and ctx.main[req.nurse_id][before] == "N"
                and (before < open_from or not ctx.is_free(req.nurse_id, before))):
            return (f"{name} {before + 1}일 나이트를 바꿀 수 없어 {req.day + 1}일에 휴가를 넣을 수 없습니다"
                    "(휴가 전날 나이트 금지 설정).")
        return None
    codes = {req.code} if isinstance(req, SetCell) else set(req.codes)
    usable = set(EDIT_MAINS) if ctx.use_mid else set(EDIT_MAINS) - {"M"}
    if not codes <= usable:
        return f"고를 수 없는 근무입니다: {sorted(codes - usable)}"
    if isinstance(req, SetCell) and req.code in ctx.forbidden.get((req.nurse_id, req.day), set()):
        return f"{name} {req.day + 1}일 {req.code} 는 허용 근무가 아니거나 기피 근무입니다."
    if isinstance(req, AvoidCell) and not usable - codes:
        return "그 칸에 넣을 수 있는 근무가 남지 않습니다."
    return None


def _cover_problem(ctx: EditContext, req: CoverTarget, open_from: int) -> Optional[str]:
    if not 0 <= req.day < ctx.days:
        return f"{req.day + 1}일은 이 달에 없습니다."
    if req.day < open_from:
        return f"{req.day + 1}일은 이미 지난 날짜라 바꿀 수 없습니다."
    usable = WORK_CODES if ctx.use_mid else WORK_CODES - {"M"}
    if req.code not in usable:
        return "그날 인원은 근무(D·E·N" + ("·M" if ctx.use_mid else "") + ")로만 바꿀 수 있습니다."
    if req.target < 0:
        return "인원은 0명보다 줄일 수 없습니다."
    members = cover_members(ctx, req)
    able = [n for n in members if ctx.main[n][req.day] == req.code or (
        ctx.is_free(n, req.day) and req.code not in ctx.forbidden.get((n, req.day), set()))]
    who = f"{req.grade}등급까지인 " if req.grade is not None else ""
    if req.target > len(able):
        return f"{req.day + 1}일 {req.code} 를 할 수 있는 {who}사람이 {len(able)}명뿐입니다."
    # 줄이는 쪽 — 바꿀 수 없는 칸(확정 원티드·파견 등)으로 그 근무에 묶인 사람은 못 뺀다(검토 11-03)
    locked = sum(1 for n in members if ctx.main[n][req.day] == req.code and not ctx.is_free(n, req.day))
    if req.target < locked:
        return (f"{req.day + 1}일 {req.code} 는 바꿀 수 없는 칸(확정 원티드·파견 등)으로 {who}{locked}명이 묶여 있어 "
                f"{req.target}명으로 줄일 수 없습니다.")
    return None


def _balance_problem(ctx: EditContext, req: BalanceTarget, open_from: int) -> Optional[str]:
    usable = set(EDIT_MAINS) if ctx.use_mid else set(EDIT_MAINS) - {"M"}
    if not set(req.codes) <= usable:
        return f"고를 수 없는 근무입니다: {sorted(set(req.codes) - usable)}"
    if req.first_day < open_from or req.first_day >= ctx.days:
        return "이 달에 바꿀 수 있는 날이 남아 있지 않습니다."
    if len(req.members) < 2:
        return "고르게 맞출 사람이 두 명보다 적습니다(그 근무를 할 수 있는 사람 기준)."
    if req.target is not None:
        low = {n: sum(1 for d in range(ctx.days) if (d < open_from or not ctx.is_free(n, d))
                      and count_cells(ctx, ctx.main, n, req.codes, (d,))) for n in req.members}
        free = {n: sum(1 for d in range(open_from, ctx.days) if ctx.is_free(n, d)) for n in req.members}
        over = [ctx.names.get(n, n) for n in req.members if low[n] > req.target]
        under = [ctx.names.get(n, n) for n in req.members if low[n] + free[n] < req.target]
        if over or under:
            who = ", ".join((over + under)[:3]) + (" 등" if len(over + under) > 3 else "")
            return f"{who}은(는) 바꿀 수 없는 칸 때문에 그 수({req.target}회)로 맞출 수 없습니다."
    if req.spread is not None and req.spread < 0:
        return "차이는 0 이상이어야 합니다."
    return None


def _count_problem(ctx: EditContext, req: CountTarget, open_from: int) -> Optional[str]:
    name = ctx.names.get(req.nurse_id, req.nurse_id)
    if not 0 <= req.first_day <= req.last_day < ctx.days:
        return "기간이 이 달 안에 있지 않습니다."
    if req.first_day < open_from:
        return f"{req.first_day + 1}일은 이미 지난 날짜라 바꿀 수 없습니다."
    usable = set(EDIT_MAINS) if ctx.use_mid else set(EDIT_MAINS) - {"M"}
    if not set(req.codes) <= usable:
        return f"고를 수 없는 근무입니다: {sorted(set(req.codes) - usable)}"
    if req.transfer is not None and req.transfer not in TRANSFERS:
        return "메우는 방식을 알 수 없습니다."
    # 방향과 맞지 않는 인원 완화는 받지 않는다 — 사용자가 동의하지 않은 완화가 생긴다(검토 P1)
    if req.transfer == "add_coverage" and work_down(ctx, req):
        return "근무가 줄어드는 요청이라 '그날 인원 늘리기'는 고를 수 없습니다."
    if req.transfer == "reduce_coverage" and not work_down(ctx, req):
        return "근무가 늘어나는 요청이라 '그날 인원 줄이기'는 고를 수 없습니다."
    span = req.last_day - req.first_day + 1
    if not 0 <= req.target <= span:
        return f"{name}의 그 기간 근무는 0~{span}회 사이로만 정할 수 있습니다."
    floor = fixed_count(ctx, req, open_from)
    if req.target < floor:
        return f"{name}의 그 근무 중 {floor}회는 바꿀 수 없는 칸(고정·지난 날)이라 {floor}회 아래로는 줄일 수 없습니다."
    return None


def is_satisfied(ctx: EditContext, req: EditRequest, grid: Optional[Dict[str, List]] = None) -> bool:
    """grid 를 주면 그 격자에서(후보 적용 뒤), 없으면 지금 근무표에서."""
    if isinstance(req, CountTarget):
        return count_in(ctx, grid or ctx.main, req) == req.target
    if isinstance(req, CoverTarget):
        return cover_count(ctx, grid or ctx.main, req) == req.target
    if isinstance(req, WishTarget):
        targets = wish_targets(ctx, req)
        need = len(targets) if req.at_least is None else req.at_least
        return wish_count(ctx, grid or ctx.main, targets) >= need
    if isinstance(req, ShapeTarget):
        limit = 0 if req.at_most is None else req.at_most
        if req.kind == "weekend":
            return all(v <= max(limit, weekend_floor(ctx, req, n))
                       for n, v in shape_values(ctx, grid or ctx.main, req).items())
        return shape_count(ctx, grid or ctx.main, req) <= limit
    if isinstance(req, BalanceTarget):
        counts = balance_counts(ctx, grid or ctx.main, req).values()
        if req.target is not None:
            return all(c == req.target for c in counts)
        return max(counts) - min(counts) <= (1 if req.spread is None else req.spread)
    current = (grid or ctx.main)[req.nurse_id][req.day]
    if isinstance(req, LeaveCell):
        return current == "O"          # 대표코드는 O — 휴가 코드는 후보 칸(after)에 실린다
    if isinstance(req, SetCell):
        return current == req.code
    return current not in req.codes
