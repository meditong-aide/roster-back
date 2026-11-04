"""수정 에이전트 기록(`schedule_edit_log`) — 대화 턴·사용자 행동을 한 건씩 남긴다.

  · message  사용자 원문 + LLM 해석(스킬·인자)          · propose  확정된 요청 + 결과(상태·후보·레버·이유)
  · clarify  되묻기(이름 모호 등)                       · lever / reject / keep  사용자 행동
  · apply    적용(새 버전)                              · error    처리 중 오류
  · choose   개수 요청에서 빠지는 근무를 메우는 방식을 고름
  · confirm / cancel  바꾸기 전 확인에 [그래도 진행] / [바꾸지 않기]   · check  적용 전 확인을 물음
  · close    사용자가 대화를 닫음(다시 이어 가지 않는다)
대화 id 로 한 대화를 순서대로 다시 보고(`read_conversation`), 재기동 뒤 대화 상태를 다시 세운다.

기록은 부가 기능이다. 표가 없거나 쓰기가 실패해도 수정 흐름은 멈추지 않는다(세이브포인트·경고 로그).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db.models import ScheduleEditLog

logger = logging.getLogger(__name__)

EVENTS = ("message", "propose", "clarify", "lever", "reject", "keep", "choose", "confirm", "cancel", "check", "apply",
          "error", "close", "open")
#: 대화 하나를 다시 읽을 때 상한 — 대화가 이보다 길면 앞부분은 상태 복원에 안 쓴다
READ_LIMIT = 500
_ready = False
_checked_at: Optional[float] = None
#: 표가 없을 때 다시 확인하는 간격 — 공용 DB 라 이벤트마다 카탈로그를 묻지 않는다(basis 와 같게)
_NEGATIVE_TTL = 300.0


def _table_ready(db: Session) -> bool:
    global _ready, _checked_at
    if _ready or (_checked_at is not None and time.monotonic() - _checked_at < _NEGATIVE_TTL):
        return _ready
    _checked_at = time.monotonic()
    try:
        _ready = sa_inspect(db.get_bind()).has_table(ScheduleEditLog.__tablename__)
    except SQLAlchemyError:
        _ready = False
    return _ready


def record_event(db: Session, schedule_id: str, event: str, payload: Dict[str, Any], *,
                 created_by: Optional[str] = None, new_schedule_id: Optional[str] = None,
                 conversation_id: Optional[str] = None, turn_no: Optional[int] = None,
                 elapsed_ms: Optional[int] = None) -> Optional[int]:
    """한 건 남기고 커밋한다. 남긴 log_id, 못 남겼으면 None."""
    if event not in EVENTS or not _table_ready(db):
        return None
    try:
        with db.begin_nested():
            row = ScheduleEditLog(conversation_id=conversation_id, turn_no=turn_no, schedule_id=schedule_id,
                                  new_schedule_id=new_schedule_id, event=event, payload=payload,
                                  elapsed_ms=elapsed_ms, created_by=created_by, created_at=datetime.now())
            db.add(row)
        db.commit()
        return row.log_id
    except SQLAlchemyError:
        db.rollback()
        logger.warning("[schedule_edit] 기록 실패: %s %s", schedule_id, event, exc_info=True)
        return None


def read_conversation(db: Session, conversation_id: str) -> List[ScheduleEditLog]:
    """대화 하나의 기록(오래된 것부터). 표가 없거나 읽기가 실패하면 빈 목록."""
    if not conversation_id or not _table_ready(db):
        return []
    try:
        rows = (db.query(ScheduleEditLog).filter(ScheduleEditLog.conversation_id == conversation_id)
                .order_by(ScheduleEditLog.log_id.desc()).limit(READ_LIMIT).all())
    except SQLAlchemyError:
        db.rollback()
        logger.warning("[schedule_edit] 기록 읽기 실패: %s", conversation_id, exc_info=True)
        return []
    return list(reversed(rows))


def read_conversation_head(db: Session, conversation_id: str) -> Optional[ScheduleEditLog]:
    """대화의 첫 기록 — 주인·시작 시각(만료 판정)의 기준. 없거나 읽기가 실패하면 None."""
    if not conversation_id or not _table_ready(db):
        return None
    try:
        return (db.query(ScheduleEditLog).filter(ScheduleEditLog.conversation_id == conversation_id)
                .order_by(ScheduleEditLog.log_id.asc()).first())
    except SQLAlchemyError:
        db.rollback()
        logger.warning("[schedule_edit] 기록 읽기 실패: %s", conversation_id, exc_info=True)
        return None


def read_apply_for(db: Session, schedule_id: str) -> Optional[ScheduleEditLog]:
    """이 버전을 만든 수정 도우미 적용 기록(없으면 None) — 변경 이력에 '무엇을 넘겨서 나온 수정인지' 보인다."""
    if not schedule_id or not _table_ready(db):
        return None
    try:
        return (db.query(ScheduleEditLog)
                .filter(ScheduleEditLog.new_schedule_id == schedule_id, ScheduleEditLog.event == "apply")
                .order_by(ScheduleEditLog.log_id.desc()).first())
    except SQLAlchemyError:
        db.rollback()
        logger.warning("[schedule_edit] 적용 기록 읽기 실패: %s", schedule_id, exc_info=True)
        return None


#: 변경 이력에 '이번만 넘긴 것'으로 보일 종류(근무 모양 악화는 따로)
BEND_KINDS = ("setting", "cover", "wanted")


def edit_origin(db: Session, schedule_id: str) -> Optional[Dict[str, Any]]:
    """변경 이력용 — 수정 도우미가 만든 버전이면 {request, bends, shape, at}. 넘긴 설정(bends)은 그 수정이 어떤 설정을
    이번만 넘겨서 나왔는지다(사용자 지시 11-03 '어떤 부분을 변경해서 발생한 수정인지 알기 위함')."""
    row = read_apply_for(db, schedule_id)
    if row is None:
        return None
    payload = row.payload or {}
    items = [a if isinstance(a, dict) else {"kind": "setting", "text": str(a)} for a in payload.get("accepted") or []]
    if "accepted" not in payload and payload.get("allow"):
        items = _bends_from_allow(db, payload["allow"])        # 이 기능 전(11-03 오전) 기록 — 고른 레버의 위치로
    return {
        "request": payload.get("request_text") or None,
        "bends": [a.get("text", "") for a in items if a.get("kind") in BEND_KINDS],
        "shape": [a.get("text", "") for a in items if a.get("kind") == "quality"],
        "at": row.created_at.isoformat() if row.created_at else None,
    }


def _bends_from_allow(db: Session, allow_json: List[Any]) -> List[Dict[str, Any]]:
    """옛 적용 기록에는 받아들인 것 목록이 없다 — 그 대화에서 고른 레버의 허용 위치를 문장으로(레버 카드와 같은 말)."""
    from db.models import Nurse
    from services.schedule_edit.explain import Bend, bend_text
    from services.schedule_edit.serialize import allow_from_json
    allow = allow_from_json(allow_json)
    ids = sorted({str(k[1]) for k in allow if len(k) > 1 and k[1]})
    names: Dict[str, str] = {}
    if ids:
        try:
            names = {str(n): str(m) for n, m in db.query(Nurse.nurse_id, Nurse.name).filter(Nurse.nurse_id.in_(ids))}
        except SQLAlchemyError:
            db.rollback()
    return [{"kind": "setting", "text": bend_text(Bend(k[0], k, amount), names)}
            for k, amount in sorted(allow.items(), key=lambda kv: str(kv[0]))]
