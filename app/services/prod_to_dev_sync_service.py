"""eun_roster (prod) → eun_roster_dev (dev) 일일 동기화 서비스.

전략: dev 전체 wipe 후 prod 완전 복사. 매일 07:00 실행.
Prod DB 에는 **SELECT 만** 발생 (쓰기 없음).
"""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PROD_DB = "eun_roster"
DEV_DB = "eun_roster_dev"

# FK 부모 → 자식 순서 (INSERT 순서)
# DELETE 는 이 배열 역순으로 실행
SYNC_TABLES: List[str] = [
    "offices",
    "groups",
    "teams",
    "nurses",
    "roster_config",
    "roster_grade_config",
    "wanted_config",
    "weekly_off_settings",
    "shifts",
    "wanted_requests",
    "nurse_shift_requests",
    "nurse_pair_requests",
    "fixed_wanted_entries",
    "schedules",
    "schedule_entries",
    "issued_roster",
    "issued_roster_snapshot",
]


def _table_exists(db: Session, db_name: str, table: str) -> bool:
    row = db.execute(
        text(
            f"SELECT 1 FROM {db_name}.INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=:t"
        ),
        {"t": table},
    ).fetchone()
    return bool(row)


def _get_columns(db: Session, db_name: str, table: str) -> List[str]:
    rows = db.execute(
        text(
            f"SELECT COLUMN_NAME FROM {db_name}.INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=:t "
            "ORDER BY ORDINAL_POSITION"
        ),
        {"t": table},
    ).fetchall()
    return [r[0] for r in rows]


def _has_identity(db: Session, db_name: str, table: str) -> bool:
    row = db.execute(
        text("SELECT OBJECTPROPERTY(OBJECT_ID(:full), 'TableHasIdentity')"),
        {"full": f"{db_name}.dbo.{table}"},
    ).fetchone()
    return bool(row and row[0])


def _disable_fk(db: Session, table: str) -> None:
    db.execute(text(f"ALTER TABLE {DEV_DB}.dbo.[{table}] NOCHECK CONSTRAINT ALL"))


def _enable_fk(db: Session, table: str) -> None:
    db.execute(
        text(f"ALTER TABLE {DEV_DB}.dbo.[{table}] WITH CHECK CHECK CONSTRAINT ALL")
    )


def _delete_dev(db: Session, table: str) -> int:
    result = db.execute(text(f"DELETE FROM {DEV_DB}.dbo.[{table}]"))
    return result.rowcount if result.rowcount is not None else -1


def _copy_prod_to_dev(db: Session, table: str) -> dict:
    """dev 에 prod 내용을 그대로 INSERT. 공통 컬럼만 복사."""
    if not _table_exists(db, PROD_DB, table):
        return {"table": table, "skipped": "prod_missing", "inserted": 0}
    if not _table_exists(db, DEV_DB, table):
        return {"table": table, "skipped": "dev_missing", "inserted": 0}

    dev_cols = _get_columns(db, DEV_DB, table)
    prod_cols = set(_get_columns(db, PROD_DB, table))
    common = [c for c in dev_cols if c in prod_cols]
    if not common:
        return {"table": table, "skipped": "no_common_cols", "inserted": 0}

    col_list = ", ".join(f"[{c}]" for c in common)
    sel_list = ", ".join(f"src.[{c}]" for c in common)

    has_identity = _has_identity(db, DEV_DB, table)

    sql = (
        f"INSERT INTO {DEV_DB}.dbo.[{table}] ({col_list}) "
        f"SELECT {sel_list} FROM {PROD_DB}.dbo.[{table}] AS src"
    )

    if has_identity:
        db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.[{table}] ON"))
    try:
        result = db.execute(text(sql))
        inserted = result.rowcount if result.rowcount is not None else -1
    finally:
        if has_identity:
            db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.[{table}] OFF"))

    return {"table": table, "inserted": inserted}


def sync_prod_to_dev(db: Session, tables: Optional[List[str]] = None) -> dict:
    """전체 wipe + copy. prod 에는 SELECT 만 발생.

    Args:
        db: dev DB 세션 (prod/dev 가 같은 서버라 cross-DB 접근 가능)
        tables: 특정 테이블만 sync (None 이면 SYNC_TABLES 전체)
    """
    target = tables or SYNC_TABLES
    target_existing = [t for t in target if _table_exists(db, DEV_DB, t)]
    results: List[dict] = []
    errors: List[dict] = []

    # 1. 모든 대상 테이블 FK 제약 비활성화
    for table in target_existing:
        try:
            _disable_fk(db, table)
        except Exception as e:
            errors.append({"phase": "disable_fk", "table": table, "error": str(e)})
            logger.error("[prod→dev sync] FK disable 실패 %s: %s", table, e)

    # 2. 역순 DELETE (자식 → 부모)
    deleted_summary = {}
    for table in reversed(target_existing):
        try:
            cnt = _delete_dev(db, table)
            deleted_summary[table] = cnt
        except Exception as e:
            errors.append({"phase": "delete", "table": table, "error": str(e)})
            logger.error("[prod→dev sync] DELETE 실패 %s: %s", table, e)

    # 3. 순방향 INSERT (부모 → 자식)
    for table in target_existing:
        try:
            r = _copy_prod_to_dev(db, table)
            r["deleted"] = deleted_summary.get(table, 0)
            results.append(r)
        except Exception as e:
            errors.append({"phase": "insert", "table": table, "error": str(e)})
            logger.error("[prod→dev sync] INSERT 실패 %s: %s", table, e, exc_info=True)
            results.append({"table": table, "error": str(e), "inserted": 0})

    # 4. FK 제약 재활성화
    for table in target_existing:
        try:
            _enable_fk(db, table)
        except Exception as e:
            errors.append({"phase": "enable_fk", "table": table, "error": str(e)})
            logger.error("[prod→dev sync] FK enable 실패 %s: %s", table, e)

    # 5. 커밋 (한 트랜잭션)
    if errors:
        db.rollback()
        logger.error("[prod→dev sync] 오류 발생 — 롤백: %s", errors)
    else:
        db.commit()

    summary = {
        "tables": len(target_existing),
        "results": results,
        "errors": errors,
        "committed": not bool(errors),
    }
    logger.info(
        "[prod→dev sync] 완료 tables=%d errors=%d committed=%s",
        summary["tables"], len(errors), summary["committed"],
    )
    return summary
