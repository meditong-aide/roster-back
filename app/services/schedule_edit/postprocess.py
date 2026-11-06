"""생성 후처리를 이미 만든 근무표에 다시 돌린다 — 수정 도우미가 쓸 수 있는 범위(사용자 결정 11-03).

근무표를 만들 때 저장 직전에 도는 서비스 함수를 **그대로** 부른다(규칙을 두 벌로 만들지 않는다). 지금 칸을 그 함수에
넣어 미리 돌리고, 바뀌는 칸을 후보 하나로 낸다 — 적용은 다른 후보처럼 새 버전으로 저장한다(`apply.py`).
  · 자동 연차전환(annual_leave_swap) = `services.cp_sat.off_swap.postprocess_off_swap` — 병동 설정
    (off_swap_enabled · off_days · 바꿀 코드 off_swap_target · 주 2회 휴일 하한)대로. 연차 잔여 장부는 보지 않는다
    (생성할 때와 같다).
  · 설정은 그 근무표가 만들어질 때 쓴 것(schedules.config_id · [RVERIFY-08]).
  · 고치지 않는 날(`first_open_day`)·바꿀 수 없는 칸은 바꾸지 않는다 — 생성은 앞으로의 달이라 날짜를 보지 않는다.
  · 원티드 칸도 바꾸지 않는다 — 확정 원티드는 서비스가 이미 빼고, 지금 반영된 제출 원티드는 `protect` 로
    넘겨 서비스가 다른 OFF 를 고르게 한다(원티드 칸은 수간호사가 그 칸을 직접 짚을 때만 바꾼다 · 사용자 지시 11-03).
DB 는 읽기만 한다(서비스 함수도 읽기만 한다).
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from db.models import RosterConfig, Schedule
from services.cp_sat.off_swap import postprocess_off_swap
from services.schedule_edit.context import pure_off_count
from services.schedule_edit.requests import first_open_day
from services.schedule_edit.session import EditSession
from services.schedule_edit.solver import Candidate, CellChange, EditResult

ANNUAL_LEAVE_SWAP = "annual_leave_swap"


@dataclass
class PostprocessResult:
    kind: str
    changes: List[CellChange]
    #: 서비스 함수가 채운 숫자(off_swap 의 report) — 이유 문장 재료
    report: Dict[str, Any] = field(default_factory=dict)
    #: 서비스가 바꿨지만 고치지 않는 날이거나 바꿀 수 없는 칸이라 뺀 수
    held: int = 0


def load_schedule_config(db: Session, schedule_id: str) -> Tuple[Optional[Schedule], Optional[RosterConfig]]:
    schedule = db.query(Schedule).filter(Schedule.schedule_id == schedule_id).first()
    if schedule is None or not schedule.config_id:
        return schedule, None
    return schedule, db.query(RosterConfig).filter(RosterConfig.config_id == schedule.config_id).first()


def annual_leave_swap(db: Session, session: EditSession, today: date) -> PostprocessResult:
    """자동 연차전환을 지금 칸에 미리 돌린다. 저장하지 않는다."""
    ctx = session.ctx
    schedule, config = load_schedule_config(db, session.schedule_id)
    if schedule is None or config is None:
        return PostprocessResult(ANNUAL_LEAVE_SWAP, [], {"skip": "no_config"})
    before = {n: list(ctx.raw[n]) for n in ctx.nurse_ids}
    after = copy.deepcopy(before)
    report: Dict[str, Any] = {}
    wanted = {(n, d + 1) for (n, d), codes in ctx.wishes.items() if ctx.main[n][d] in codes}
    postprocess_off_swap(db, schedule, after, config, SimpleNamespace(year=ctx.year, month=ctx.month),
                         report=report, protect=wanted)
    open_from = first_open_day(ctx, today)
    changes: List[CellChange] = []
    held = 0
    for nid in ctx.nurse_ids:
        for day, (was, now) in enumerate(zip(before[nid], after[nid])):
            if was == now:
                continue
            if day < open_from or (nid, day) in ctx.frozen:
                held += 1
                continue
            # 연차도 쉬는 날이라 대표코드는 그대로 O — 규칙 판정(연속 근무·전환 금지 등)은 바뀌지 않는다
            changes.append(CellChange(nurse_id=nid, name=ctx.names.get(nid, nid), day=day, before=was,
                                      after=str(now), before_main=ctx.main[nid][day],
                                      after_main=ctx.main[nid][day] or "O", vacation=True))
    return PostprocessResult(ANNUAL_LEAVE_SWAP, changes, report, held)


def as_result(session: EditSession, result: PostprocessResult) -> EditResult:
    """바뀌는 칸을 후보 하나로 — 화면·적용이 솔버 후보와 같은 길로 다룬다."""
    ctx = session.ctx
    by_nurse = Counter(c.nurse_id for c in result.changes)
    days = [c.day for c in result.changes]
    counts = {}
    for nid, n in by_nurse.items():
        off = pure_off_count(ctx, nid, ctx.main[nid])
        counts[nid] = {"OFF": (off, off - n)}
    span = (min(days), max(days))
    cand = Candidate(rank=1, changes=sorted(result.changes, key=lambda c: (c.nurse_id, c.day)),
                     affected=sorted(by_nurse), optimal=True, window=span, counts=counts, span=span,
                     loss=len(result.changes))
    return EditResult("ok", candidates=[cand], notes=list(ctx.notes), source=result.kind)


#: 수정 도우미가 쓸 수 있는 생성 후처리 → 미리 돌리는 함수 — 하나씩 붙인다(사용자 결정 11-03 · 보건휴가·수면 OFF·
#: 휴직 가림·콜 당번은 다음)
RUNNERS = {ANNUAL_LEAVE_SWAP: annual_leave_swap}
KINDS = tuple(RUNNERS)
