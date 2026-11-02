"""수정 에이전트 기록(`schedule_edit_log`) — 제안·레버·넘김·적용을 한 건씩 남긴다.

기록은 부가 기능이다. 표가 없거나 쓰기가 실패해도 수정 흐름은 멈추지 않는다(세이브포인트·경고 로그).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db.models import ScheduleEditLog

logger = logging.getLogger(__name__)

EVENTS = ("propose", "lever", "reject", "apply")
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
                 created_by: Optional[str] = None, new_schedule_id: Optional[str] = None) -> Optional[int]:
    """한 건 남기고 커밋한다. 남긴 log_id, 못 남겼으면 None."""
    if event not in EVENTS or not _table_ready(db):
        return None
    try:
        with db.begin_nested():
            row = ScheduleEditLog(schedule_id=schedule_id, new_schedule_id=new_schedule_id, event=event,
                                  payload=payload, created_by=created_by, created_at=datetime.now())
            db.add(row)
        db.commit()
        return row.log_id
    except SQLAlchemyError:
        db.rollback()
        logger.warning("[schedule_edit] 기록 실패: %s %s", schedule_id, event, exc_info=True)
        return None
