"""수정 요청의 기본 단위 — 칸 지정·칸 금지·개수. 교환·기간 OFF 는 칸 지정으로 풀어 만든다.
여러 날 인원 계획(StaffingPlan)·근무조 등급 하한(GradeFloor)은 32장(나사렛 7B 11월 재요청).

자연어 해석(이름→nurse_id, '나이트'→N, 날짜)은 스킬 층이 한다. 여기는 해석이 끝난 값만 받는다.
day 는 0부터(1일 = 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple, Union

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


#: 숫자를 읽었을 때 받침이 있나(영·일·삼·육·칠·팔) — 'VER1을' · 'VER2를'
_DIGIT_FINAL = {"0": True, "1": True, "2": False, "3": True, "4": False, "5": False, "6": True, "7": True, "8": True,
                "9": False}


def _final(word: str):
    """끝 글자에 받침이 있나 — 괄호로 끝나면 괄호 안 끝 글자('VER2(… 수정본)'). 모르면 None."""
    word = (word or "").rstrip()
    if word.endswith(")"):
        word = word[:-1]
    last = word[-1:]
    if "가" <= last <= "힣":
        return bool((ord(last) - 0xAC00) % 28)
    return _DIGIT_FINAL.get(last)


def _particle(word: str, with_final: str, without: str) -> str:
    final = _final(word)
    return word + (f"{with_final}({without})" if final is None else with_final if final else without)


def eul(word: str) -> str:
    """목적격 조사 — 받침이 있으면 '을', 없으면 '를'('연차를' · '공가를' · '생휴를' · 'VER1을' · 'VER2를')."""
    return _particle(word, "을", "를")


def eun(word: str) -> str:
    """보조사 — '은'/'는'('VER1은' · 'VER2는')."""
    return _particle(word, "은", "는")


def gwa(word: str) -> str:
    """접속 조사 — '과'/'와'('VER1과' · 'VER2와')."""
    return _particle(word, "과", "와")


def ro(word: str) -> str:
    """부사격 조사 — 받침이 없거나 ㄹ 받침이면 '로', 그 밖의 받침이면 '으로'('0회로' · '6→8로' · '2→3으로')."""
    bare = (word or "").rstrip().rstrip(")")
    last = bare[-1:]
    if last in ("1", "7", "8") or ("가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 == 8):
        return word + "로"
    return _particle(word, "으로", "로")


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
    asked_first·asked_target: 사용자가 정한 날로 기간 앞을 줄였을 때 원래 첫날·목표(None = 줄인 적 없음) — 정한 날을
    다시 당기면 여기서 되살린다(`skills._from_open` · 검토 11-05).
    total: '총 14개'처럼 그 달 전체 개수로 말했다 — 목표는 기간 개수로 바꿔 두고, 요청 줄은 한 달 개수로 보인다(사용자 결정 11-05).
    """

    nurse_id: str
    codes: FrozenSet[str]
    target: int
    first_day: int
    last_day: int
    transfer: Optional[str] = None
    asked_first: Optional[int] = None
    asked_target: Optional[int] = None
    total: bool = False

    @property
    def day(self) -> int:
        """창·고치지 않는 날 판정의 기준 — 기간 첫날."""
        return self.first_day

    def days(self) -> range:
        return range(self.first_day, self.last_day + 1)


@dataclass(frozen=True)
class BalanceTarget:
    """여러 사람의 이 근무 수를 고르게 — 예) 'N 근무 수 고르게 맞춰줘'(사용자 결정 11-03).

    한 달 전체 개수로 맞춘다(고치지 않는 날·바꿀 수 없는 칸에 이미 있는 근무도 센다). 대상(members)은 그 근무를 할 수
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
        """창·고치지 않는 날 판정의 기준 — 바꿀 수 있는 첫날."""
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
    #: 말한 기간 앞을 사용자가 정한 날로 줄였을 때 원래 첫날(None = 줄인 적 없음 · `CountTarget.asked_first` 와 같다)
    asked_first: Optional[int] = None
    #: 넣을 원티드의 근무(None = 전부) — '신청 오프 유지' 는 OFF 원티드만({"O"} · 32장)
    codes: Optional[FrozenSet[str]] = None
    #: 수간호사가 포기한 원티드 칸 — 넣지 않는다(다 못 넣을 때 고른 '이 원티드를 포기하면 가능' · 사용자 결정 11-05)
    skip: Tuple[Tuple[str, int], ...] = ()

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
    바꿀 수 있는 칸이 하나도 없는 모양(고치지 않는 날·묶인 칸뿐)은 세지 않는다 — 고칠 수 없다. 주말은 바꿀 수 없는 주말 근무가
    이미 상한을 넘는 사람이면 그 수까지로 본다(`weekend_floor` · 솔버와 판정이 같은 기준).
    """

    kind: str
    nurses: Tuple[str, ...]
    first_day: int
    last_day: int
    at_most: Optional[int] = None
    ranged: bool = False
    said: bool = False
    #: 말한 기간 앞을 사용자가 정한 날로 줄였을 때 원래 첫날(None = 줄인 적 없음 · `CountTarget.asked_first` 와 같다)
    asked_first: Optional[int] = None

    @property
    def nurse_id(self) -> None:
        """사람 하나를 짚지 않는다 — 칸 요청을 거르는 자리를 그대로 지나간다."""
        return None

    @property
    def day(self) -> int:
        return self.first_day


