"""LLM 이 넘긴 인자(말한 그대로의 이름·날짜·근무)를 이 근무표의 요청으로 확정한다.

이름은 근무표에 있는 간호사 안에서만 찾는다 — 같은 이름이 둘이거나, 일부만 말해 여럿이 걸리거나, 없으면
되묻는다(추측하지 않는다). 근무는 LLM 이 스키마의 정해진 값(D·E·N·M·OFF)으로 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.schedule_edit import distribution as dist
from services.schedule_edit.context import EditContext
from services.schedule_edit.requests import (
    TRANSFERS, AvoidCell, BalanceTarget, CountTarget, CoverTarget, EditRequest, LeaveCell, SetCell, balance_members,
    cover_count, eul, swap,
)
from services.schedule_edit.rules import coverage_bounds

SHIFTS = ("D", "E", "N", "M", "OFF")
#: 기간 OFF 한 번에 받는 최대 날 수 — 그보다 길면 휴가 처리 대상이라 되묻는다
MAX_PERIOD_DAYS = 14


@dataclass
class Grounded:
    requests: List[EditRequest] = field(default_factory=list)
    #: 되물을 것 — 하나라도 있으면 요청을 풀지 않고 묻는다
    asks: List[str] = field(default_factory=list)


def ground_changes(ctx: EditContext, changes: Sequence[Dict[str, Any]], open_from: int = 0) -> Grounded:
    """open_from: 바꿀 수 있는 첫날(0부터) — 개수 요청의 기간이 여기서 시작한다."""
    out = Grounded()
    for change in changes or []:
        reqs, ask = _one(ctx, change, open_from)
        if ask:
            out.asks.append(ask)
        else:
            out.requests.extend(r for r in reqs if r not in out.requests)
    if not out.requests and not out.asks:
        out.asks.append("무엇을 바꿀지 알려 주세요. 예) '김민지 14일 OFF', '15일 나이트 이영희랑 바꿔 줘'")
    return out


def _one(ctx: EditContext, change: Dict[str, Any], open_from: int) -> Tuple[List[EditRequest], Optional[str]]:
    kind = change.get("kind")
    if kind == "cover":                      # 그날 인원 — 사람을 짚지 않는다
        return _cover(ctx, change)
    if kind == "balance":                    # 여러 사람의 근무 수 고르게 — 사람을 짚지 않는다
        return _balance(ctx, change, open_from)
    nurse, ask = resolve_nurse(ctx, change.get("nurse"))
    if ask:
        return [], ask
    if kind == "count":
        return _count(ctx, change, nurse, open_from)
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
        # 교환은 대표코드끼리 맞바꾼다 — 대표코드가 같으면(하위코드만 달라도) 바뀌는 칸이 없다
        if ctx.main[nurse][day] == ctx.main[other][day]:
            word = SHIFT_WORD.get(ctx.main[nurse][day] or "", ctx.main[nurse][day] or "빈 칸")
            return [], (f"{ctx.names.get(nurse, nurse)}·{ctx.names.get(other, other)} 둘 다 {day + 1}일 {word}라 "
                        "바꿔도 같습니다. 다른 날이나 다른 사람을 말씀해 주세요.")
        return swap(ctx, nurse, other, day), None
    if kind == "leave":
        code, ask = resolve_leave(ctx, change.get("leave"))
        if ask:
            return [], ask
        last, ask = resolve_day(ctx, change.get("to_day") or change.get("day"))
        if ask:
            return [], ask
        days = range(min(day, last), max(day, last) + 1)
        if len(days) > MAX_PERIOD_DAYS:
            return [], f"휴가 기간이 {MAX_PERIOD_DAYS}일보다 깁니다. 휴가 신청으로 처리해 주세요."
        # 기간이면 바꿀 수 있는 근무일에만 — 쉬는 날·지난 날·고정 칸은 그대로 둔다(하루짜리는 요청 검사가 이유를
        # 말한다 · 검토 M3: 고정 칸 하나에 기간 전체가 거절됐다)
        work = ([d for d in days if ctx.main[nurse][d] not in (None, "O") and d >= open_from and ctx.is_free(nurse, d)]
                if len(days) > 1 else list(days))
        if not work:
            return [], f"{ctx.names.get(nurse, nurse)}의 그 기간은 모두 쉬는 날이라 {eul(ctx.leave_codes[code])} 넣을 날이 없습니다."
        return [LeaveCell(nurse, d, code) for d in work], None
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


def _balance(ctx: EditContext, change: Dict[str, Any], open_from: int) -> Tuple[List[EditRequest], Optional[str]]:
    """'N 근무 수 고르게 맞춰줘' · 'OFF 다 9개로' — 대상은 그 근무를 할 수 있는 전원(사용자 결정 11-03).
    차이를 말하지 않았으면 비워 둔다 — 스킬이 줄일 수 있는 가장 작은 차이를 찾는다."""
    code, ask = resolve_shift(ctx, change.get("shift"))
    if ask:
        return [], "어떤 근무의 수를 고르게 맞출까요? (D·E·N·OFF)"
    nums, bad = _numbers(change, "target", "spread")
    if bad:
        return [], "개수·차이는 숫자로 말씀해 주세요. 예) 'OFF 다 9개로'"
    if open_from >= ctx.days:
        return [], "이 달에 바꿀 수 있는 날이 남아 있지 않습니다."
    codes = frozenset({code})
    return [BalanceTarget(codes, balance_members(ctx, codes, open_from), open_from, ctx.days - 1,
                          nums["spread"], nums["target"])], None


def _cover(ctx: EditContext, change: Dict[str, Any]) -> Tuple[List[EditRequest], Optional[str]]:
    """그날 인원 — '15일 E 한 명 더'·'20일 N 한 명 빼'·'20일 N 에 1등급 넣어'. 인원을 말하지 않았으면 지금 인원을
    보여 주고 되묻는다(추측하지 않는다)."""
    day, ask = resolve_day(ctx, change.get("day"))
    if ask:
        return [], ask
    code, ask = resolve_shift(ctx, change.get("shift"))
    if ask:
        return [], ask
    if code == "O":
        return [], "그날 인원은 근무(D·E·N)로 말씀해 주세요. 예) '15일 E 한 명 더'"
    nums, bad = _numbers(change, "grade", "target", "change")
    if bad:
        return [], "인원·등급은 숫자로 말씀해 주세요. 예) '15일 E 한 명 더' · '20일 N 에 1등급 넣어'"
    grade = nums["grade"]
    probe = CoverTarget(day, code, 0, grade)
    now = cover_count(ctx, ctx.main, probe)
    who = f"{grade}등급까지인 사람 " if grade is not None else ""
    if nums["target"] is not None:
        target = nums["target"]
    elif nums["change"] is not None:
        target = now + nums["change"]
    elif grade is not None:
        # 넣기·빼기를 모르면 되묻는다 — 한 명 더로 짐작하면 '1등급 빼'가 거꾸로 풀린다(검토 11-03)
        return [], f"{day + 1}일 {code} 는 지금 {who}{now}명입니다. 한 명 더 넣을까요, 뺄까요?"
    else:
        need, upper, _over = coverage_bounds(ctx, day, code)
        rng = f"(설정 최소 {need}명" + (f" · 최대 {upper}명" if upper is not None else "") + ")"
        return [], f"{day + 1}일 {code} 는 지금 {who}{now}명입니다{rng}. 몇 명으로 바꿀까요?"
    if target == now:
        return [], f"{day + 1}일 {code} 는 이미 {who}{now}명입니다."
    return [CoverTarget(day, code, target, grade)], None


def _count(ctx: EditContext, change: Dict[str, Any], nurse: str,
           open_from: int) -> Tuple[List[EditRequest], Optional[str]]:
    """개수 요청 — '장원영 N 2개만 빼줘'. 기간은 바꿀 수 있는 첫날(또는 말한 날)부터 말일(또는 말한 날)까지.
    개수를 말하지 않았으면 현황을 보여 주고 되묻는다(사용자 결정 11-02 · 추측하지 않는다)."""
    code, ask = resolve_shift(ctx, change.get("shift"))
    if ask:
        return [], ask
    first, last = open_from, ctx.days - 1
    if change.get("day") is not None:
        day, ask = resolve_day(ctx, change.get("day"))
        if ask:
            return [], ask
        first = max(first, day)
    if change.get("to_day") is not None:
        day, ask = resolve_day(ctx, change.get("to_day"))
        if ask:
            return [], ask
        last = day
    if first > last:
        return [], "그 기간은 이미 지났거나 비어 있습니다. 기간을 다시 알려 주세요."
    row = ctx.main[nurse]
    # OFF 는 휴가·공가 칸을 세지 않는다(순수 OFF · `requests.counts_as`)
    now = sum(1 for d in range(first, last + 1) if row[d] == code and not (code == "O" and (nurse, d) in ctx.vacation))
    word = SHIFT_WORD[code]
    nums, bad = _numbers(change, "target", "change")
    if bad:
        return [], "개수는 숫자로 말씀해 주세요. 예) '장원영 N 2개만 빼줘'"
    if nums["target"] is not None:
        target = nums["target"]
    elif nums["change"] is not None:
        target = now + nums["change"]
    else:
        return [], count_status(ctx, nurse, code) + " 몇 회로 바꿀까요?"
    name = ctx.names.get(nurse, nurse)
    if target == now:
        return [], f"{name} {word}은(는) 이미 {now}회입니다."
    if target < 0:
        return [], f"{name}의 {word}은(는) {now}회라 그만큼 줄일 수 없습니다."
    transfer = change.get("transfer") if change.get("transfer") in TRANSFERS else None
    return [CountTarget(nurse, frozenset({code}), target, first, last, transfer)], None


#: 근무 종류를 사람에게 보일 때
SHIFT_WORD = {"D": "D", "E": "E", "N": "N", "M": "M", "O": "OFF"}


def count_status(ctx: EditContext, nurse: str, code: str) -> str:
    """'장원영 9월 N 5회(병동 평균 5.4회 · 가장 적은 사람 5회 · 가장 많은 사람 8회).' — 근무표에서만 센다."""
    members = (dist.night_range_members(ctx) if code == "N" else dist.range_members(ctx)) or ctx.nurse_ids

    def count(n: str) -> int:   # OFF 는 순수 OFF(휴가·공가 칸 빼고)
        return sum(1 for d, m in enumerate(ctx.main[n]) if m == code and not (code == "O" and (n, d) in ctx.vacation))

    counts = [count(n) for n in members]
    mine = count(nurse)
    head = f"{ctx.names.get(nurse, nurse)} {ctx.month}월 {SHIFT_WORD[code]} {mine}회"
    if len(counts) < 2:
        return head + "입니다."
    return (f"{head}입니다(병동 평균 {sum(counts) / len(counts):.1f}회 · 가장 적은 사람 {min(counts)}회 · "
            f"가장 많은 사람 {max(counts)}회).")


def resolve_leave(ctx: EditContext, said: Any) -> Tuple[Optional[str], Optional[str]]:
    """휴가 이름 → 이 병동의 휴가·공가 코드. 코드·이름이 같으면 그것, 아니면 이름 앞부분('연차' → 이름 '연차'·코드 '연').

    여럿이 맞거나 말하지 않았는데 코드가 여럿이면 되묻는다(추측하지 않는다).
    """
    codes = ctx.leave_codes
    if not codes:
        return None, "이 병동에는 휴가·공가 근무 코드가 없습니다. 근무코드 설정을 확인해 주세요."
    word = str(said or "").strip()
    listing = " · ".join(f"{c}({n})" if n != c else c for c, n in codes.items())
    exact = [c for c, n in codes.items() if word and word in (c, n)]
    if len(exact) == 1:            # 이름이 정확히 '휴가' 인 코드가 있으면 그것(실데이터 11-03)
        return exact[0], None
    if not word or word in ("휴가", "휴무"):
        if len(codes) == 1:
            return next(iter(codes)), None
        return None, f"어떤 휴가 코드로 넣을까요? ({listing})"
    near = exact or [c for c, n in codes.items() if n.startswith(word) or word.startswith(n) or word.startswith(c)]
    if len(near) == 1:
        return near[0], None
    if near:
        return None, "어떤 휴가 코드인가요? (" + " · ".join(f"{c}({codes[c]})" for c in near) + ")"
    return None, f"'{word}' 휴가 코드가 이 병동에 없습니다. 쓸 수 있는 코드: {listing}"


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
    if key.endswith("이") and len(key) >= 3:
        # '원영이' 처럼 이름 뒤에 붙여 부르는 '이' — 떼고 한 번 더 찾는다(못 찾으면 원래 말로 되묻는다)
        nurse, ask = resolve_nurse(ctx, key[:-1])
        if nurse is not None:
            return nurse, None
    return None, f"이 근무표에 '{key}' 간호사가 없습니다. 이름을 다시 확인해 주세요."


def _given_name_match(name: str, key: str) -> bool:
    full = "".join(name.split())
    return len(full) > len(key) and full.endswith(key)


def _who(ctx: EditContext, nurse_ids: Sequence[str]) -> str:
    names = [ctx.names.get(n, n) for n in nurse_ids[:5]]
    if len(set(names)) < len(names):   # 동명이인 — 사번 끝자리로 구분
        names = [f"{ctx.names.get(n, n)}({str(n)[-4:]})" for n in nurse_ids[:5]]
    return "·".join(names) + (" 등" if len(nurse_ids) > 5 else "")


def _numbers(change: Dict[str, Any], *keys: str) -> Tuple[Dict[str, Optional[int]], bool]:
    """LLM 이 숫자 칸에 넣은 값 — 비었으면 None, 정수가 아닌 값이 있으면 bad(되묻는다 · 검토 11-03: '1등급' 에 예외)."""
    out, bad = {}, False
    for key in keys:
        value = change.get(key)
        try:
            out[key] = None if value is None else int(value)
        except (TypeError, ValueError):
            out[key], bad = None, True
    return out, bad


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
