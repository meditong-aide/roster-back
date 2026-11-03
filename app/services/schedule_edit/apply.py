"""후보 적용 — 고른 후보로 새 버전을 만든다(사용자 결정 ② · 기존 [새 버전으로 저장] 규칙 그대로).

  1. 이 대화의 마지막 결과에 든 후보만 받는다 — 화면이 보낸 칸이 아니라 서버가 들고 있는 후보만 쓴다.
  2. 원본 행을 잠근 뒤(UPDLOCK) 후보를 찾은 뒤 근무표가 바뀌었는지(칸 지문) 본다 — 확인과 저장 사이에
     다른 저장이 끼면 새 버전이 그 칸을 옛 값으로 되돌린다(검토 11-02 M1). 저장도 같은 트랜잭션이라
     저장 함수가 다시 잠가도 막히지 않는다.
  3. 같은 판정(요청 충족·지난 날짜·엔진 기준·화면 기준, 고른 레버 허용량 포함)으로 다시 확인한다.
  4. [새 버전으로 저장]과 같은 함수로 저장한다 — 번호(그 달 최대+1)·이름('원본 이름 수정본')·출처
     (schedule_lineage kind=save_as)·변경 이력(source=save_as)·판정 기준 사본 승계가 그대로 따라온다.
  5. 수정 기록(`schedule_edit_log`, event=apply)에 요청문·후보·허용 레버와 적용 전 확인에서 받아들인 것(넘기는 설정·
     원티드 칸·근무 모양)을 남긴다(에이전트 출처·학습 재료).

★ 저장 규칙을 두 벌로 만들지 않으려고 라우터의 `save_roster_as_new_version` 을 직접 부른다(그 본문이
  아직 서비스로 분리돼 있지 않다). 권한(수간호사·ADM)·원본 잠금·같은 달 확인도 그 안에서 한다.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from db.models import Schedule
from services.schedule_edit.journal import record_event
from services.schedule_edit.serialize import allow_to_json, candidate_to_dict
from services.schedule_edit.session import EditSession, is_stale
from services.schedule_edit.solver import Candidate, verify_candidate

logger = logging.getLogger(__name__)


class ApplyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def apply_to_new_version(db: Session, session: EditSession, candidate: Candidate, current_user, *,
                         request_text: str = "", name: Optional[str] = None,
                         today: Optional[date] = None, conversation_id: Optional[str] = None,
                         turn_no: Optional[int] = None,
                         accepted: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if session.last is None or not any(c is candidate for c in session.last.candidates):
        raise ApplyError("unknown", "이 대화에서 마지막으로 찾은 후보가 아닙니다. 다시 후보를 찾아 주세요.")
    try:
        _lock_source(db, session.schedule_id)
        if is_stale(db, session):
            raise ApplyError("stale", "후보를 찾은 뒤 근무표가 바뀌었습니다. 다시 후보를 찾아 주세요.")
        problems = verify_candidate(session.ctx, session.rules, candidate, session.requests,
                                    allow=session.allow, screen=session.screen, today=today)
        if problems:
            raise ApplyError("invalid", "다시 확인해 보니 이 후보는 맞지 않습니다: " + "; ".join(problems[:3]))
    except Exception:
        db.rollback()   # 잠금을 푼다 — 잠금 자체가 실패(시간 초과·교착)해도 세션을 이어 쓸 수 있게
        raise
    saved = _save_as(db, session, candidate, current_user, name)
    if int(saved.get("changed_cells", -1)) != candidate.changed_count:
        # 출처 요약은 화면 기준(코드 비교)이라 하위코드만 바뀐 칸 등으로 다를 수 있다 — 막지 않고 남긴다
        logger.warning("[schedule_edit] 적용 칸 수 차이: 후보 %s · 저장 %s", candidate.changed_count,
                       saved.get("changed_cells"))
    record_event(db, session.schedule_id, "apply", {
        "request_text": request_text,
        "candidate": candidate_to_dict(session.ctx, candidate),
        "allow": allow_to_json(session.allow),
        "accepted": list(accepted or []),
    }, created_by=getattr(current_user, "account_id", None), new_schedule_id=saved.get("new_schedule_id"),
        conversation_id=conversation_id, turn_no=turn_no)
    return saved


def _lock_source(db: Session, schedule_id: str) -> None:
    """저장 함수와 같은 잠금(`save_roster_as_new_version` 의 원본 UPDLOCK) — `/roster/save` 와 차례로 선다."""
    db.query(Schedule.schedule_id).with_hint(Schedule, "WITH (UPDLOCK, ROWLOCK)", "mssql").filter(
        Schedule.schedule_id == schedule_id).first()


def _save_as(db: Session, session: EditSession, candidate: Candidate, current_user,
             name: Optional[str]) -> Dict[str, Any]:
    from routers.roster import save_roster_as_new_version   # 저장 규칙 한 벌 — 위 머리말
    from schemas.roster_schema import SaveAsRequest
    ctx = session.ctx
    req = SaveAsRequest(source_schedule_id=session.schedule_id, year=ctx.year, month=ctx.month,
                        roster=roster_payload(session, candidate), name=name, memo=None)
    try:
        return save_roster_as_new_version(req, current_user=current_user, db=db)
    except HTTPException as exc:
        db.rollback()
        raise ApplyError("save_failed", str(exc.detail)) from exc
    except Exception:
        db.rollback()   # DB 오류(잠금 시간 초과 등) — 세션을 이어 쓸 수 있게 되돌리고 그대로 올린다
        raise


def roster_payload(session: EditSession, candidate: Candidate) -> List[Dict[str, Any]]:
    """[새 버전으로 저장] 요청 모양 — 지금 칸 그대로 + 후보 칸만 바꾼다(바뀐 칸 id 는 저장 경로가 대표 행으로)."""
    ctx = session.ctx
    changed = {(c.nurse_id, c.day): c.after for c in candidate.changes}
    out = []
    for nid in ctx.nurse_ids:
        codes, ids = list(ctx.raw[nid]), list(ctx.raw_ids[nid])
        for day in range(ctx.days):
            if (nid, day) in changed:
                codes[day], ids[day] = changed[(nid, day)], None
        out.append({"nurse_id": nid, "schedule": codes, "schedule_ids": ids})
    return out