#: 인원 계획의 날 묶음 — 매일·평일·토일·특정 날
STAFF_SCOPES = ("all", "weekday", "weekend", "days")
SCOPE_LABEL = {"all": "매일", "weekday": "평일", "weekend": "토일"}


@dataclass(frozen=True)
class StaffRule:
    """인원 계획 한 줄 — 그 날 묶음의 그 근무 인원. 예) 평일 D 3~4명.

    low·high: 최소·최대. 숫자 하나만 말했으면('3.3.3'·'미드 1') high=None·exact=None — 정확히인지 이상인지 아직 정하지 않은 것.
    지금 판에 그보다 많은 날이 있으면 스킬이 묻고(`plan_ambiguous`), 없으면 정확히로 본다('3.3.3' 은 정확히 3 · 32장).
    exact=True 면 정확히 low, False 면 low 이상(high 가 없으면 최대는 병동 설정 그대로).
    """

    scope: str
    code: str
    low: int
    high: Optional[int] = None
    days: Tuple[int, ...] = ()
    exact: Optional[bool] = None

    def bounds(self) -> Tuple[int, Optional[int]]:
        if self.exact is False:
            return self.low, self.high
        if self.high is not None and self.exact is None:
            return self.low, self.high          # 범위를 말했다
        return self.low, self.low

    def covers(self, ctx: EditContext, day: int) -> bool:
        if self.scope == "days":
            return day in self.days
        if self.scope == "all":
            return True
        return (ctx.day_date(day).weekday() >= 5) == (self.scope == "weekend")


@dataclass(frozen=True)
class StaffingPlan:
    """여러 날 근무별 인원 — 예) '그외 3.3.3 안될시 주말 2.2.3 미드 1'(사용자 결정 11-05 · 32장).

    alternatives: 차례대로 시도할 안 — 안 하나는 인원 계획 줄 묶음(같은 날·근무는 뒤 줄이 앞 줄을 덮는다).
    고칠 수 있는 날(first_day~last_day)마다 그날 인원에 세는 사람으로 센다(`rules.counted_people`). 고정 근무자를 셀지는
    count_fixed(None = 아직 정하지 않음 — 그 근무에 고정 근무자가 있으면 스킬이 묻는다).
    chosen: 쓰기로 정한 안(0부터 · None = 아직 — 스킬이 차례대로 시도해 정한다). short: 남겨도 되는 모자란 칸 수
    (None = 0 · 수간호사가 동의한 만큼만 · 사용자 지시 11-05).
    """

    alternatives: Tuple[Tuple[StaffRule, ...], ...]
    first_day: int
    last_day: int
    chosen: Optional[int] = None
    short: Optional[int] = None
    count_fixed: Optional[bool] = None

    @property
    def nurse_id(self) -> None:
        return None

    @property
    def day(self) -> int:
        return self.first_day

    def rules(self, alt: Optional[int] = None) -> Tuple[StaffRule, ...]:
        index = alt if alt is not None else (self.chosen or 0)
        return self.alternatives[index] if 0 <= index < len(self.alternatives) else ()


@dataclass(frozen=True)
class GradeFloor:
    """근무조마다 등급 하한 — 예) '매일 모든 근무조에 1등급 1명 이상'(32장).

    고칠 수 있는 날마다 codes(기본 D·E·N) + m_days 의 M 근무조에 등급 ≤ grade 인 사람 count 명 이상. m_days 는 인원 계획에
    M 이 있는 날(스킬이 고른 안으로 채운다). 등급은 엔진이 읽는 간호사 등급(`ctx.grade_of`). count_fixed 는 인원 계획과 같게
    (고정 근무자를 근무조에 셀지). short: 남겨도 되는 부족 근무조 수(None = 0 · 동의한 만큼만).
    """

    grade: int
    count: int
    first_day: int
    last_day: int
    codes: Tuple[str, ...] = ("D", "E", "N")
    m_days: Tuple[int, ...] = ()
    short: Optional[int] = None
    count_fixed: Optional[bool] = None

    @property
    def nurse_id(self) -> None:
        return None

    @property
    def day(self) -> int:
        return self.first_day


