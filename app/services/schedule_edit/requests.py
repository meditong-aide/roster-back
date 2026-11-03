"""수정 요청의 기본 단위 — 칸 지정·칸 금지·개수. 교환·기간 OFF 는 칸 지정으로 풀어 만든다.

자연어 해석(이름→nurse_id, '나이트'→N, 날짜)은 스킬 층이 한다. 여기는 해석이 끝난 값만 받는다.
day 는 0부터(1일 = 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, FrozenSet, Iterable, List, Optional, Union

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


EditRequest = Union[SetCell, AvoidCell, CountTarget, LeaveCell, CoverTarget]


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
    current = (grid or ctx.main)[req.nurse_id][req.day]
    if isinstance(req, LeaveCell):
        return current == "O"          # 대표코드는 O — 휴가 코드는 후보 칸(after)에 실린다
    if isinstance(req, SetCell):
        return current == req.code
    return current not in req.codes
