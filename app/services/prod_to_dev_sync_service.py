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

# DB 에 PK 제약이 없는 테이블의 명시적 PK (모델 정의 기반)
EXPLICIT_PKS = {
    "schedules": ["schedule_id"],
    "schedule_entries": ["entry_id"],
    "wanted_requests": ["nurse_id", "request_id", "month"],
    "nurse_shift_requests": ["nurse_id", "request_id", "detailed_request_id"],
    "nurse_pair_requests": ["nurse_id", "request_id", "month"],
    "issued_roster": ["office_id", "group_id", "version"],
}

# (table, mode) — FK 부모 → 자식 순서
# mode="wipe":  dev 전체 삭제 후 prod 복사 (마스터 — prod 완전 미러)
# mode="upsert": MERGE (prod row 있으면 UPDATE, 없으면 INSERT, dev-only 보존)
SYNC_TABLES: List[tuple] = [
    # 마스터 (prod 완전 미러)
    ("offices", "wipe"),
    ("groups", "wipe"),
    ("teams", "wipe"),
    ("nurses", "wipe"),
    ("roster_config", "wipe"),
    ("roster_grade_config", "wipe"),
    ("wanted_config", "wipe"),
    ("weekly_off_settings", "wipe"),
    ("shifts", "wipe"),
    # 트랜잭션 (dev 테스트 데이터 보존)
    ("wanted_requests", "upsert"),
    # nurse_shift_requests / nurse_pair_requests / issued_roster:
    # prod에 PK 제약이 없어 중복 발생 → MERGE 불가, wipe 로 처리
    ("nurse_shift_requests", "wipe"),
    ("nurse_pair_requests", "wipe"),
    ("fixed_wanted_entries", "upsert"),
    ("schedules", "upsert"),
    ("schedule_entries", "upsert"),
    ("daily_shift", "upsert"),
    ("issued_roster", "wipe"),
    ("issued_roster_snapshot", "upsert"),
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


def _get_pk_cols(db: Session, db_name: str, table: str) -> List[str]:
    rows = db.execute(
        text(
            f"""
            SELECT kcu.COLUMN_NAME
            FROM {db_name}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN {db_name}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
             AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
             AND tc.TABLE_NAME = kcu.TABLE_NAME
            WHERE tc.CONSTRAINT_TYPE='PRIMARY KEY'
              AND tc.TABLE_SCHEMA='dbo' AND tc.TABLE_NAME=:t
            ORDER BY kcu.ORDINAL_POSITION
            """
        ),
        {"t": table},
    ).fetchall()
    return [r[0] for r in rows]


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


def _merge_upsert(db: Session, table: str) -> dict:
    """MERGE 로 prod → dev upsert. dev-only row 보존 (DELETE 절 없음)."""
    if not _table_exists(db, PROD_DB, table):
        return {"table": table, "skipped": "prod_missing", "upserted": 0}
    if not _table_exists(db, DEV_DB, table):
        return {"table": table, "skipped": "dev_missing", "upserted": 0}

    pk_cols = _get_pk_cols(db, DEV_DB, table) or EXPLICIT_PKS.get(table, [])
    if not pk_cols:
        return {"table": table, "skipped": "no_pk", "upserted": 0}

    dev_cols = _get_columns(db, DEV_DB, table)
    prod_cols = set(_get_columns(db, PROD_DB, table))
    common = [c for c in dev_cols if c in prod_cols]
    if not common:
        return {"table": table, "skipped": "no_common_cols", "upserted": 0}

    # 문자열 PK 컬럼의 collation 충돌 방지 (prod/dev DB 기본 collation 다를 수 있음)
    str_types = {"varchar", "nvarchar", "char", "nchar", "text", "ntext"}
    col_types = {}
    type_rows = db.execute(
        text(
            f"SELECT COLUMN_NAME, DATA_TYPE FROM {DEV_DB}.INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=:t"
        ),
        {"t": table},
    ).fetchall()
    for r in type_rows:
        col_types[r[0]] = r[1]

    non_pk = [c for c in common if c not in pk_cols]
    pk_parts = []
    for c in pk_cols:
        if col_types.get(c, "") in str_types:
            pk_parts.append(f"dst.[{c}] = src.[{c}] COLLATE DATABASE_DEFAULT")
        else:
            pk_parts.append(f"dst.[{c}] = src.[{c}]")
    pk_join = " AND ".join(pk_parts)
    insert_cols = ", ".join(f"[{c}]" for c in common)
    insert_vals = ", ".join(f"src.[{c}]" for c in common)
    update_clause = ""
    if non_pk:
        update_set = ", ".join(f"dst.[{c}] = src.[{c}]" for c in non_pk)
        update_clause = f"WHEN MATCHED THEN UPDATE SET {update_set}"

    sql = f"""
    MERGE {DEV_DB}.dbo.[{table}] AS dst
    USING {PROD_DB}.dbo.[{table}] AS src
        ON {pk_join}
    {update_clause}
    WHEN NOT MATCHED BY TARGET THEN
        INSERT ({insert_cols}) VALUES ({insert_vals});
    """

    has_identity = _has_identity(db, DEV_DB, table)
    if has_identity:
        db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.[{table}] ON"))
    try:
        result = db.execute(text(sql))
        affected = result.rowcount if result.rowcount is not None else -1
    finally:
        if has_identity:
            db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.[{table}] OFF"))

    return {"table": table, "upserted": affected}


def _fresh_session():
    """매 단계마다 새 세션을 발급해 connection drop 에 견디도록."""
    from db.client2 import SessionLocal
    return SessionLocal()


def _run_in_session(fn) -> tuple:
    """단일 세션 작업을 새 세션으로 실행. (result, error_str_or_None) 반환."""
    s = _fresh_session()
    try:
        out = fn(s)
        s.commit()
        return out, None
    except Exception as e:
        try:
            s.rollback()
        except Exception:
            pass
        return None, str(e)
    finally:
        try:
            s.close()
        except Exception:
            pass


def sync_prod_to_dev(db: Session = None, tables: Optional[List[str]] = None) -> dict:
    """마스터=wipe+copy / 트랜잭션=upsert(MERGE).

    각 단계를 **테이블별 독립 세션**으로 실행하여 connection drop 에 강건.
    db 인자는 호환용으로 받지만 무시.

    Args:
        tables: 특정 테이블만 sync (None 이면 SYNC_TABLES 전체).
                각 테이블의 mode 는 SYNC_TABLES 에서 lookup.
    """
    # 모드 lookup
    mode_map = {t: m for t, m in SYNC_TABLES}
    if tables:
        target_pairs = [(t, mode_map.get(t, "wipe")) for t in tables]
    else:
        target_pairs = list(SYNC_TABLES)

    results: List[dict] = []
    errors: List[dict] = []

    # 0. 사전 존재 확인
    def _check(s: Session):
        return [(t, m) for t, m in target_pairs if _table_exists(s, DEV_DB, t)]

    target_existing, err = _run_in_session(_check)
    if err or not target_existing:
        return {
            "tables": 0, "success": 0, "skipped": 0, "failed": len(target_pairs),
            "results": [], "errors": [{"phase": "precheck", "error": err or "no tables"}],
            "committed": False,
        }

    wipe_tables = [t for t, m in target_existing if m == "wipe"]
    upsert_tables = [t for t, m in target_existing if m == "upsert"]

    # 1. FK 비활성화 (전체)
    for table in [t for t, _ in target_existing]:
        _, err = _run_in_session(lambda s, t=table: _disable_fk(s, t))
        if err:
            errors.append({"phase": "disable_fk", "table": table, "error": err})
            logger.error("[prod→dev sync] FK disable 실패 %s: %s", table, err)

    # 2. wipe 대상만 역순 DELETE
    deleted_summary: dict = {}
    for table in reversed(wipe_tables):
        cnt, err = _run_in_session(lambda s, t=table: _delete_dev(s, t))
        if err:
            errors.append({"phase": "delete", "table": table, "error": err})
            logger.error("[prod→dev sync] DELETE 실패 %s: %s", table, err)
        else:
            deleted_summary[table] = cnt

    # 3. 순방향 처리 — wipe=INSERT, upsert=MERGE
    for table, mode in target_existing:
        if mode == "wipe":
            r, err = _run_in_session(lambda s, t=table: _copy_prod_to_dev(s, t))
            if err:
                errors.append({"phase": "insert", "table": table, "error": err})
                logger.error("[prod→dev sync] INSERT 실패 %s: %s", table, err)
                results.append({"table": table, "mode": "wipe", "error": err,
                                "inserted": 0, "deleted": deleted_summary.get(table, 0)})
            else:
                r["mode"] = "wipe"
                r["deleted"] = deleted_summary.get(table, 0)
                results.append(r)
        else:  # upsert
            r, err = _run_in_session(lambda s, t=table: _merge_upsert(s, t))
            if err:
                errors.append({"phase": "upsert", "table": table, "error": err})
                logger.error("[prod→dev sync] UPSERT 실패 %s: %s", table, err)
                results.append({"table": table, "mode": "upsert", "error": err, "upserted": 0})
            else:
                r["mode"] = "upsert"
                results.append(r)

    # 4. FK 재활성화
    for table in [t for t, _ in target_existing]:
        _, err = _run_in_session(lambda s, t=table: _enable_fk(s, t))
        if err:
            errors.append({"phase": "enable_fk", "table": table, "error": err})
            logger.error("[prod→dev sync] FK enable 실패 %s: %s", table, err)

    success = sum(1 for r in results if "error" not in r and "skipped" not in r)
    skipped = sum(1 for r in results if "skipped" in r)
    failed = sum(1 for r in results if "error" in r)
    committed = not bool(errors)

    summary = {
        "tables": len(target_existing),
        "success": success,
        "skipped": skipped,
        "failed": failed,
        "results": results,
        "errors": errors,
        "committed": committed,
    }
    logger.info(
        "[prod→dev sync] 완료 tables=%d success=%d skipped=%d failed=%d committed=%s",
        summary["tables"], success, skipped, failed, committed,
    )
    return summary