@dataclass(frozen=True)
class GiveUpWish:
    """지금 반영된 원티드를 포기한 칸 — 그 칸을 직접 짚은 것으로 본다(원티드 보호는 짚을 때만 푼다 [RD-29] · 계획이 막혀
    '이 원티드를 포기하면 가능' 카드를 고른 것이 짚는 것 · 사용자 결정 11-05). 칸을 무엇으로 바꿀지는 정하지 않는다."""

    cells: Tuple[Tuple[str, int], ...]

    @property
    def nurse_id(self) -> None:
        return None

    @property
    def day(self) -> int:
        return min((d for _n, d in self.cells), default=0)


EditRequest = Union[SetCell, AvoidCell, CountTarget, LeaveCell, CoverTarget, BalanceTarget, WishTarget, ShapeTarget,
                    StaffingPlan, GradeFloor, GiveUpWish]
#: 칸 하나를 짚지 않는 요청 — 원티드 보호 면제·칸 요청 비교에서 빠진다(GiveUpWish 의 칸은 `model.requested_cells` 가 따로 센다)
AGGREGATE = (CountTarget, CoverTarget, BalanceTarget, WishTarget, ShapeTarget, StaffingPlan, GradeFloor, GiveUpWish)
#: 여러 날 인원·등급 계획 — 칸 상한 없이 두 단계로 푼다(`solver._plan_search`)
PLAN = (StaffingPlan, GradeFloor)


# ── 인원 계획·등급 하한(StaffingPlan · GradeFloor) ─────────────────────────────


def fixed_workers(ctx: EditContext) -> List[str]:
    """고정 근무자(엔진이 따로 채우는 사람 — 한 달 칸이 '고정근무자'로 묶였다)."""
    return sorted({n for (n, _d), why in ctx.frozen.items() if why == "고정근무자"})


def plan_people(ctx: EditContext, day: int, count_fixed: Optional[bool]) -> List[str]:
    """그날 인원·근무조에 세는 사람 — 화면과 같은 셈(`rules.counted_people`), 고정 근무자를 빼기로 했으면 뺀다."""
    from services.schedule_edit.rules import counted_people
    people = counted_people(ctx, day)
    if count_fixed is False:
        fixed = set(fixed_workers(ctx))
        people = [n for n in people if n not in fixed]
    return people


def staffing_bounds(ctx: EditContext, plan: StaffingPlan,
                    alt: Optional[int] = None) -> Dict[int, Dict[str, Tuple[int, Optional[int]]]]:
    """날 → 근무 → (최소, 최대). 같은 날·근무는 뒤 줄이 덮는다."""
    out: Dict[int, Dict[str, Tuple[int, Optional[int]]]] = {}
    for rule in plan.rules(alt):
        for d in range(plan.first_day, plan.last_day + 1):
            if rule.covers(ctx, d):
                out.setdefault(d, {})[rule.code] = rule.bounds()
    return out


def staff_count(ctx: EditContext, grid: Dict[str, List], day: int, code: str, count_fixed: Optional[bool]) -> int:
    return sum(1 for n in plan_people(ctx, day, count_fixed) if grid[n][day] == code)


def staffing_gaps(ctx: EditContext, grid: Dict[str, List], plan: StaffingPlan,
                  alt: Optional[int] = None) -> List[Tuple[int, str, int, int]]:
    """모자라는 칸 — (날, 근무, 최소, 지금 수). 최대를 넘는 것은 `staffing_over`."""
    out = []
    for d, by in sorted(staffing_bounds(ctx, plan, alt).items()):
        for code, (low, _high) in sorted(by.items()):
            have = staff_count(ctx, grid, d, code, plan.count_fixed)
            if have < low:
                out.append((d, code, low, have))
    return out


def staffing_over(ctx: EditContext, grid: Dict[str, List], plan: StaffingPlan,
                  alt: Optional[int] = None) -> List[Tuple[int, str, int, int]]:
    """최대를 넘는 칸 — (날, 근무, 최대, 지금 수). 바꿀 수 없는 칸만으로 넘는 것은 뺀다(모델도 그만큼은 봐준다)."""
    out = []
    for d, by in sorted(staffing_bounds(ctx, plan, alt).items()):
        for code, (_low, high) in sorted(by.items()):
            if high is None:
                continue
            people = plan_people(ctx, d, plan.count_fixed)
            have = sum(1 for n in people if grid[n][d] == code)
            fixed = sum(1 for n in people if not ctx.is_free(n, d) and ctx.main[n][d] == code)
            if have > max(high, fixed):
                out.append((d, code, high, have))
    return out


def plan_short(ctx: EditContext, grid: Dict[str, List], plan: StaffingPlan, alt: Optional[int] = None) -> int:
    return sum(low - have for _d, _c, low, have in staffing_gaps(ctx, grid, plan, alt))


