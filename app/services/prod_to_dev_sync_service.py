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
    "wanted": ["group_id", "year", "month"],
    # attribute period SSOT (id identity PK — _wipe_by_parent_fk 의 pk 가드 통과용)
    "nurse_preceptee_period": ["id"],
    "nurse_allowed_shift_period": ["id"],
    "nurse_weekendoff_period": ["id"],
    "nurse_grade_period": ["id"],
}

# group_id 컬럼이 없는 자식 테이블의 부모 매핑.
# include_group_ids 모드에서 부모 group_id 로 간접 wipe-by-parent 처리.
# 형식: { 자식테이블: (부모테이블, FK 컬럼) }
PARENT_FK_MAP = {
    "schedule_entries": ("schedules", "schedule_id"),
    # nurse 귀속 attribute period (group_id 없음) → nurse_id 로 nurses.group_id 간접 스코프
    "nurse_preceptee_period": ("nurses", "nurse_id"),
    "nurse_allowed_shift_period": ("nurses", "nurse_id"),
    "nurse_weekendoff_period": ("nurses", "nurse_id"),
    # 2026-08 신규 — group_id 가 없어(nurse_id 만) group 모드에서 스코프를 잡을 수단이
    # 이것뿐이다. 없으면 전체 미러로 빠져 다른 group 의 dev 행까지 지운다.
    "nurse_leave_period": ("nurses", "nurse_id"),
}

# group_id 컬럼이 없지만 다른 컬럼(들)으로 group 스코프가 가능한 테이블.
# 값이 리스트면 OR 스코프: (col1 IN (...) OR col2 IN (...)).
# (예: nurse_assignment 은 group_id 없이 source_group_id/target_group_id 만 보유 →
#  해당 group 이 source 이거나 target 인 배정 이력 모두 가져옴)
GROUP_COL_OVERRIDE = {
    "nurse_assignment": ["source_group_id", "target_group_id"],
}

# include_group_ids 모드에서 dev/prod 가 갈려(간호사 타그룹 이동·id 불일치) PK/UNIQUE 충돌이
# 나는 테이블 — 들어올 prod(그 group 스코프) 행과 아래 키가 겹치는 dev 행을 선삭제한다
# (cross-group/cross-id 정리). dev 에 nurses FK 없음 확인 → 안전.
CONFLICT_DELETE_KEYS = {
    "nurses": ["nurse_id"],                                             # PK cross-group
    "nurse_monthly_limits": ["nurse_id", "group_id", "year", "month"],  # UNIQUE(id 불일치)
}

# leaf satellite (자기 id 를 FK 로 참조하는 곳 없음) — 마이그 시 prod id 를 보존하지 않고
# dev 가 identity 재발번한다. dev/prod 가 독립 identity 라 id 를 보존하면 스코프 밖
# 다른 nurse/group 행과 PK 충돌(2627). 정합성·스코프는 nurse_id/group_id+valid_from 으로
# 유지되므로 surrogate id 는 버려도 안전.
REGENERATE_ID_TABLES = {
    "nurse_grade_period",
    "nurse_allowed_shift_period",
    "nurse_weekendoff_period",
    "nurse_preceptee_period",
    # office 102243 전수 마이그에서 실제 충돌(2627) — dev id=2614 를 office 102527 이 점유.
    #   prod 102243 의 id 범위 2238~2643 과 dev 타 office 행이 3건 겹쳤다.
    #   ShiftManage.id 를 FK 로 참조하는 코드 없음(확인) · 스코프는
    #   office_id+group_id+year+month+shift_slot+nurse_class 로 유지되므로 재발번 안전.
    "shift_manage",
    # ★ office 102243 마이그에서 PK 2627 로 **삽입이 통째로 실패**했다(2026-08-21 실측:
    #   fixed_wanted_entries id=15484 · nurse_monthly_limits id=348 를 dev 타 office 가 점유).
    #   dev 행은 이미 지워진 뒤라 그 office 데이터가 **비어버린다.**
    #   `nurse_monthly_limits` 는 CONFLICT_DELETE_KEYS 에 있지만 그 키는
    #   (nurse_id,group_id,year,month) **UNIQUE 기준**이라 surrogate id 충돌은 못 막는다.
    #   두 테이블 모두 id 를 FK 로 참조하는 곳이 없고(확인) 스코프는
    #   nurse_id/group_id+year+month 로 유지되므로 재발번이 안전하다.
    "fixed_wanted_entries",
    "nurse_monthly_limits",
    # 2026-08 신규 — surrogate id 를 가진 3종. 같은 이유로 재발번한다
    # (`wanted_monthly_memo` 는 PK 가 nurse_id+group_id+year+month 복합이라 대상 아님).
    "banned_wanted_entries",
    "nurse_leave_period",
    "nurse_night_cycle",
}

