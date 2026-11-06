"""처음 화면 예시 — 이 근무표의 실제 이름·날짜로 만든다. 문장마다 그 말의 도구 호출(도구 이름·인자)을 함께 만들어,
누르면 LLM 없이 그 도구를 부른다(사용자 지시 11-06 '우리 도구 의존도를 높여 서비스적으로' · 토큰 0). 인자는 도구 정의
(`descriptions.py`)의 예와 같은 모양이고, 이름은 말한 그대로 넘겨 스킬이 근무표 안에서 확정한다.

'○○○ 14일 오프로' 같은 틀은 누른 뒤 ○○○ 를 지우고 다시 써야 했다(ui-craft 검토 11-03). 누르면 그대로 보내도
되는 문장이 되도록, 바꿀 수 있는 날(오늘 뒤)의 근무 칸에서 고른다. 칸·이름만 읽고(근무 정의·칸·이름 세 조회)
판정 재료는 읽지 않는다 — 화면을 열 때마다 불린다. 읽지 못하면 틀 예시로 돌아간다.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from db.models import Schedule
from services.schedule_edit.context import EditContext, load_cells
from services.schedule_edit.requests import first_open_day

logger = logging.getLogger(__name__)

TEMPLATE = ["○○○ 14일 오프로", "15일 ○○○, ○○○ 근무 바꿔줘", "○○○ N 2개만 줄여줘", "15일 이브닝 한 명 더",
            "○○○ 25일 연차 넣어줘", "15일 나이트 누구야", "N 근무 수 고르게 맞춰줘", "반영 안 된 원티드 넣어줘",
            "하루짜리 OFF 없애줘", "남는 OFF 연차로 전환해줘"]
WORK = ("D", "E", "N")
#: (도구 이름, 인자)
Call = Tuple[str, Dict[str, Any]]


def live_examples(db: Session, schedule: Optional[Schedule], today: date) -> List[str]:
    return [text for text, _call in live_example_calls(db, schedule, today)]


def live_example_calls(db: Session, schedule: Optional[Schedule], today: date) -> List[Tuple[str, Optional[Call]]]:
    """(문장, 도구 호출). 틀 예시(○○○)는 채워 써야 해 호출이 없다."""
    template: List[Tuple[str, Optional[Call]]] = [(text, None) for text in TEMPLATE]
    if schedule is None:
        return template
    try:
        year, month = int(schedule.year), int(schedule.month)
        ctx = EditContext(schedule_id=str(schedule.schedule_id), group_id=str(schedule.group_id),
                          office_id=str(getattr(schedule, "office_id", "") or ""), year=year, month=month,
                          days=calendar.monthrange(year, month)[1])
        load_cells(db, ctx)
    except Exception:       # 예시는 거들 뿐 — 처음 화면을 막지 않는다
        logger.warning("[schedule_edit] 예시 재료를 읽지 못해 틀 예시를 씁니다", exc_info=True)
        return template
    return list(example_calls(ctx, today)) or template


def examples_from(ctx: EditContext, today: date) -> List[str]:
    """바꿀 수 있는 날이 없으면(지난 달) 빈 목록."""
    return [text for text, _call in example_calls(ctx, today)]


def _edit(*changes: Dict[str, Any]) -> Call:
    return "propose_edit", {"changes": list(changes)}


def example_calls(ctx: EditContext, today: date) -> List[Tuple[str, Call]]:
    """문장과 그 말의 도구 호출 — 바꿀 수 있는 날이 없으면(지난 달) 빈 목록."""
    # 오늘 칸은 닫혀 있어 내일부터(`first_open_day`) · 사람은 근무표 순서, 날은 앞에서부터
    days = list(range(first_open_day(ctx, today), ctx.days))
    people = [n for n in ctx.nurse_ids if ctx.names.get(n)]
    if not days or len(people) < 2:
        return []
    name = ctx.names.get

    def works(nid: str, d: int, codes=WORK) -> bool:
        return ctx.main[nid][d] in codes and ctx.is_free(nid, d)

    out: List[Tuple[str, Call]] = []
    first = _cell(people, days, works)
    if first:
        out.append((f"{name(first[0])} {first[1] + 1}일 오프로",
                    _edit({"kind": "set", "nurse": name(first[0]), "day": first[1] + 1, "shift": "OFF"})))
    pair = _pair(ctx, people, days, works)
    if pair:
        d, a, b = pair
        out.append((f"{d + 1}일 {name(a)}, {name(b)} 근무 바꿔줘",
                    _edit({"kind": "swap", "nurse": name(a), "other_nurse": name(b), "day": d + 1})))
    # N 을 덜 할 수 있는 사람만 — 그 달에 D·E 근무가 없으면 N 전담일 수 있어 줄이면 불가로 끝난다
    mixed = [n for n in people if any(c in ("D", "E") for c in ctx.main[n])]
    nights = sorted(((sum(works(n, d, ("N",)) for d in days), n) for n in mixed),
                    key=lambda t: (-t[0], people.index(t[1])))
    if nights and nights[0][0] >= 2:
        out.append((f"{name(nights[0][1])} N 1개만 줄여줘",
                    _edit({"kind": "count", "nurse": name(nights[0][1]), "shift": "N", "change": -1})))
    if first:
        out.append((f"{first[1] + 1}일 이브닝 한 명 더",
                    _edit({"kind": "cover", "nurse": "", "day": first[1] + 1, "shift": "E", "change": 1})))
    others = [n for n in people if not first or n != first[0]]
    leave = _cell(others, days[len(days) // 2:] or days, works)
    if leave:
        out.append((f"{name(leave[0])} {leave[1] + 1}일 연차 넣어줘",
                    _edit({"kind": "leave", "nurse": name(leave[0]), "day": leave[1] + 1, "leave": "연차"})))
    if first:
        out.append((f"{first[1] + 1}일 나이트 누구야", ("lookup", {"kind": "day_shift", "day": first[1] + 1, "shift": "N"})))
    out.append(("N 근무 수 고르게 맞춰줘", _edit({"kind": "balance", "nurse": "", "shift": "N"})))
    out.append(("반영 안 된 원티드 넣어줘", _edit({"kind": "wanted", "nurse": ""})))
    out.append(("하루짜리 OFF 없애줘", _edit({"kind": "shape", "shape": "isolated_off", "nurse": ""})))
    out.append(("남는 OFF 연차로 전환해줘", ("run_postprocess", {"kind": "annual_leave_swap"})))
    return out


def _cell(people: List[str], days: List[int], works) -> Optional[Tuple[str, int]]:
    """앞쪽 날부터 근무 칸 하나(사람·날). 날을 먼저 돈다 — 가까운 날이 표에서 바로 보인다."""
    for d in days[1:] + days[:1]:       # 내일 칸은 바로 근무라 하루 뒤부터 고른다
        for n in people:
            if works(n, d):
                return n, d
    return None


def _pair(ctx: EditContext, people: List[str], days: List[int], works) -> Optional[Tuple[int, str, str]]:
    """같은 날 근무가 다른 두 사람(D·E 처럼) — 맞바꾸면 실제로 바뀐다."""
    for d in days[1:] + days[:1]:
        on = [n for n in people if works(n, d)]
        for i, a in enumerate(on):
            for b in on[i + 1:]:
                if ctx.main[a][d] != ctx.main[b][d]:
                    return d, a, b
    return None
