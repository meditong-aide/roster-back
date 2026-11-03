"""처음 화면 예시 — 이 근무표의 실제 이름·날짜로 만든다.

'○○○ 14일 오프로' 같은 틀은 누른 뒤 ○○○ 를 지우고 다시 써야 했다(ui-craft 검토 11-03). 누르면 그대로 보내도
되는 문장이 되도록, 바꿀 수 있는 날(오늘 뒤)의 근무 칸에서 고른다. 칸·이름만 읽고(근무 정의·칸·이름 세 조회)
판정 재료는 읽지 않는다 — 화면을 열 때마다 불린다. 읽지 못하면 틀 예시로 돌아간다.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from db.models import Schedule
from services.schedule_edit.context import EditContext, load_cells
from services.schedule_edit.requests import first_open_day

logger = logging.getLogger(__name__)

TEMPLATE = ["○○○ 14일 오프로", "15일 ○○○, ○○○ 근무 바꿔줘", "○○○ N 2개만 줄여줘", "15일 이브닝 한 명 더",
            "○○○ 25일 연차 넣어줘", "15일 나이트 누구야", "N 근무 수 고르게 맞춰줘", "반영 안 된 원티드 넣어줘",
            "남는 OFF 연차로 전환해줘"]
WORK = ("D", "E", "N")


def live_examples(db: Session, schedule: Optional[Schedule], today: date) -> List[str]:
    if schedule is None:
        return TEMPLATE
    try:
        year, month = int(schedule.year), int(schedule.month)
        ctx = EditContext(schedule_id=str(schedule.schedule_id), group_id=str(schedule.group_id),
                          office_id=str(getattr(schedule, "office_id", "") or ""), year=year, month=month,
                          days=calendar.monthrange(year, month)[1])
        load_cells(db, ctx)
    except Exception:       # 예시는 거들 뿐 — 처음 화면을 막지 않는다
        logger.warning("[schedule_edit] 예시 재료를 읽지 못해 틀 예시를 씁니다", exc_info=True)
        return TEMPLATE
    return examples_from(ctx, today) or TEMPLATE


def examples_from(ctx: EditContext, today: date) -> List[str]:
    """바꿀 수 있는 날이 없으면(지난 달) 빈 목록."""
    # 오늘 칸은 이미 근무 중일 수 있어 내일부터 · 사람은 근무표 순서, 날은 앞에서부터
    days = list(range(first_open_day(ctx, today) + 1, ctx.days))
    people = [n for n in ctx.nurse_ids if ctx.names.get(n)]
    if not days or len(people) < 2:
        return []
    name = ctx.names.get

    def works(nid: str, d: int, codes=WORK) -> bool:
        return ctx.main[nid][d] in codes and ctx.is_free(nid, d)

    out: List[str] = []
    first = _cell(people, days, works)
    if first:
        out.append(f"{name(first[0])} {first[1] + 1}일 오프로")
    pair = _pair(ctx, people, days, works)
    if pair:
        d, a, b = pair
        out.append(f"{d + 1}일 {name(a)}, {name(b)} 근무 바꿔줘")
    # N 을 덜 할 수 있는 사람만 — 그 달에 D·E 근무가 없으면 N 전담일 수 있어 줄이면 불가로 끝난다
    mixed = [n for n in people if any(c in ("D", "E") for c in ctx.main[n])]
    nights = sorted(((sum(works(n, d, ("N",)) for d in days), n) for n in mixed),
                    key=lambda t: (-t[0], people.index(t[1])))
    if nights and nights[0][0] >= 2:
        out.append(f"{name(nights[0][1])} N 1개만 줄여줘")
    if first:
        out.append(f"{first[1] + 1}일 이브닝 한 명 더")
    others = [n for n in people if not first or n != first[0]]
    leave = _cell(others, days[len(days) // 2:] or days, works)
    if leave:
        out.append(f"{name(leave[0])} {leave[1] + 1}일 연차 넣어줘")
    if first:
        out.append(f"{first[1] + 1}일 나이트 누구야")
    out.append("N 근무 수 고르게 맞춰줘")
    out.append("반영 안 된 원티드 넣어줘")
    out.append("남는 OFF 연차로 전환해줘")
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
