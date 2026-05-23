"""AuditLogger — AgentMemoryAudit 단일 기록 헬퍼 (US-A5).

UserMemoryRepo 등에서 호출. 실패 시 silent log — fact 작업 자체는 성공으로 처리.
향후 SessionMemoryRepo / Procedural 계층에서도 재사용 가능.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def write_audit(
    db: Session,
    action: str,
    tier: str,
    row_id: int | None = None,
    who: str | None = None,
    why: str | None = None,
    agent_run_id: str | None = None,
) -> None:
    """단일 audit 행 기록.

    Args:
        db: SQLAlchemy Session (flush만 호출, commit은 호출자 책임).
        action: READ | WRITE | UPDATE | DELETE | EXPIRE
        tier: SESSION | USER | PROCEDURAL
        row_id: 영향 받은 row PK (가능 시).
        who: user_id 또는 agent identity.
        why: reason / context 텍스트.
        agent_run_id: evidence_session_id 등 추적 식별자.

    실패 시 ValueError/Exception 을 잡아 WARNING 로그만 남기고 반환.
    호출자의 트랜잭션을 깨뜨리지 않는다.
    """
    try:
        # import here to avoid circular at module level (models → client2 → engine)
        from db.models import AgentMemoryAudit

        row = AgentMemoryAudit(
            action=action,
            tier=tier,
            row_id=row_id,
            who=who,
            why=why,
            agent_run_id=agent_run_id,
        )
        db.add(row)
        db.flush()
        logger.debug(
            "[audit] action=%s tier=%s row_id=%s who=%s",
            action, tier, row_id, who,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[audit] write_audit failed (silent) action=%s tier=%s: %s",
            action, tier, exc,
        )
