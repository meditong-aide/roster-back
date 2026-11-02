"""수정 요청의 기본 단위 — 칸 지정·칸 금지. 교환·기간 OFF 는 이 둘로 풀어 만든다.

자연어 해석(이름→nurse_id, '나이트'→N, 날짜)은 스킬 층이 한다. 여기는 해석이 끝난 값만 받는다.
day 는 0부터(1일 = 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, FrozenSet, Iterable, List, Optional, Union

from services.schedule_edit.context import EDIT_MAINS, EditContext


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


EditRequest = Union[SetCell, AvoidCell]


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
    if req.nurse_id not in ctx.main:
        return f"{req.nurse_id} 는 이 근무표에 없습니다."
    if not 0 <= req.day < ctx.days:
        return f"{req.day + 1}일은 이 달에 없습니다."
    if req.day < open_from:
        return f"{req.day + 1}일은 이미 지난 날짜라 바꿀 수 없습니다."
    name = ctx.names.get(req.nurse_id, req.nurse_id)
    reason = ctx.frozen.get((req.nurse_id, req.day))
    if reason:
        return f"{name} {req.day + 1}일은 '{reason}' 칸이라 바꿀 수 없습니다."
    codes = {req.code} if isinstance(req, SetCell) else set(req.codes)
    usable = set(EDIT_MAINS) if ctx.use_mid else set(EDIT_MAINS) - {"M"}
    if not codes <= usable:
        return f"고를 수 없는 근무입니다: {sorted(codes - usable)}"
    if isinstance(req, SetCell) and req.code in ctx.forbidden.get((req.nurse_id, req.day), set()):
        return f"{name} {req.day + 1}일 {req.code} 는 허용 근무가 아니거나 기피 근무입니다."
    if isinstance(req, AvoidCell) and not usable - codes:
        return "그 칸에 넣을 수 있는 근무가 남지 않습니다."
    return None


def is_satisfied(ctx: EditContext, req: EditRequest, grid: Optional[Dict[str, List]] = None) -> bool:
    """grid 를 주면 그 격자에서(후보 적용 뒤), 없으면 지금 근무표에서."""
    current = (grid or ctx.main)[req.nurse_id][req.day]
    if isinstance(req, SetCell):
        return current == req.code
    return current not in req.codes
