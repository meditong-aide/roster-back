"""버전 비교·되돌리기(11-04 · 사용자 결정 '통째는 열기·일부는 새 버전').

  · 같은 병동·같은 달·지우지 않은 버전만. 어느 버전인지는 해석기가 which(number·previous·first·latest·date·name)로 준다.
  · 비교 숫자는 화면 [버전 비교](`/roster/compare`)와 같은 함수(`compare_schedules`) — 둘이 달라 보이면 안 된다.
  · 일부 되돌리기 = 그 버전의 칸을 칸 지정 요청(SetCell·LeaveCell)으로 — 규칙 확인·가장 적게 바꾸기·적용 전 확인을
    다른 요청과 같은 길로 받는다. 지난 날·바꿀 수 없는 칸·도우미가 넣지 못하는 코드는 빼고 그 수를 알린다.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from db.models import Schedule, ScheduleLineage
from services.schedule_edit.context import EditContext, load_cells
from services.schedule_edit.requests import EditRequest, LeaveCell, SetCell, eun

logger = logging.getLogger(__name__)

#: 해석기가 고르는 '어느 버전' — number=VER 번호 · previous=이 버전을 만든 버전(없으면 바로 앞 번호) · first=이 달 첫 버전 ·
#: latest=가장 최근 버전 · date=그날 만든 버전 · name=버전 이름
WHICH = ("number", "previous", "first", "latest", "date", "name")
WHICH_ASK = "어느 버전과 견줄까요? 예) 'VER1과 뭐가 달라?', '바로 전 버전으로 되돌려줘', '처음 버전과 비교해줘'"
#: 되돌리기에서 뺀 칸의 이유 → 문장 조각
SKIP_WORD = {"past": "지난 날", "frozen": "바꿀 수 없는 칸(파견·확정 원티드 등)", "code": "도우미가 넣지 못하는 코드(주휴·특수 휴무 등)",
             "subcode": "같은 근무의 하위 코드만 다른 칸", "leave_on_off": "지금 쉬는 날인 칸의 휴가",
             "empty": "그 버전에 칸이 없는 날"}
#: 일부 되돌리기 한 번에 가져올 칸 상한 — 넘으면 좁혀 달라고 되묻는다(칸 상한이 이 수 + 10 까지 커진다)
REVERT_MAX_CELLS = 30


def versions_of(db: Session, schedule: Schedule) -> List[Schedule]:
    """같은 병동·같은 달의 지우지 않은 버전 — 번호 순."""
    return (db.query(Schedule)
            .filter(Schedule.group_id == schedule.group_id, Schedule.year == schedule.year,
                    Schedule.month == schedule.month, Schedule.dropped == False)  # noqa: E712 — SQL 비교
            .order_by(Schedule.version).all())


def label(s: Schedule) -> str:
    """예) 'VER2(11월 근무표 VER1 수정본)' — 이름이 없거나 'VER2' 그대로면 번호만."""
    name = str(getattr(s, "name", "") or "").strip()
    head = f"VER{s.version}"
    return head if not name or name == head else f"{head}({name})"


def _brief(versions: Sequence[Schedule]) -> str:
    return "·".join(f"VER{v.version}" + (f"({v.created_at:%m-%d})" if v.created_at else "") for v in versions)


def pick_version(db: Session, current: Schedule, which: Optional[str], *, number=None, on: Optional[str] = None,
                 name: Optional[str] = None) -> Tuple[Optional[Schedule], Optional[str]]:
    """(고른 버전, 되물을 문장). 지금 보는 버전·지운 버전·다른 달은 고르지 않는다."""
    versions = versions_of(db, current)
    others = [v for v in versions if v.schedule_id != current.schedule_id]
    if not others:
        return None, "이 달에는 다른 버전이 없습니다."
    listing = f"이 달 버전: {_brief(versions)}"
    if which == "number":
        try:
            wanted = int(number)
        except (TypeError, ValueError):
            return None, "몇 번 버전인지 숫자로 말씀해 주세요. " + listing
        if wanted == int(current.version):
            return None, f"지금 보고 있는 버전이 VER{wanted}입니다. " + listing
        hit = next((v for v in others if int(v.version) == wanted), None)
        return (hit, None) if hit is not None else (None, f"{eun(f'VER{wanted}')} 없습니다. " + listing)
    if which == "previous":
        source = _source_of(db, current.schedule_id)
        hit = next((v for v in others if v.schedule_id == source), None)
        if hit is None:
            older = [v for v in others if int(v.version) < int(current.version)]
            hit = older[-1] if older else None
        return (hit, None) if hit is not None else (None, "이 버전보다 앞선 버전이 없습니다. " + listing)
    if which in ("first", "latest"):
        hit = versions[0] if which == "first" else versions[-1]
        if hit.schedule_id == current.schedule_id:
            where = "첫" if which == "first" else "가장 최근"
            return None, f"지금 보고 있는 버전이 이 달 {where} 버전입니다. " + listing
        return hit, None
    if which == "date":
        try:
            day = date.fromisoformat(str(on or "")[:10])
        except ValueError:
            return None, "언제 만든 버전인지 날짜로 말씀해 주세요. " + listing
        hits = [v for v in others if v.created_at is not None and v.created_at.date() == day]
        return _one_of(hits, f"{day.month}월 {day.day}일에 만든 다른 버전이 없습니다. " + listing)
    if which == "name":
        wanted = str(name or "").strip()
        hits = [v for v in others if str(v.name or "").strip() == wanted] or \
               [v for v in others if wanted and wanted in str(v.name or "")]
        return _one_of(hits, f"'{wanted}' 이름의 버전이 없습니다. " + listing)
    return None, WHICH_ASK


def _one_of(hits: List[Schedule], none: str) -> Tuple[Optional[Schedule], Optional[str]]:
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, none
    shown = "·".join(f"VER{v.version}" + (f"({v.created_at:%H:%M})" if v.created_at else "") for v in hits)
    return None, f"해당하는 버전이 여럿입니다: {shown}. 몇 번 버전인지 말씀해 주세요."


def _source_of(db: Session, schedule_id: str) -> Optional[str]:
    """[새 버전으로 저장]의 출처 버전 — 출처 표가 아직 없으면(DDL 전) None(버전 목록과 같은 규약 · SAVEPOINT)."""
    try:
        with db.begin_nested():
            row = db.query(ScheduleLineage.source_schedule_id).filter(
                ScheduleLineage.schedule_id == schedule_id).first()
    except Exception:  # noqa: BLE001
        logger.warning("[schedule_edit] 버전 출처를 읽지 못해 번호로 고릅니다", exc_info=True)
        return None
    return row[0] if row else None


def load_version_cells(db: Session, ctx: EditContext, schedule_id: str) -> EditContext:
    """다른 버전의 칸 — 지금 판정 재료와 같은 근무 정의·칸 해석(`load_cells`)으로 읽는다."""
    other = EditContext(schedule_id=schedule_id, group_id=ctx.group_id, office_id=ctx.office_id, year=ctx.year,
                        month=ctx.month, days=ctx.days)
    load_cells(db, other)
    return other


@dataclass
class RevertPlan:
    requests: List[EditRequest] = field(default_factory=list)
    #: 그 버전과 다른 칸 수(범위 안 · 양쪽 명단에 다 있는 사람)와 뺀 칸 이유별 수
    differ: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    #: 범위 안 사람 중 그 버전 명단에 없는 사람
    missing: List[str] = field(default_factory=list)


def revert_plan(ctx: EditContext, other: EditContext, nurses: Sequence[str], first: int, last: int,
                open_from: int) -> RevertPlan:
    """그 버전 칸으로 되돌릴 칸 요청. 칸 비교는 코드 그대로(화면 비교와 같은 단위)."""
    plan = RevertPlan()
    skipped: Counter = Counter()
    for n in (nurses or ctx.nurse_ids):
        if n not in other.raw:
            plan.missing.append(n)
            continue
        for d in range(max(0, first), min(last, ctx.days - 1) + 1):
            now, then = ctx.raw[n][d], other.raw[n][d]
            if str(now or "").upper() == str(then or "").upper():
                continue
            plan.differ += 1                              # 화면 비교와 같게 한쪽만 빈 칸도 센다(검토 11-04 M3)
            if then is None:
                skipped["empty"] += 1
            elif d < open_from:
                skipped["past"] += 1
            elif not ctx.is_free(n, d):
                skipped["frozen"] += 1
            elif (n, d) in other.vacation:
                if then not in ctx.leave_codes:
                    skipped["code"] += 1
                elif ctx.main[n][d] in (None, "O"):
                    skipped["leave_on_off"] += 1          # 휴가는 근무일에만 넣는다(LeaveCell 규약)
                else:
                    plan.requests.append(LeaveCell(n, d, then))
            elif (n, d) in other.frozen or other.main[n][d] not in ("D", "E", "N", "M", "O"):
                skipped["code"] += 1
            elif other.main[n][d] == ctx.main[n][d]:
                skipped["subcode"] += 1
            else:
                plan.requests.append(SetCell(n, d, other.main[n][d]))
    plan.skipped = dict(skipped)
    return plan
