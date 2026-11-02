"""근무표 수정 대화 한 번 — 판정 재료를 한 번 읽어 두고, 고른 것·넘긴 것·허용한 레버를 이어 간다.

  · 판정 재료(사본 기준 ctx·규칙·화면 기준선)는 열 때 한 번만 읽는다(dev 기준 0.3~1.6초).
  · 그사이 근무표가 바뀌었는지는 칸 지문(`cells_stamp`)으로 본다 — 바뀌었으면 다시 열어야 한다.
  · 레버를 고르면 허용량이 쌓이고, 다음 탐색부터 그만큼 허용한다.
  · 넘긴 후보의 '바뀐 사람 묶음'은 같은 요청에서 다시 내밀지 않는다(요청이 바뀌면 비운다 — 다른 요청에서는
    같은 사람 묶음이 다른 변경일 수 있고, 남겨 두면 엉뚱한 불가가 난다 · 검토 11-02 M3).
  · 레버는 이 대화의 마지막 결과에 든 것만 id 로 고른다(`allow_lever_id`) — 밖에서 온 허용량은 쓰지 않는다.
  · 고른 후보는 이어지는 요청(예: '그 안에서 15일은 빼줘')의 시작 해로 쓴다(CP-SAT 힌트).
대화 상태는 이 객체 하나다 — 에이전트 껍데기(P2)가 대화 id 로 들고 있는다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from db.models import Schedule, ScheduleEntry
from services.schedule_edit.context import EditContext, load_edit_context
from services.schedule_edit.levers import Lever, lever_allow
from services.schedule_edit.requests import EditRequest
from services.schedule_edit.rules import Grid, RuleSet, build_rules
from services.schedule_edit.screen_check import ScreenBaseline, load_screen_baseline
from services.schedule_edit.serialize import lever_id
from services.schedule_edit.solver import Candidate, EditResult, apply_candidate, propose_edits


@dataclass
class EditSession:
    schedule_id: str
    ctx: EditContext
    rules: RuleSet
    screen: Optional[ScreenBaseline]
    stamp: str
    allow: Dict[Tuple, int] = field(default_factory=dict)
    avoid_people: List[FrozenSet[str]] = field(default_factory=list)
    hint: Optional[Grid] = None
    requests: List[EditRequest] = field(default_factory=list)
    last: Optional[EditResult] = None


def cells_stamp(db: Session, schedule_id: str) -> str:
    """칸 지문 — 근무표 칸(간호사·날짜·코드·id) 전체의 해시. 한 칸이라도 바뀌면 달라진다."""
    rows = db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date, ScheduleEntry.shift_id, ScheduleEntry.id).filter(
        ScheduleEntry.schedule_id == schedule_id).all()
    text = "|".join(sorted(f"{n},{d:%Y-%m-%d},{s},{i}" for n, d, s, i in rows))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def open_session(db: Session, schedule_id: str, *, with_screen: bool = True) -> EditSession:
    ctx = load_edit_context(db, schedule_id)
    screen = None
    if with_screen:
        schedule = db.query(Schedule).filter(Schedule.schedule_id == schedule_id).first()
        screen = load_screen_baseline(db, schedule)
        note = screen_basis_note(ctx, screen)
        if note:
            ctx.notes.append(note)
    return EditSession(schedule_id=schedule_id, ctx=ctx, rules=build_rules(ctx), screen=screen,
                       stamp=cells_stamp(db, schedule_id))


SCREEN_DRIFT_NOTE = ("근무표를 만든 뒤 일별 인원 설정이 바뀌었습니다. 판정은 만들 때 기준이지만, 화면 빨간 칸은 지금 "
                     "설정 기준이라 화면에 새 표시가 생기는 후보는 내지 않습니다.")


def screen_basis_note(ctx: EditContext, screen: ScreenBaseline) -> Optional[str]:
    """사본(만들 때) 일별 최소 인원과 화면(지금) 값이 다르면 안내 — 두 기준이 갈리면 후보가 줄 수 있다(검토 M2)."""
    daily = screen.inputs.manpower_daily or {}
    for code in ("D", "E", "N", "M"):
        now = daily.get(code) or []
        for day in range(min(ctx.days, len(now), len(ctx.cov_min))):
            then = ctx.cov_min[day].get(code)
            if then is not None and now[day] is not None and int(now[day]) != int(then):
                return SCREEN_DRIFT_NOTE
    return None


def is_stale(db: Session, session: EditSession) -> bool:
    return cells_stamp(db, session.schedule_id) != session.stamp


def propose(session: EditSession, requests: Sequence[EditRequest], *, today: Optional[date] = None,
            keep_hint: bool = True) -> EditResult:
    """요청을 풀어 후보를 낸다. 같은 대화의 허용 레버·고른 후보는 이어 쓰고, 넘긴 묶음은 같은 요청일 때만."""
    if set(requests) != set(session.requests):   # 순서만 바뀐 같은 요청은 같은 요청
        session.avoid_people = []
    session.requests = list(requests)
    session.last = propose_edits(
        session.ctx, session.requests, rules=session.rules, screen=session.screen, today=today,
        allow=session.allow, avoid_people=session.avoid_people,
        hint=session.hint if keep_hint else None)
    return session.last


def allow_lever(session: EditSession, lever: Lever) -> None:
    """사용자가 고른 레버 — 허용량을 쌓는다(같은 위치를 두 번 고르면 더해진다). 레버 대상 규칙만."""
    for key, amount in lever_allow(lever.allow).items():
        session.allow[key] = session.allow.get(key, 0) + amount


def allow_lever_id(session: EditSession, chosen: str) -> Lever:
    """화면이 고른 레버 id → 마지막 결과의 레버. 없으면 KeyError(그사이 다시 찾았거나 지어낸 id)."""
    for lever in (session.last.levers if session.last else []):
        if lever_id(lever.allow) == chosen:
            allow_lever(session, lever)
            return lever
    raise KeyError(chosen)


def reject(session: EditSession, candidate: Candidate) -> None:
    """이 후보는 싫다 — 같은 사람 묶음을 이 대화에서 다시 내밀지 않는다."""
    session.avoid_people.append(frozenset(candidate.affected))


def keep(session: EditSession, candidate: Candidate) -> None:
    """이 후보를 기준으로 다듬는다 — 다음 탐색의 시작 해로 쓴다."""
    session.hint = apply_candidate(session.ctx, candidate)