def plan_label(rules: Sequence[StaffRule]) -> str:
    """예) '매일 D3·E3·N3' · '평일 D3~4·E3·N3 · 토일 D2·E2·N3·M1'."""
    groups: Dict[Tuple, List[str]] = {}
    # 같은 날 묶음·근무를 다시 말한 줄은 앞 줄을 덮는다 — 이름에도 마지막 줄만(덧붙인 '평일 D 3~4명')
    shown = [r for i, r in enumerate(rules)
             if not any((x.scope, x.days, x.code) == (r.scope, r.days, r.code) for x in rules[i + 1:])]
    for r in shown:
        where = SCOPE_LABEL.get(r.scope) or "·".join(str(d + 1) for d in r.days) + "일"
        low, high = r.bounds()
        if r.exact is False and r.high is None:
            amount = f"{low}이상"
        else:
            amount = f"{low}" if high == low else f"{low}~{high}"
        groups.setdefault((r.scope, r.days, where), []).append(f"{r.code}{amount}")
    return " · ".join(f"{where} {'·'.join(parts)}" for (_s, _d, where), parts in groups.items())


def plan_capacity(ctx: EditContext, plan: StaffingPlan, alt: Optional[int] = None,
                  requests: Sequence["EditRequest"] = ()) -> Tuple[int, int]:
    """산술 점검 — (필요 칸, 근무 가능 칸). 필요 = 날마다 근무별 최소의 합. 근무 가능 = 그 칸을 채울 수 있는 근무 칸의 합 —
    바꿀 수 없는 칸은 계획한 근무일 때만, 바꿀 수 있는 칸은 그 사람이 지켜야 할 OFF 수(OFF 수·개인 한도·OFF 개수 요청 —
    지금보다 나빠지지 않게 보는 기준과 같다)를 뺀 만큼. 필요가 더 크면 어떤 방법으로도 다 못 채운다."""
    bounds = staffing_bounds(ctx, plan, alt)
    need = sum(low for by in bounds.values() for low, _h in by.values())
    capacity = 0
    for n in ctx.nurse_ids:
        counted = [d for d in bounds if n in plan_people(ctx, d, plan.count_fixed)]
        free = [d for d in counted if ctx.is_free(n, d)]
        capacity += sum(1 for d in counted if not ctx.is_free(n, d) and ctx.main[n][d] in bounds[d])
        if free:
            work = max(0, len(free) - _required_free_off(ctx, n, set(free), requests))
            capacity += min(work, _night_only_room(ctx, n, set(free)))
    return need, capacity


def _night_only_room(ctx: EditContext, nurse_id: str, free: Set[int]) -> int:
    """N 만 하는 사람(N 전담·허용 근무 N 하나)이 바꿀 수 있는 칸에 더 넣을 수 있는 N — 월 나이트 상한·개인 한도(지금보다
    나빠지지 않게)에서 바꿀 수 없는 칸의 N 을 뺀 만큼. 그 밖의 사람은 제한 없음(큰 수)."""
    if nurse_id not in ctx.n_only and ctx.single_shift.get(nurse_id) != "N":
        return len(free)
    row = ctx.main[nurse_id]
    now = sum(1 for m in row if m == "N")
    limits = ctx.monthly_limits.get(nurse_id) or {}
    caps = [int(limits[k]) for k in ("n_exact", "n_max") if limits.get(k) is not None]
    cap = max(min(caps + [int(ctx.config.get("max_nig_per_month") or 15)]), now)
    fixed = sum(1 for d, m in enumerate(row) if m == "N" and d not in free)
    return max(0, cap - fixed)


def _required_free_off(ctx: EditContext, nurse_id: str, free: Set[int], requests: Sequence["EditRequest"]) -> int:
    """그 사람이 바꿀 수 있는 칸(free) 안에 둬야 할 순수 OFF 수 — 규칙은 '지금보다 나빠지지 않게'라 지금 수까지만 센다."""
    row = ctx.main[nurse_id]
    now = sum(1 for d, m in enumerate(row) if m == "O" and (nurse_id, d) not in ctx.vacation)
    fixed = sum(1 for d, m in enumerate(row) if m == "O" and (nurse_id, d) not in ctx.vacation and d not in free)
    low = min(int((ctx.off_bounds.get(nurse_id) or (0, 0))[0] or 0), now)
    limits = ctx.monthly_limits.get(nurse_id) or {}
    for key in ("o_exact", "o_min"):
        if limits.get(key) is not None:
            low = max(low, min(int(limits[key]), now))
    need = low - fixed
    for r in requests:
        if isinstance(r, CountTarget) and r.nurse_id == nurse_id and set(r.codes) == {"O"}:
            # 개수 요청의 기간 안 OFF 는 목표 그대로 — 그 기간의 바꿀 수 없는 칸에 이미 있는 OFF 를 뺀 만큼은 바꿀 칸에 둔다
            fixed_in = count_cells(ctx, ctx.main, nurse_id, r.codes, [d for d in r.days() if d not in free])
            need = max(need, r.target - fixed_in)
    return max(0, need)


