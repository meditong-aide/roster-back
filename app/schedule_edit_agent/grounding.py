"""LLM 이 넘긴 인자(말한 그대로의 이름·날짜·근무)를 이 근무표의 요청으로 확정한다.

이름은 근무표에 있는 간호사 안에서만 찾는다 — 같은 이름이 둘이거나, 일부만 말해 여럿이 걸리거나, 없으면
되묻는다(추측하지 않는다). 근무는 LLM 이 스키마의 정해진 값(D·E·N·M·OFF)으로 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.schedule_edit.context import EditContext
from services.schedule_edit.requests import AvoidCell, EditRequest, SetCell, swap

SHIFTS = ("D", "E", "N", "M", "OFF")
#: 기간 OFF 한 번에 받는 최대 날 수 — 그보다 길면 휴가 처리 대상이라 되묻는다
MAX_PERIOD_DAYS = 14


@dataclass
class Grounded:
    requests: List[EditRequest] = field(default_factory=list)
    #: 되물을 것 — 하나라도 있으면 요청을 풀지 않고 묻는다
    asks: List[str] = field(default_factory=list)


def ground_changes(ctx: EditContext, changes: Sequence[Dict[str, Any]]) -> Grounded:
    out = Grounded()
    for change in changes or []:
        reqs, ask = _one(ctx, change)
        if ask:
            out.asks.append(ask)
        else:
            out.requests.extend(r for r in reqs if r not in out.requests)
    if not out.requests and not out.asks:
        out.asks.append("무엇을 바꿀지 알려 주세요. 예) '김민지 14일 OFF', '15일 나이트 이영희랑 바꿔 줘'")
    return out


def _one(ctx: EditContext, change: Dict[str, Any]) -> Tuple[List[EditRequest], Optional[str]]:
    kind = change.get("kind")
    nurse, ask = resolve_nurse(ctx, change.get("nurse"))
    if ask:
        return [], ask
    day, ask = resolve_day(ctx, change.get("day"))
    if ask:
        return [], ask
    if kind == "set":
        code, ask = resolve_shift(ctx, change.get("shift"))
        return ([], ask) if ask else ([SetCell(nurse, day, code)], None)
    if kind == "avoid":
        codes = []
        for said in change.get("shifts") or [change.get("shift")]:
            code, ask = resolve_shift(ctx, said)
            if ask:
                return [], ask
            codes.append(code)
        return [AvoidCell(nurse, day, frozenset(codes))], None
    if kind == "swap":
        other, ask = resolve_nurse(ctx, change.get("other_nurse"))
        if ask:
            return [], ask
        if other == nurse:
            return [], "바꿀 두 사람이 같습니다. 누구와 바꿀지 알려 주세요."
        return swap(ctx, nurse, other, day), None
    if kind == "off_period":
        last, ask = resolve_day(ctx, change.get("to_day") or change.get("day"))
        if ask:
            return [], ask
        if last < day:
            day, last = last, day
        if last - day + 1 > MAX_PERIOD_DAYS:
            return [], f"OFF 기간이 {MAX_PERIOD_DAYS}일보다 깁니다. 휴가 신청으로 처리해 주세요."
        return [SetCell(nurse, d, "O") for d in range(day, last + 1)], None
    return [], "어떤 변경인지 알기 어렵습니다. 예) '김민지 14일 OFF'"


def resolve_nurse(ctx: EditContext, said: Any) -> Tuple[Optional[str], Optional[str]]:
    """이름 → nurse_id. 정확히 같은 이름 1명 → 그 사람, 아니면 성을 뺀 이름('민지' → 김민지)이 맞는 1명.

    일부 일치는 두 글자 이상이고 이름 **끝**이 맞을 때만 받는다 — 한 글자·성만·가운데 글자('지은' → 박지은수)로
    엉뚱한 사람을 고르지 않게(검토 M4). 고른 사람의 전체 이름은 답변·카드에 그대로 나와 적용 전에 보인다.
    """
    key = "".join(str(said or "").split())
    if not key:
        return None, "누구의 근무인지 알려 주세요."
    if key in ctx.names:
        return key, None
    exact = [n for n in ctx.nurse_ids if "".join(ctx.names.get(n, "").split()) == key]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, f"이 근무표에 '{key}' 이름이 {len(exact)}명 있습니다. {_who(ctx, exact)} 중 누구인가요?"
    partial = [n for n in ctx.nurse_ids if len(key) >= 2 and _given_name_match(ctx.names.get(n, ""), key)]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, f"'{key}'(이)라는 이름이 여럿입니다. {_who(ctx, partial)} 중 누구인가요?"
    return None, f"이 근무표에 '{key}' 간호사가 없습니다. 이름을 다시 확인해 주세요."


def _given_name_match(name: str, key: str) -> bool:
    full = "".join(name.split())
    return len(full) > len(key) and full.endswith(key)


def _who(ctx: EditContext, nurse_ids: Sequence[str]) -> str:
    names = [ctx.names.get(n, n) for n in nurse_ids[:5]]
    if len(set(names)) < len(names):   # 동명이인 — 사번 끝자리로 구분
        names = [f"{ctx.names.get(n, n)}({str(n)[-4:]})" for n in nurse_ids[:5]]
    return "·".join(names) + (" 등" if len(nurse_ids) > 5 else "")


def resolve_day(ctx: EditContext, said: Any) -> Tuple[Optional[int], Optional[str]]:
    """날짜(일, 1부터) → 0부터. 이 달에 없는 날은 되묻기."""
    try:
        day = int(said)
    except (TypeError, ValueError):
        return None, "며칠인지 알려 주세요."
    if not 1 <= day <= ctx.days:
        return None, f"{ctx.month}월에는 {day}일이 없습니다."
    return day - 1, None


def resolve_shift(ctx: EditContext, said: Any) -> Tuple[Optional[str], Optional[str]]:
    code = str(said or "").upper()
    if code not in SHIFTS:
        return None, "어떤 근무로 바꿀지 알려 주세요(D·E·N·OFF)."
    if code == "M" and not ctx.use_mid:
        return None, "이 병동은 MID 근무를 쓰지 않습니다."
    return ("O" if code == "OFF" else code), None
