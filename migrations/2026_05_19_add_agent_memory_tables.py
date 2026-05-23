"""Migration 2026-05-19 — Agent memory tables + AgentMemoryAudit.who NOT NULL.

신규 5 테이블:
  agent_conversation, agent_conversation_message,
  agent_user_memory, agent_memory_audit, agent_skill_invocation

스키마 변경:
  agent_memory_audit.who: NULL → NOT NULL (의료 audit 요건, Architect SUGGESTION)

실행:
  cd roster-back
  PYTHONPATH=app python migrations/2026_05_19_add_agent_memory_tables.py
  PYTHONPATH=app python migrations/2026_05_19_add_agent_memory_tables.py --dry-run
  PYTHONPATH=app python migrations/2026_05_19_add_agent_memory_tables.py --rollback

운영 (MSSQL) 적용 절차:
  1. DB 백업 (필수).
  2. 기존 agent_memory_audit row 가 있다면 who IS NULL row 정리
     (본 스크립트가 'SYSTEM' fallback 으로 채워줌).
  3. 본 스크립트 실행 (멱등성 보장: 이미 있으면 skip).
  4. 검증: SELECT COUNT(*) FROM agent_memory_audit WHERE who IS NULL → 0.

멱등성:
  - 이미 존재하는 테이블은 CREATE skip.
  - 이미 NOT NULL 인 컬럼은 ALTER skip.
  - 데이터 손실 없음.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Path setup — repo root 기준 PYTHONPATH=app 가정
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from sqlalchemy import inspect, text  # noqa: E402

from db.client2 import engine as default_engine  # noqa: E402
from db.models import (  # noqa: E402
    Base,
    AgentConversation,
    AgentConversationMessage,
    AgentUserMemory,
    AgentMemoryAudit,
    AgentSkillInvocation,
)

logger = logging.getLogger(__name__)


NEW_TABLES = [
    AgentConversation.__table__,
    AgentConversationMessage.__table__,
    AgentUserMemory.__table__,
    AgentMemoryAudit.__table__,
    AgentSkillInvocation.__table__,
]


def _create_missing_tables(engine, *, dry_run: bool) -> list[str]:
    """기존에 없는 테이블만 CREATE. 반환: 생성된 테이블 이름 목록."""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    to_create = [t for t in NEW_TABLES if t.name not in existing]
    created = [t.name for t in to_create]

    if not to_create:
        logger.info("[create] all target tables already exist — skip")
        return []

    logger.info("[create] target tables: %s", created)
    if dry_run:
        logger.info("[create] DRY RUN — no CREATE executed")
        return created

    Base.metadata.create_all(bind=engine, tables=to_create)
    logger.info("[create] DONE created=%s", created)
    return created


def _enforce_who_not_null(engine, *, dry_run: bool) -> bool:
    """agent_memory_audit.who NULL → NOT NULL.

    MSSQL/MySQL/Postgres: ALTER COLUMN.
    SQLite: ALTER COLUMN NOT NULL 미지원 — 신규 테이블이면 model nullable=False 로 이미 반영됨.

    반환: ALTER 실행 여부.
    """
    inspector = inspect(engine)
    if "agent_memory_audit" not in set(inspector.get_table_names()):
        logger.info("[alter] agent_memory_audit not present — skip ALTER")
        return False

    cols = {c["name"]: c for c in inspector.get_columns("agent_memory_audit")}
    who_col = cols.get("who")
    if who_col is None:
        logger.warning("[alter] agent_memory_audit.who column not found — skip")
        return False
    if not who_col.get("nullable", True):
        logger.info("[alter] agent_memory_audit.who is already NOT NULL — skip")
        return False

    dialect = engine.dialect.name
    logger.info("[alter] dialect=%s — applying who NOT NULL", dialect)
    if dry_run:
        logger.info("[alter] DRY RUN — no ALTER executed")
        return False

    with engine.begin() as conn:
        # 기존 NULL row 정리 — 의료 audit 요건상 'SYSTEM' fallback
        result = conn.execute(text(
            "UPDATE agent_memory_audit SET who = 'SYSTEM' WHERE who IS NULL"
        ))
        affected = result.rowcount if hasattr(result, "rowcount") else -1
        logger.info("[alter] backfilled NULL who → 'SYSTEM' rows=%s", affected)

        if dialect == "mssql":
            conn.execute(text(
                "ALTER TABLE agent_memory_audit ALTER COLUMN who VARCHAR(64) NOT NULL"
            ))
        elif dialect == "mysql":
            conn.execute(text(
                "ALTER TABLE agent_memory_audit MODIFY COLUMN who VARCHAR(64) NOT NULL"
            ))
        elif dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE agent_memory_audit ALTER COLUMN who SET NOT NULL"
            ))
        else:
            logger.warning(
                "[alter] dialect=%s — ALTER COLUMN NOT NULL not supported. "
                "신규 테이블이면 model nullable=False 가 적용됨. "
                "기존 테이블이면 수동 마이그레이션 권장.",
                dialect,
            )
            return False

    logger.info("[alter] DONE agent_memory_audit.who NOT NULL")
    return True


def _rollback(engine, *, dry_run: bool) -> None:
    """5 신규 테이블 DROP. 데이터 손실 — 명시적 --rollback 플래그 필요."""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    # FK 의존: message → conversation 이므로 message 먼저 drop
    order = [
        AgentConversationMessage.__table__,
        AgentSkillInvocation.__table__,
        AgentMemoryAudit.__table__,
        AgentUserMemory.__table__,
        AgentConversation.__table__,
    ]
    target = [t for t in order if t.name in existing]
    if not target:
        logger.info("[rollback] no target tables present — nothing to drop")
        return

    logger.warning("[rollback] DROP TARGETS: %s", [t.name for t in target])
    if dry_run:
        logger.info("[rollback] DRY RUN — no DROP executed")
        return

    Base.metadata.drop_all(bind=engine, tables=target)
    logger.info("[rollback] DONE dropped=%s", [t.name for t in target])


def run_migration(engine=None, *, dry_run: bool = False, rollback: bool = False) -> None:
    if engine is None:
        engine = default_engine
    logger.info(
        "migration start dialect=%s dry_run=%s rollback=%s",
        engine.dialect.name, dry_run, rollback,
    )
    if rollback:
        _rollback(engine, dry_run=dry_run)
        return

    _create_missing_tables(engine, dry_run=dry_run)
    _enforce_who_not_null(engine, dry_run=dry_run)
    logger.info("migration done")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Agent memory tables migration (2026-05-19)")
    parser.add_argument("--dry-run", action="store_true", help="실행하지 않고 계획만 출력")
    parser.add_argument("--rollback", action="store_true", help="5 신규 테이블 DROP (데이터 손실)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_migration(dry_run=args.dry_run, rollback=args.rollback)


if __name__ == "__main__":
    _cli()
