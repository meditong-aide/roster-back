"""log-feedback skill — 에이전트가 처리 못 하는 불만/건의/버그신고를 접수(agent_feedback).

triage 정책의 '접수' 경로: 스킬이 없는 버그신고/기능건의를 여기 적재하고 정직하게 안내.
절대 '고쳤다'고 환각하지 않는다. 테이블 부재 시 graceful skip(운영 마이그레이션 전 무해).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register

logger = logging.getLogger(__name__)

# 테이블 존재 프로브 캐시(process-wide) — 미들웨어 audit 패턴과 동일.
_feedback_table_present: bool | None = None

_KINDS = {"bug": "버그 신고", "suggestion": "기능 건의", "complaint": "불만"}


@register("log-feedback")
def log_feedback(db: Session, params: dict) -> Any:
    """불만/건의/버그를 접수. params: content(필수), kind(bug|suggestion|complaint), group_id/nurse_id(ctx)."""
    content = (params.get("content") or "").strip()
    if not content:
        return {"needs_clarification": True, "question": "어떤 내용을 접수할까요?", "options": []}
    kind = (params.get("kind") or "complaint").lower()
    if kind not in _KINDS:
        kind = "complaint"

    logged = _persist(db, params, content, kind)
    label = _KINDS[kind]
    # 정직 안내: 접수됨(=개발/운영팀 전달) — 에이전트가 직접 해결한 게 아님.
    return {
        "ok": True, "operation": "log_feedback", "kind": kind, "logged": logged,
        "message": (f"'{content[:60]}' {label}(으)로 접수했습니다. 담당팀에 전달돼 확인 예정입니다. "
                    "(이 항목은 제가 직접 처리할 수 없는 시스템/권한/개발 사안입니다.)"),
    }


def _persist(db: Session, params: dict, content: str, kind: str) -> bool:
    """agent_feedback 1행 적재. 테이블 부재/실패 시 False(접수 안내는 그대로 나감)."""
    global _feedback_table_present
    if _feedback_table_present is False:
        return False
    try:
        from sqlalchemy import inspect as _sa_inspect

        from db.models import AgentFeedback

        if _feedback_table_present is None:
            _feedback_table_present = _sa_inspect(db.get_bind()).has_table(AgentFeedback.__tablename__)
            if not _feedback_table_present:
                logger.warning("[log_feedback] agent_feedback 테이블 없음 — 적재 skip(마이그레이션 필요).")
                return False
        row = AgentFeedback(
            conversation_id=params.get("conversation_id") or params.get("acting_user_id"),
            group_id=params.get("group_id"),
            nurse_id=params.get("acting_user_id") or params.get("nurse_id"),
            kind=kind, content=content[:1000], status="open",
        )
        db.add(row)
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001 — 접수 실패해도 턴 안 깨짐
        logger.warning("[log_feedback] 적재 실패: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False