def plan_ambiguous(ctx: EditContext, plan: StaffingPlan, alt: Optional[int] = None) -> List[Tuple[int, StaffRule, List[int]]]:
    """숫자 하나만 말한 줄 중 지금 판에 그보다 많은 날이 있는 것 — (줄 번호, 줄, 그날들). '정확히'인지 '이상'인지에 따라
    결과가 달라지는 경우라 묻는다(사용자 지시 11-05). 그 날이 다른 줄로 덮였으면 그 줄은 묻지 않는다."""
    rules = plan.rules(alt)
    out = []
    for i, rule in enumerate(rules):
        if rule.exact is not None or rule.high is not None:
            continue
        days = [d for d in range(plan.first_day, plan.last_day + 1) if rule.covers(ctx, d)
                and not any(later.code == rule.code and later.covers(ctx, d) for later in rules[i + 1:])
                and staff_count(ctx, ctx.main, d, rule.code, plan.count_fixed) > rule.low]
        if days:
            out.append((i, rule, days))
    return out


def fixed_in_plan(ctx: EditContext, plan: StaffingPlan) -> List[Tuple[str, str]]:
    """계획한 근무를 하는 고정 근무자 — (간호사, 근무). 셀지 묻는 근거(사용자 지시 11-05: 기본값으로 고르지 않는다)."""
    codes = {r.code for alt in plan.alternatives for r in alt}
    out = []
    for n in fixed_workers(ctx):
        mains = {ctx.main[n][d] for d in range(plan.first_day, plan.last_day + 1)} & codes
        out += [(n, c) for c in sorted(mains)]
    return out


def plan_sig(requests: Sequence["EditRequest"]) -> str:
    """동의(모자라는 칸·등급 부족·원티드 포기·품질 넘김)가 걸리는 계획의 구분값 — 인원 계획·등급 하한이 같으면 같다(검토 H2:
    동의가 대화 전체의 양으로 쌓여 다음 계획에 그대로 쓰였다). 스킬이 채우는 값(고른 안·동의한 양·M 근무조·정확히/이상 답)은
    빼고 센다 — 답하는 동안 동의가 풀리지 않게."""
    import hashlib
    parts = []
    for r in requests:
        if isinstance(r, StaffingPlan):
            parts.append(("plan", tuple(tuple((x.scope, x.code, x.low, x.high, x.days) for x in alt)
                                        for alt in r.alternatives), r.first_day, r.last_day, r.count_fixed))
        elif isinstance(r, GradeFloor):
            parts.append(("grade", r.grade, r.count, r.first_day, r.last_day, tuple(r.codes), r.count_fixed))
    return hashlib.sha1(repr(parts).encode("utf-8")).hexdigest()[:10]


def request_label(ctx: EditContext, req: "EditRequest") -> str:
    """요청 하나를 사람 말로 짧게 — 적용 직전 재확인의 '요청 미충족' 문장(검토 M1: dataclass 모양이 그대로 나갔다)."""
    name = ctx.names.get(getattr(req, "nurse_id", None) or "", getattr(req, "nurse_id", None) or "")
    word = {"O": "OFF"}
    if isinstance(req, CountTarget):
        codes = "·".join(word.get(c, c) for c in sorted(req.codes))
        return f"{name} {codes} {req.target}회({req.first_day + 1}~{req.last_day + 1}일)"
    if isinstance(req, CoverTarget):
        return f"{req.day + 1}일 {req.code} {req.target}명"
    if isinstance(req, BalanceTarget):
        return "·".join(word.get(c, c) for c in sorted(req.codes)) + " 근무 수 고르게"
    if isinstance(req, WishTarget):
        return "원티드 반영"
    if isinstance(req, ShapeTarget):
        return SHAPE_LABEL.get(req.kind, req.kind)
    if isinstance(req, StaffingPlan):
        return f"하루 인원 {plan_label(req.rules())}"
    if isinstance(req, GradeFloor):
        return f"근무조마다 {req.grade}등급까지 {req.count}명"
    if isinstance(req, GiveUpWish):
        return "원티드 포기"
    if isinstance(req, LeaveCell):
        return f"{name} {req.day + 1}일 {ctx.leave_codes.get(req.code, req.code)}"
    if isinstance(req, SetCell):
        return f"{name} {req.day + 1}일 {word.get(req.code, req.code)}"
    return f"{name} {req.day + 1}일 " + "·".join(word.get(c, c) for c in sorted(req.codes)) + " 빼기"