# (table, mode) — FK 부모 → 자식 순서
# mode="wipe":  dev 전체 삭제 후 prod 복사 (마스터 — prod 완전 미러)
# mode="upsert": MERGE (prod row 있으면 UPDATE, 없으면 INSERT, dev-only 보존)
SYNC_TABLES: List[tuple] = [
    # 마스터 (prod 완전 미러) — 정의/설정 성격
    ("offices", "wipe"),
    ("groups", "wipe"),
    ("teams", "wipe"),
    ("nurses", "wipe"),
    ("nurse_team_period", "wipe"),  # 팀 시점 타임라인(근무자 내역) — group_id 스코프
    ("nurse_grade_period", "wipe"),  # grade 시점 SSOT — group_id 스코프
    ("roster_config", "wipe"),
    ("roster_grade_config", "wipe"),
    ("wanted_config", "wipe"),
    ("weekly_off_settings", "wipe"),
    ("shifts", "wipe"),
    ("notices", "wipe"),
    ("sticker", "wipe"),
    ("schedule_holiday", "wipe"),
    # 트랜잭션 — wipe (prod에 PK 제약 없어 MERGE 불가 또는 PK 자체가 없음)
    ("nurse_shift_requests", "wipe"),
    ("nurse_pair_requests", "wipe"),
    ("issued_roster", "wipe"),
    ("shift_preferences", "wipe"),  # PK 없음
    # 트랜잭션 — upsert (PK 매칭 MERGE, dev-only row 보존)
    ("wanted", "upsert"),
    ("wanted_requests", "upsert"),
    ("fixed_wanted_entries", "upsert"),
    ("schedules", "upsert"),
    ("schedule_entries", "upsert"),
    ("daily_shift", "upsert"),
    ("nurse_assignment", "upsert"),
    # nurse 귀속 attribute period SSOT (group_id 없음) — group 모드는 PARENT_FK_MAP
    # (nurse_id→nurses.group_id) 로 wipe-by-parent, office 모드는 _office_where nurse 서브쿼리.
    ("nurse_allowed_shift_period", "upsert"),
    ("nurse_weekendoff_period", "upsert"),
    ("nurse_preceptee_period", "upsert"),
    ("nurse_monthly_limits", "upsert"),
    # ── 2026-08 배포 신규 4종 (기피근무 · 보건휴가/수면OFF · 나이트주기 · 월별메모) ──
    # ★ 이 목록에 없으면 **조용히 복사에서 빠진다**(실측 2026-08-21: office 102243 마이그
    #   직후 dev nurse_night_cycle 이 옛 1350행 그대로, prod 227 과 불일치).
    #   신규 테이블을 만들면 여기에 반드시 등록한다. 컬럼 추가는 INSERT 문을 스키마에서
    #   동적으로 만들므로 별도 등록이 필요 없다.
    ("banned_wanted_entries", "upsert"),
    ("nurse_leave_period", "upsert"),
    ("nurse_night_cycle", "upsert"),
    ("wanted_monthly_memo", "upsert"),
    ("messages", "upsert"),
    ("deleted_nurse_history", "upsert"),
    ("share_links", "upsert"),
    ("shift_manage", "upsert"),
    ("shift_transfer_logs", "upsert"),
    ("roster_jobs", "upsert"),
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


def _table_has_column(db: Session, db_name: str, table: str, column: str) -> bool:
    row = db.execute(
        text(
            f"SELECT 1 FROM {db_name}.INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=:t AND COLUMN_NAME=:c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return bool(row)


def _group_filter_clause(
    include_group_ids: Optional[List[str]] = None,
    exclude_group_ids: Optional[List[str]] = None,
    alias: str = "",
    cols: Optional[List[str]] = None,
) -> tuple:
    """include/exclude group_ids 를 결합해 WHERE 절 + params 반환.

    - cols: 스코프 기준 컬럼(들). 기본 ["group_id"]. 여러 개면 OR 스코프.
      include → (col1 IN (...) OR col2 IN (...))
      exclude → (col1 NOT IN (...) AND col2 NOT IN (...))  # 어느 컬럼도 매칭 안 됨
    - 둘 다 없으면: ("", {}).
    """
    cols = cols or ["group_id"]
    if not include_group_ids and not exclude_group_ids:
        return "", {}
    prefix = f"{alias}." if alias else ""
    conds: List[str] = []
    params: dict = {}
    if include_group_ids:
        ph = ", ".join(f":ig{i}" for i in range(len(include_group_ids)))
        ors = " OR ".join(f"{prefix}{c} IN ({ph})" for c in cols)
        conds.append(f"({ors})")
        for i, g in enumerate(include_group_ids):
            params[f"ig{i}"] = g
    if exclude_group_ids:
        ph = ", ".join(f":xg{i}" for i in range(len(exclude_group_ids)))
        ands = " AND ".join(f"{prefix}{c} NOT IN ({ph})" for c in cols)
        conds.append(f"({ands})")
        for i, g in enumerate(exclude_group_ids):
            params[f"xg{i}"] = g
    where = " WHERE " + " AND ".join(conds)
    return where, params


# office 마이그에서 명시적으로 건너뛸 테이블 (글로벌/로그 — office 개념 없음, dev 보존).
OFFICE_SKIP_TABLES = {"notices", "sticker", "shift_transfer_logs"}


def _office_where(
    db: Session,
    db_name: str,
    table: str,
    office_ids: List[str],
    alias: str = "",
) -> tuple:
    """office_ids 기준 해당 테이블의 WHERE 절 + params 반환 (office 마이그용).

    **컬럼은 db_name(dev/prod) 별로 확인** — prod/dev 스키마 drift 에 견고.
    (예: nurse_shift_requests 등은 dev=office_id, prod=nurse_id 라 양쪽이 다른 컬럼으로
     같은 office 스코프를 잡음.)

    분기 우선순위:
    1. OFFICE_SKIP_TABLES → (None, None) → skip (notices/sticker/shift_transfer_logs)
    2. office_id O → office_id IN (...)
    3. group_id O → group_id IN (SELECT group_id FROM <db>.groups WHERE office_id IN (...))
    4. schedule_entries → schedule_id IN (SELECT ... FROM <db>.schedules WHERE office_id IN (...))
    5. nurse_id O → nurse_id IN (SELECT nurse_id FROM <db>.nurses WHERE office_id IN (...))
       (prod 에 office_id/group_id 가 없는 request 계열 테이블 fallback)
    6. 그 외 → (None, None) → skip
    """
    if table in OFFICE_SKIP_TABLES:
        return None, None
    prefix = f"{alias}." if alias else ""
    ph = ", ".join(f":of{i}" for i in range(len(office_ids)))
    params = {f"of{i}": o for i, o in enumerate(office_ids)}
    if _table_has_column(db, db_name, table, "office_id"):
        return f" WHERE {prefix}office_id IN ({ph})", params
    # 서브쿼리 비교는 COLLATE DATABASE_DEFAULT 로 collation 충돌 방지
    # (같은 DB 내 컬럼 간 collation 이 달라도 안전 — 기존 MERGE 와 동일 정책).
    if _table_has_column(db, db_name, table, "group_id"):
        sub = (
            f"SELECT group_id COLLATE DATABASE_DEFAULT FROM {db_name}.dbo.[groups] "
            f"WHERE office_id IN ({ph})"
        )
        return f" WHERE {prefix}group_id COLLATE DATABASE_DEFAULT IN ({sub})", params
    if table == "schedule_entries":
        sub = (
            f"SELECT schedule_id COLLATE DATABASE_DEFAULT FROM {db_name}.dbo.[schedules] "
            f"WHERE office_id IN ({ph})"
        )
        return f" WHERE {prefix}schedule_id COLLATE DATABASE_DEFAULT IN ({sub})", params
    if _table_has_column(db, db_name, table, "nurse_id"):
        sub = (
            f"SELECT nurse_id COLLATE DATABASE_DEFAULT FROM {db_name}.dbo.[nurses] "
            f"WHERE office_id IN ({ph})"
        )
        return f" WHERE {prefix}nurse_id COLLATE DATABASE_DEFAULT IN ({sub})", params
    return None, None


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


def _delete_dev(
    db: Session,
    table: str,
    include_group_ids: Optional[List[str]] = None,
    exclude_group_ids: Optional[List[str]] = None,
    include_office_ids: Optional[List[str]] = None,
) -> int:
    # office 모드 우선: office 스코프 행만 삭제. 스코프 불가 테이블은 dev 보존(skip).
    if include_office_ids:
        where, params = _office_where(db, DEV_DB, table, include_office_ids)
        if where is None:
            return 0
        result = db.execute(text(f"DELETE FROM {DEV_DB}.dbo.[{table}]{where}"), params)
        return result.rowcount if result.rowcount is not None else -1
    has_grp = _table_has_column(db, DEV_DB, table, "group_id")
    # include_group_ids 지정 시 group_id 컬럼 없는 테이블은 dev 보존 (delete skip).
    if include_group_ids and not has_grp:
        return 0
    where, params = "", {}
    if has_grp and (include_group_ids or exclude_group_ids):
        where, params = _group_filter_clause(include_group_ids, exclude_group_ids)
    result = db.execute(text(f"DELETE FROM {DEV_DB}.dbo.[{table}]{where}"), params)
    return result.rowcount if result.rowcount is not None else -1


def _delete_dev_conflicts(
    db: Session, table: str, keys: List[str], include_group_ids: List[str]
) -> int:
    """include_group_ids 선삭제: 들어올 prod(그 group 스코프) 행과 keys 가 겹치는 dev 행 삭제.
    dev/prod 가 갈려(간호사 타그룹·id 불일치) 생기는 PK/UNIQUE 위반 방지."""
    raw_override = GROUP_COL_OVERRIDE.get(table, "group_id")
    group_cols = raw_override if isinstance(raw_override, list) else [raw_override]
    prod_cols = set(_get_columns(db, PROD_DB, table))
    present = [c for c in group_cols if c in prod_cols]
    if not present:
        return 0
    where, params = _group_filter_clause(
        include_group_ids, None, alias="src", cols=present
    )
    str_types = {"varchar", "nvarchar", "char", "nchar", "text", "ntext"}
    type_rows = db.execute(
        text(
            f"SELECT COLUMN_NAME, DATA_TYPE FROM {DEV_DB}.INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=:t"
        ),
        {"t": table},
    ).fetchall()
    col_types = {r[0]: r[1] for r in type_rows}
    joins = []
    for c in keys:
        if col_types.get(c, "") in str_types:
            joins.append(f"d.[{c}] = src.[{c}] COLLATE DATABASE_DEFAULT")
        else:
            joins.append(f"d.[{c}] = src.[{c}]")
    sql = (
        f"DELETE d FROM {DEV_DB}.dbo.[{table}] AS d "
        f"WHERE EXISTS (SELECT 1 FROM {PROD_DB}.dbo.[{table}] AS src{where} "
        f"AND {' AND '.join(joins)})"
    )
    return db.execute(text(sql), params).rowcount or 0


def _copy_prod_to_dev(
    db: Session,
    table: str,
    include_group_ids: Optional[List[str]] = None,
    exclude_group_ids: Optional[List[str]] = None,
    include_office_ids: Optional[List[str]] = None,
) -> dict:
    """dev 에 prod 내용을 그대로 INSERT. 공통 컬럼만 복사."""
    if not _table_exists(db, PROD_DB, table):
        return {"table": table, "skipped": "prod_missing", "inserted": 0}
    if not _table_exists(db, DEV_DB, table):
        return {"table": table, "skipped": "dev_missing", "inserted": 0}

    dev_cols = _get_columns(db, DEV_DB, table)
    prod_cols = set(_get_columns(db, PROD_DB, table))
    common = [c for c in dev_cols if c in prod_cols]
    if table in REGENERATE_ID_TABLES:  # id 재발번 — prod id 미보존(cross-nurse/group 충돌 회피)
        common = [c for c in common if c.lower() != "id"]
    if not common:
        return {"table": table, "skipped": "no_common_cols", "inserted": 0}

    # 스코프 WHERE 결정 (office 모드 우선)
    if include_office_ids:
        where, params = _office_where(
            db, PROD_DB, table, include_office_ids, alias="src"
        )
        if where is None:
            return {"table": table, "skipped": "no_office_scope", "inserted": 0}
    else:
        # include_group_ids 지정 시 group_id 컬럼 없는 테이블은 dev 보존 (insert skip).
        if include_group_ids and "group_id" not in prod_cols:
            return {"table": table, "skipped": "no_group_id_with_include", "inserted": 0}
        where, params = "", {}
        if "group_id" in prod_cols and (include_group_ids or exclude_group_ids):
            where, params = _group_filter_clause(
                include_group_ids, exclude_group_ids, alias="src"
            )

    # cross-group/cross-id 충돌 dev 행 선삭제 (PK 위반 방지)
    if include_group_ids and table in CONFLICT_DELETE_KEYS:
        _delete_dev_conflicts(db, table, CONFLICT_DELETE_KEYS[table], include_group_ids)

    col_list = ", ".join(f"[{c}]" for c in common)
    sel_list = ", ".join(f"src.[{c}]" for c in common)

    has_identity = _has_identity(db, DEV_DB, table) and table not in REGENERATE_ID_TABLES

    sql = (
        f"INSERT INTO {DEV_DB}.dbo.[{table}] ({col_list}) "
        f"SELECT {sel_list} FROM {PROD_DB}.dbo.[{table}] AS src{where}"
    )

    if has_identity:
        db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.[{table}] ON"))
    try:
        result = db.execute(text(sql), params)
        inserted = result.rowcount if result.rowcount is not None else -1
    finally:
        if has_identity:
            db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.[{table}] OFF"))

    return {"table": table, "inserted": inserted}


def _wipe_by_parent_fk(
    db: Session,
    table: str,
    parent_info: tuple,
    group_ids: List[str],
) -> dict:
    """부모 group_id 로 자식 테이블 DELETE+INSERT (include_group_ids overwrite)."""
    parent_table, fk_col = parent_info
    dev_cols = _get_columns(db, DEV_DB, table)
    prod_cols = set(_get_columns(db, PROD_DB, table))
    common = [c for c in dev_cols if c in prod_cols]
    if table in REGENERATE_ID_TABLES:  # id 재발번 — prod id 미보존(충돌 회피)
        common = [c for c in common if c.lower() != "id"]
    if not common:
        return {"table": table, "skipped": "no_common_cols", "mode": "wipe_by_parent"}
    ph = ", ".join(f":ig{i}" for i in range(len(group_ids)))
    params = {f"ig{i}": g for i, g in enumerate(group_ids)}
    dev_sub = f"SELECT [{fk_col}] FROM {DEV_DB}.dbo.[{parent_table}] WHERE group_id IN ({ph})"
    prod_sub = f"SELECT [{fk_col}] FROM {PROD_DB}.dbo.[{parent_table}] WHERE group_id IN ({ph})"
    del_sql = f"DELETE FROM {DEV_DB}.dbo.[{table}] WHERE [{fk_col}] IN ({dev_sub})"
    deleted = db.execute(text(del_sql), params).rowcount or 0
    col_list = ", ".join(f"[{c}]" for c in common)
    sel_list = ", ".join(f"src.[{c}]" for c in common)
    ins_sql = (
        f"INSERT INTO {DEV_DB}.dbo.[{table}] ({col_list}) "
        f"SELECT {sel_list} FROM {PROD_DB}.dbo.[{table}] AS src "
        f"WHERE src.[{fk_col}] IN ({prod_sub})"
    )
    has_identity = _has_identity(db, DEV_DB, table) and table not in REGENERATE_ID_TABLES
    if has_identity:
        db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.[{table}] ON"))
    try:
        inserted = db.execute(text(ins_sql), params).rowcount or 0
    finally:
        if has_identity:
            db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.[{table}] OFF"))
    return {"table": table, "mode": "wipe_by_parent", "deleted": deleted, "inserted": inserted}


def _full_mirror_regen(
    db: Session,
    table: str,
    include_group_ids: Optional[List[str]] = None,
    exclude_group_ids: Optional[List[str]] = None,
) -> dict:
    """REGENERATE_ID_TABLES 미러: dev DELETE + prod INSERT(id 제외 재발번).

    group_id 없는 leaf satellite → 스코프 수단이 없어 전체 교체(호출부가 PARENT_FK 로 우회).
    ★ 테이블에 group_id 가 있으면 **스코프를 그대로 적용**한다 — include 면 그 group 만,
      exclude 면 그 group 을 건드리지 않는다. 전체 교체는 스코프 밖 dev 데이터를 파괴한다.
    id 를 보존하지 않으므로 IDENTITY_INSERT 불필요(dev auto-gen), cross-dev/prod PK 충돌 없음."""
    dev_cols = _get_columns(db, DEV_DB, table)
    prod_cols = set(_get_columns(db, PROD_DB, table))
    common = [c for c in dev_cols if c in prod_cols and c.lower() != "id"]
    if not common:
        return {"table": table, "skipped": "no_common_cols", "mode": "full_regen"}
    # ★ group 스코프 요청인데 이 테이블에 group_id 가 있으면 **전체 교체를 하지 않는다.**
    #   전체 DELETE 는 문서화된 계약을 깨고 스코프 밖 dev 데이터를 통째로 날린다.
    #     include → 그 group 만 교체(다른 group 보존)
    #     exclude → 그 group 만 건드리지 않음(dev 기존 행 보존)
    #   두 경우 모두 DELETE 와 SELECT 에 같은 조건을 건다.
    scoped = "group_id" in prod_cols and bool(include_group_ids or exclude_group_ids)
    where_dev = where_src = ""
    params: dict = {}
    if scoped:
        where_dev, params = _group_filter_clause(include_group_ids, exclude_group_ids)
        where_src, _ = _group_filter_clause(
            include_group_ids, exclude_group_ids, alias="src"
        )

    deleted = db.execute(
        text(f"DELETE FROM {DEV_DB}.dbo.[{table}]{where_dev}"), params
    ).rowcount or 0
    col_list = ", ".join(f"[{c}]" for c in common)
    sel_list = ", ".join(f"src.[{c}]" for c in common)
    inserted = db.execute(
        text(
            f"INSERT INTO {DEV_DB}.dbo.[{table}] ({col_list}) "
            f"SELECT {sel_list} FROM {PROD_DB}.dbo.[{table}] AS src{where_src}"
        ),
        params,
    ).rowcount or 0
    return {"table": table, "mode": "group_regen" if scoped else "full_regen",
            "deleted": deleted, "inserted": inserted}


def _merge_upsert(
    db: Session,
    table: str,
    include_group_ids: Optional[List[str]] = None,
    exclude_group_ids: Optional[List[str]] = None,
) -> dict:
    """MERGE 로 prod → dev upsert.

    - 기본(전체 sync / exclude 모드): dev-only row 보존 (DELETE 절 없음).
    - include_group_ids 지정 시: 해당 group 스코프의 dev-only(prod 에 없는) 행은
      `WHEN NOT MATCHED BY SOURCE` 로 제거 → 지정 group 을 prod 와 완전 동일하게(잔존 방지).
      다른 group 의 dev 행은 스코프 밖이라 보존.
    """
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

    # REGENERATE_ID_TABLES(leaf satellite): id 재발번이라 id 매칭 MERGE 불가(id 의미 dev/prod 독립).
    #   group 스코프 → PARENT_FK wipe-by-parent, 그 외(full/exclude) → 전체 미러(id 재발번).
    if table in REGENERATE_ID_TABLES:
        parent_info = PARENT_FK_MAP.get(table)
        if include_group_ids and parent_info:
            return _wipe_by_parent_fk(db, table, parent_info, include_group_ids)
        return _full_mirror_regen(db, table, include_group_ids, exclude_group_ids)

    # include_group_ids 지정 시 group_id 컬럼 없는 테이블 처리:
    # - PARENT_FK_MAP 매핑 있으면 부모 group_id 기반 wipe-by-parent 강제 (자식 overwrite)
    # - 매핑 없으면 dev 보존 (skip)
    # group_id 컬럼이 없어도 override 컬럼(들)으로 스코프
    # (예: nurse_assignment → source_group_id OR target_group_id). prod 에 실재하는 컬럼만 사용.
    raw_override = GROUP_COL_OVERRIDE.get(table, "group_id")
    group_cols = raw_override if isinstance(raw_override, list) else [raw_override]
    present_cols = [c for c in group_cols if c in prod_cols]
    if include_group_ids and not present_cols:
        parent_info = PARENT_FK_MAP.get(table)
        if not parent_info:
            return {"table": table, "skipped": "no_group_id_with_include", "upserted": 0}
        return _wipe_by_parent_fk(db, table, parent_info, include_group_ids)

    src_clause = f"{PROD_DB}.dbo.[{table}]"
    params: dict = {}
    if present_cols and (include_group_ids or exclude_group_ids):
        where, params = _group_filter_clause(
            include_group_ids, exclude_group_ids, cols=present_cols
        )
        src_clause = f"(SELECT * FROM {PROD_DB}.dbo.[{table}]{where})"

    # cross-group/cross-id 충돌 dev 행 선삭제 (UNIQUE 위반 방지 — id 매칭 MERGE 한계 보완)
    if include_group_ids and table in CONFLICT_DELETE_KEYS:
        _delete_dev_conflicts(db, table, CONFLICT_DELETE_KEYS[table], include_group_ids)

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

    # include_group_ids 스코프 동기화: prod 에 없는 dev-only(해당 group) 행 제거 →
    # 지정 group 을 prod 와 완전 동일하게(잔존 방지). exclude/전체 sync 는 기존대로 보존.
    delete_clause = ""
    if include_group_ids and present_cols and not exclude_group_ids:
        for i, v in enumerate(include_group_ids):
            params[f"d_inc_{i}"] = v
        _ph = ",".join(f":d_inc_{i}" for i in range(len(include_group_ids)))
        _conds = " OR ".join(f"dst.[{c}] IN ({_ph})" for c in present_cols)
        delete_clause = f"WHEN NOT MATCHED BY SOURCE AND ({_conds}) THEN DELETE"

    sql = f"""
    MERGE {DEV_DB}.dbo.[{table}] AS dst
    USING {src_clause} AS src
        ON {pk_join}
    {update_clause}
    WHEN NOT MATCHED BY TARGET THEN
        INSERT ({insert_cols}) VALUES ({insert_vals})
    {delete_clause};
    """

    has_identity = _has_identity(db, DEV_DB, table)
    if has_identity:
        db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.[{table}] ON"))
    try:
        result = db.execute(text(sql), params)
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


def _copy_inbound_nurses(db: Session, group_ids: List[str]) -> dict:
    """include_group_ids 보강: 대상 group 의 스케줄/배정에 등장하지만 nurses.group_id 가
    그 group 이 아닌 **전입(inbound) 간호사**의 nurses 행을 추가 복사한다.

    group_id=G 스코프만으로는 전입 간호사(홈 병동이 G 아님)의 nurses 마스터 행이 빠져,
    dev 스케줄이 dev.nurses 에 없는 nurse_id 를 참조하게 된다(이름·프로필 미해석). 이를 메운다.
    전입 판정 = G 스케줄의 schedule_entries ∪ nurse_assignment(target/source=G) 에 등장 + group_id≠G.
    dev.nurses 는 FK 없음(CONFLICT_DELETE_KEYS 주석 참조) → nurse_id 선삭제 후 INSERT(refresh) 안전.
    """
    ph = ", ".join(f":g{i}" for i in range(len(group_ids)))
    params = {f"g{i}": g for i, g in enumerate(group_ids)}
    inbound_ids_sql = (
        f"SELECT n.nurse_id FROM {PROD_DB}.dbo.nurses n "
        f"WHERE n.group_id NOT IN ({ph}) AND n.nurse_id IN ("
        f" SELECT e.nurse_id FROM {PROD_DB}.dbo.schedule_entries e "
        f" WHERE e.schedule_id IN (SELECT schedule_id FROM {PROD_DB}.dbo.schedules WHERE group_id IN ({ph})) "
        f" UNION "
        f" SELECT a.nurse_id FROM {PROD_DB}.dbo.nurse_assignment a "
        f" WHERE a.target_group_id IN ({ph}) OR a.source_group_id IN ({ph}))"
    )
    dev_cols = _get_columns(db, DEV_DB, "nurses")
    prod_cols = set(_get_columns(db, PROD_DB, "nurses"))
    common = [c for c in dev_cols if c in prod_cols]
    if not common:
        return {"table": "nurses(inbound)", "skipped": "no_common_cols"}
    # dev 선삭제(nurse_id 매칭) — 이미 있으면 prod 기준 refresh, PK 중복 방지.
    deleted = db.execute(
        text(
            f"DELETE d FROM {DEV_DB}.dbo.nurses d WHERE d.nurse_id COLLATE DATABASE_DEFAULT "
            f"IN (SELECT nurse_id COLLATE DATABASE_DEFAULT FROM ({inbound_ids_sql}) x)"
        ),
        params,
    ).rowcount or 0
    col_list = ", ".join(f"[{c}]" for c in common)
    sel_list = ", ".join(f"src.[{c}]" for c in common)
    ins_sql = (
        f"INSERT INTO {DEV_DB}.dbo.nurses ({col_list}) "
        f"SELECT {sel_list} FROM {PROD_DB}.dbo.nurses src WHERE src.nurse_id IN ({inbound_ids_sql})"
    )
    has_identity = _has_identity(db, DEV_DB, "nurses")
    if has_identity:
        db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.nurses ON"))
    try:
        inserted = db.execute(text(ins_sql), params).rowcount or 0
    finally:
        if has_identity:
            db.execute(text(f"SET IDENTITY_INSERT {DEV_DB}.dbo.nurses OFF"))
    return {"table": "nurses(inbound)", "mode": "inbound", "deleted": deleted, "inserted": inserted}


def sync_prod_to_dev(
    db: Session = None,
    tables: Optional[List[str]] = None,
    include_group_ids: Optional[List[str]] = None,
    exclude_group_ids: Optional[List[str]] = None,
    include_office_ids: Optional[List[str]] = None,
) -> dict:
    """마스터=wipe+copy / 트랜잭션=upsert(MERGE).

    각 단계를 **테이블별 독립 세션**으로 실행하여 connection drop 에 강건.
    db 인자는 호환용으로 받지만 무시.

    Args:
        tables: 특정 테이블만 sync (None 이면 SYNC_TABLES 전체).
                각 테이블의 mode 는 SYNC_TABLES 에서 lookup.
        include_group_ids: group_id 컬럼이 있는 테이블에 한해 해당 group_id 행만
                prod 에서 가져오고 dev 의 해당 group_id 행만 wipe 대상.
                group_id 컬럼이 없는 테이블은 **건드리지 않음 (dev 보존)**.
                다른 group 의 dev 데이터는 그대로 유지.
        exclude_group_ids: group_id 컬럼이 있는 테이블에 한해 해당 group_id 행을
                prod 에서 가져오지 않고 dev wipe 대상에서도 제외 (dev 기존 데이터 보존).
                group_id 컬럼이 없는 테이블은 영향 없음 (full wipe+copy).
    """
    # office 모드: group 필터는 무시(우선순위). 완전 교체이므로 모든 대상 테이블을
    # scoped wipe(office 스코프 삭제 + prod 복사)로 처리. office 스코프 불가 테이블
    # (notices/sticker/shift_transfer_logs 등)은 _office_where=None → 자동 skip(보존).
    if include_office_ids:
        include_group_ids = None
        exclude_group_ids = None

    # 모드 lookup
    mode_map = {t: m for t, m in SYNC_TABLES}
    if include_office_ids:
        base = tables or [t for t, _ in SYNC_TABLES]
        target_pairs = [(t, "wipe") for t in base]
    elif tables:
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
    #    - exclude_group_ids 행은 dev 보존
    #    - include_group_ids 지정 시 해당 group_id 행만 DELETE
    deleted_summary: dict = {}
    for table in reversed(wipe_tables):
        cnt, err = _run_in_session(
            lambda s, t=table: _delete_dev(
                s, t, include_group_ids, exclude_group_ids, include_office_ids
            )
        )
        if err:
            errors.append({"phase": "delete", "table": table, "error": err})
            logger.error("[prod→dev sync] DELETE 실패 %s: %s", table, err)
        else:
            deleted_summary[table] = cnt

    # 3. 순방향 처리 — wipe=INSERT, upsert=MERGE
    for table, mode in target_existing:
        if mode == "wipe":
            r, err = _run_in_session(
                lambda s, t=table: _copy_prod_to_dev(
                    s, t, include_group_ids, exclude_group_ids, include_office_ids
                )
            )
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
            r, err = _run_in_session(
                lambda s, t=table: _merge_upsert(
                    s, t, include_group_ids, exclude_group_ids
                )
            )
            if err:
                errors.append({"phase": "upsert", "table": table, "error": err})
                logger.error("[prod→dev sync] UPSERT 실패 %s: %s", table, err)
                results.append({"table": table, "mode": "upsert", "error": err, "upserted": 0})
            else:
                # PARENT_FK_MAP fallback 시 _merge_upsert 가 mode="wipe_by_parent" 반환 → 보존
                r.setdefault("mode", "upsert")
                results.append(r)

    # 3.5 전입(inbound) 간호사 nurses 보강 — include_group_ids 모드 + nurses 동기화 대상일 때만.
    #     G 스코프로는 홈 병동이 다른 전입 간호사의 nurses 행이 빠져 dev 스케줄이 미존재 nurse 참조.
    if include_group_ids and not include_office_ids and "nurses" in {t for t, _ in target_existing}:
        r, err = _run_in_session(lambda s: _copy_inbound_nurses(s, include_group_ids))
        if err:
            errors.append({"phase": "inbound_nurses", "error": err})
            logger.error("[prod→dev sync] inbound nurses 실패: %s", err)
        elif r:
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