def grade_slots(ctx: EditContext, req: GradeFloor) -> List[Tuple[int, str, Tuple[str, ...]]]:
    """(날, 근무, 그 등급까지인 사람) — 등급 하한을 거는 근무조."""
    out = []
    for d in range(req.first_day, req.last_day + 1):
        codes = tuple(req.codes) + (("M",) if d in req.m_days and "M" not in req.codes else ())
        people = [n for n in plan_people(ctx, d, req.count_fixed)
                  if ctx.grade_of.get(n) is not None and int(ctx.grade_of[n]) <= req.grade]
        out += [(d, c, tuple(people)) for c in codes]
    return out


def grade_gaps(ctx: EditContext, grid: Dict[str, List], req: GradeFloor) -> List[Tuple[int, str, int]]:
    """등급 하한이 모자라는 근무조 — (날, 근무, 모자라는 수)."""
    out = []
    for d, code, people in grade_slots(ctx, req):
        have = sum(1 for n in people if grid[n][d] == code)
        if have < req.count:
            out.append((d, code, req.count - have))
    return out


def wish_targets(ctx: EditContext, req: "WishTarget") -> List[Tuple[str, int, FrozenSet[str]]]:
    """넣을 원티드 — (간호사, 날, 원하는 근무). 지금 근무표(ctx.main) 기준으로 반영 안 된 것만."""
    who = set(req.nurses) if req.nurses else None
    out = []
    for (n, d), codes in sorted(ctx.wishes.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if (who is not None and n not in who) or not req.first_day <= d <= req.last_day:
            continue
        wanted = frozenset(codes) if req.codes is None else frozenset(codes) & req.codes
        if (n, d) in req.skip:
            continue
        if wanted and (ctx.is_free(n, d) or (n, d) in ctx.wish_fixed) and ctx.main[n][d] not in codes:
            out.append((n, d, wanted))
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
    """기간 안에서 못 바꾸는 칸(고치지 않는 날·고정 칸)에 이미 있는 그 근무 수 — 목표의 하한."""
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
    """바꿀 수 있는 첫 날(0부터). 지난 날과 오늘은 닫는다 — 오늘은 이미 시작된 근무일이다(사용자 결정 11-05).
    사용자가 고칠 첫날을 정했으면(ctx.edit_from) 그 앞을 모두 닫는다. 오늘은 짚은 날이 바로 오늘일 때만 열린다 — 짚은 날이
    오늘보다 앞이면(어제 '오늘부터'로 정한 대화를 오늘 이어 써도) 오늘도 닫는다. 지난 날은 어떤 경우에도 닫힌다.
    다음 달 근무표면 아무것도 닫지 않고(정한 날 앞만), 지난 달이면 ctx.days."""
    now = today_index(ctx, today)
    start = ctx.edit_from if ctx.edit_from is not None and ctx.edit_from >= now else now + 1
    return min(max(start, 0), ctx.days)


def today_index(ctx: EditContext, today: date) -> int:
    """오늘의 자리(0부터) — 다음 달 근무표면 음수, 지난 달이면 ctx.days 이상."""
    return (today - date(ctx.year, ctx.month, 1)).days


def days_text(first: int, last: int) -> str:
    """날 묶음(0부터) → '3일' · '3·4일' · '1~19일'."""
    if first == last:
        return f"{first + 1}일"
    return f"{first + 1}·{last + 1}일" if last == first + 1 else f"{first + 1}~{last + 1}일"


def _now(ctx: EditContext, open_from: int, today: Optional[date]) -> int:
    """닫힌 날 문장의 '오늘' — today 가 없으면 기준일·open_from 으로 가린다. 정한 날이 닫는 선이면 그 앞을 사용자 몫으로
    보고(-1 · 정한 날이 바로 오늘인지는 모른다), 아니면 open_from 앞날이 오늘(달 마지막 날이 오늘인지는 알 수 없어 지난 날로)."""
    if today is not None:
        return today_index(ctx, today)
    if ctx.edit_from is not None and ctx.edit_from == open_from:
        return -1
    return open_from - 1 if open_from < ctx.days else ctx.days


def held_until(ctx: EditContext, open_from: int, today: Optional[date] = None) -> Optional[int]:
    """사용자가 정한 날 때문에 닫힌 날이 있으면(정한 날이 오늘보다 뒤) 그대로 둘 마지막 날(1부터) — 아니면 None.
    정한 날이 오늘이거나 그 앞이면 닫힌 날은 지난 날·오늘뿐이라 '그대로 두기로 한 날'로 말하지 않는다."""
    if ctx.edit_from is None or ctx.edit_from <= 0 or ctx.edit_from != open_from:
        return None
    return ctx.edit_from if ctx.edit_from > _now(ctx, open_from, today) else None


def closed_day_text(ctx: EditContext, day: int, open_from: int, today: Optional[date] = None) -> str:
    """닫힌 날(day < open_from)을 짚었을 때 — 왜 못 바꾸는지 사실대로(지난 날·오늘·사용자가 그대로 두기로 한 날).
    닫힌 날을 말하는 자리는 모두 이 문장을 쓴다(기간은 `closed_span_text`)."""
    reopen = f" {open_from + 1}일부터 고칠 수 있습니다." if open_from < ctx.days else ""
    if day < _now(ctx, open_from, today):
        return f"{day + 1}일은 이미 지난 날짜라 바꿀 수 없습니다.{reopen}"
    held = held_until(ctx, open_from, today)
    if held:
        return f"{held}일까지는 그대로 두기로 해서 {day + 1}일은 바꾸지 않습니다.{reopen}"
    return (f"오늘({day + 1}일)은 이미 시작된 근무일이라 바꾸지 않습니다. "
            "오늘도 고치려면 '오늘부터 고쳐줘'라고 하시면 됩니다.")


def closed_span_text(ctx: EditContext, last: int, open_from: int, today: Optional[date] = None) -> str:
    """기간(마지막 날 last)이 모두 닫힌 날일 때(원티드·근무 모양·개수·휴가 기간 등) — `closed_day_text` 와 같은 기준."""
    if open_from >= ctx.days:
        return "이 달에 바꿀 수 있는 날이 남아 있지 않습니다."
    reopen = f" {open_from + 1}일부터 고칠 수 있습니다."
    if last < _now(ctx, open_from, today):
        return f"그 기간은 이미 지난 날이라 바꿀 수 없습니다.{reopen}"
    held = held_until(ctx, open_from, today)
    if held:
        return f"{held}일까지는 그대로 두기로 해서 그 기간은 바꾸지 않습니다.{reopen}"
    return f"그 기간은 지난 날·오늘이라 바꾸지 않습니다.{reopen} 오늘도 고치려면 '오늘부터 고쳐줘'라고 하시면 됩니다."


def edit_from_note(ctx: EditContext, open_from: int, today: Optional[date] = None) -> str:
    """사용자가 정한 날 안내 — 정한 뒤 처음 보이는 답에 한 번(참고 문구). 정하지 않았으면 빈 문자열.
    짚은 날이 오늘보다 앞이면 그날들을 지난 날로 말하고 오늘을 여는 말을 알려 준다(오늘은 짚은 날이 오늘일 때만 열린다)."""
    if ctx.edit_from is None:
        return ""
    if open_from >= ctx.days:
        return "이 달에는 고칠 수 있는 날이 남아 있지 않습니다."
    if held_until(ctx, open_from, today):
        return f"{open_from + 1}일부터 고칩니다({open_from}일까지는 그대로 둡니다)."
    now = _now(ctx, open_from, today)
    if ctx.edit_from == now:
        return f"오늘({now + 1}일)부터 고칩니다."
    if ctx.edit_from > now:                     # 다음 달 근무표를 1일부터
        return f"{open_from + 1}일부터 고칩니다."
    return (f"{days_text(ctx.edit_from, now - 1)}은 지난 날이라 바꿀 수 없어 {open_from + 1}일부터 고칩니다. "
            "오늘도 고치려면 '오늘부터 고쳐줘'라고 하시면 됩니다.")


def _closed_range(ctx: EditContext, first: int, last: int, open_from: int, today: Optional[date]) -> str:
    """기간 요청의 첫날이 닫혀 있을 때 — 기간이 모두 닫혔으면 기간 문장, 아니면 첫날 문장."""
    return (closed_span_text(ctx, last, open_from, today) if last < open_from
            else closed_day_text(ctx, first, open_from, today))


def request_problem(ctx: EditContext, req: EditRequest, open_from: int = 0, *,
                    today: Optional[date] = None) -> Optional[str]:
    """요청 자체가 성립하지 않는 이유(사람이 읽는 문장). 성립하면 None. today 는 닫힌 날 문장에만 쓴다(`closed_day_text`)."""
    if isinstance(req, CoverTarget):
        return _cover_problem(ctx, req, open_from, today)
    if isinstance(req, BalanceTarget):
        return _balance_problem(ctx, req, open_from, today)
    if isinstance(req, PLAN):
        return _plan_problem(ctx, req, open_from, today)
    if isinstance(req, GiveUpWish):
        missing = [n for n, _d in req.cells if n not in ctx.main]
        return f"{missing[0]} 는 이 근무표에 없습니다." if missing else None
    if isinstance(req, WishTarget):
        if not 0 <= req.first_day <= req.last_day < ctx.days:
            return "이 달에 바꿀 수 있는 날이 남아 있지 않습니다."
        if req.first_day < open_from:
            return _closed_range(ctx, req.first_day, req.last_day, open_from, today)
        return None
    if isinstance(req, ShapeTarget):
        if not 0 <= req.first_day <= req.last_day < ctx.days:
            return "이 달에 바꿀 수 있는 날이 남아 있지 않습니다."
        if req.first_day < open_from:
            return _closed_range(ctx, req.first_day, req.last_day, open_from, today)
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
        return _count_problem(ctx, req, open_from, today)
    if not 0 <= req.day < ctx.days:
        return f"{req.day + 1}일은 이 달에 없습니다."
    if req.day < open_from:
        return closed_day_text(ctx, req.day, open_from, today)
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


def _plan_problem(ctx: EditContext, req, open_from: int, today: Optional[date]) -> Optional[str]:
    if not 0 <= req.first_day <= req.last_day < ctx.days:
        return "이 달에 바꿀 수 있는 날이 남아 있지 않습니다."
    if req.first_day < open_from:
        return _closed_range(ctx, req.first_day, req.last_day, open_from, today)
    usable = WORK_CODES if ctx.use_mid else WORK_CODES - {"M"}
    if isinstance(req, GradeFloor):
        if req.grade < 1 or req.count < 1:
            return "등급과 인원은 1 이상으로 말씀해 주세요. 예) '매일 모든 근무조에 1등급 1명 이상'"
        if not any(g is not None and int(g) <= req.grade for n, g in ctx.grade_of.items() if n in ctx.main):
            return f"이 근무표에는 {req.grade}등급까지인 간호사가 없습니다."
        if not set(req.codes) <= usable:
            return "근무조는 D·E·N" + ("·M" if ctx.use_mid else "") + " 중에서 말씀해 주세요."
        return None
    if not req.alternatives or not all(req.alternatives):
        return "몇 명으로 할지 알려 주세요. 예) '평일 D 3명·E 3명·N 3명'"
    for alt in req.alternatives:
        for rule in alt:
            if rule.code not in usable:
                return ("하루 인원은 D·E·N" + ("·M" if ctx.use_mid else "") + " 근무로만 정할 수 있습니다."
                        + ("" if ctx.use_mid else " 이 병동은 MID 근무를 쓰지 않습니다."))
            if rule.low < 0 or (rule.high is not None and rule.high < rule.low):
                return f"{rule.code} 인원은 최소가 최대보다 크지 않게 말씀해 주세요."
            if rule.scope == "days" and not any(req.first_day <= d <= req.last_day for d in rule.days):
                return "정한 날이 고칠 수 있는 날 밖입니다."
    if req.chosen is not None and not 0 <= req.chosen < len(req.alternatives):
        return "고른 인원 계획이 없습니다. 다시 요청해 주세요."
    return None


def _cover_problem(ctx: EditContext, req: CoverTarget, open_from: int, today: Optional[date]) -> Optional[str]:
    if not 0 <= req.day < ctx.days:
        return f"{req.day + 1}일은 이 달에 없습니다."
    if req.day < open_from:
        return closed_day_text(ctx, req.day, open_from, today)
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


def _balance_problem(ctx: EditContext, req: BalanceTarget, open_from: int, today: Optional[date]) -> Optional[str]:
    usable = set(EDIT_MAINS) if ctx.use_mid else set(EDIT_MAINS) - {"M"}
    if not set(req.codes) <= usable:
        return f"고를 수 없는 근무입니다: {sorted(set(req.codes) - usable)}"
    if req.first_day >= ctx.days:
        return "이 달에 바꿀 수 있는 날이 남아 있지 않습니다."
    if req.first_day < open_from:
        return _closed_range(ctx, req.first_day, req.last_day, open_from, today)
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


def _count_problem(ctx: EditContext, req: CountTarget, open_from: int, today: Optional[date]) -> Optional[str]:
    name = ctx.names.get(req.nurse_id, req.nurse_id)
    if not 0 <= req.first_day <= req.last_day < ctx.days:
        return "기간이 이 달 안에 있지 않습니다."
    if req.first_day < open_from:
        return _closed_range(ctx, req.first_day, req.last_day, open_from, today)
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
        return f"{name}의 그 근무 중 {floor}회는 바꿀 수 없는 칸(고정 칸·고치지 않는 날)이라 {floor}회 아래로는 줄일 수 없습니다."
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
    if isinstance(req, GiveUpWish):
        return True
    if isinstance(req, StaffingPlan):
        g = grid or ctx.main
        return plan_short(ctx, g, req) <= (req.short or 0) and not staffing_over(ctx, g, req)
    if isinstance(req, GradeFloor):
        return sum(x for _d, _c, x in grade_gaps(ctx, grid or ctx.main, req)) <= (req.short or 0)
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
