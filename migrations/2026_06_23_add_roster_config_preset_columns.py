"""Migration 2026-06-23 — roster_config 설정 프리셋 컬럼 추가.

신규 컬럼(roster_config):
  version       INT            NULL   — 그룹(office+group)별 0부터 시작하는 프리셋 버전.
                                        legacy row 는 NULL(=프리셋 아님, 목록 비노출).
  config_name   NVARCHAR(100)  NULL   — 프리셋 이름('새로운 설정n' 자동분 포함).
  config_memo   NVARCHAR(500)  NULL   — 간단 메모.

인덱스:
  ux_roster_config_group_version (version 유일성) 는 개발 마무리 후 별도 추가 예정 — 지금은 컬럼만.
    추가 시 일반 UNIQUE 가 아닌 WHERE version IS NOT NULL 필터드 유니크로 넣을 것
    (MSSQL 은 복합 UNIQUE 에서 NULL 을 동일값으로 보아 legacy NULL 다수가 충돌하므로).

실행:
  cd roster-back
  PYTHONPATH=app python migrations/2026_06_23_add_roster_config_preset_columns.py
  PYTHONPATH=app python migrations/2026_06_23_add_roster_config_preset_columns.py --dry-run
  PYTHONPATH=app python migrations/2026_06_23_add_roster_config_preset_columns.py --rollback

운영 (MSSQL) 적용 절차:
  1. DB 백업.
  2. 본 스크립트 실행(멱등: 이미 있으면 skip). 또는 동봉 .sql 직접 실행.
  3. 검증: 컬럼 3개 존재 + 인덱스 ux_roster_config_group_version 존재.

멱등성:
  - 이미 존재하는 컬럼/인덱스는 skip. 데이터 손실 없음.
  - 기존 row 는 version=NULL 로 남는다(백필 없음 — legacy = 프리셋 아님).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from sqlalchemy import inspect, text  # noqa: E402

from db.client2 import engine as default_engine  # noqa: E402

logger = logging.getLogger(__name__)

TABLE = "roster_config"

# (컬럼명, MSSQL 타입, SQLite 타입)
NEW_COLUMNS = [
    ("version", "INT", "INTEGER"),
    ("config_name", "NVARCHAR(100)", "TEXT"),
    ("config_memo", "NVARCHAR(500)", "TEXT"),
    ("updated_at", "DATETIME", "DATETIME"),  # 마지막 저장 시각(upsert 시 앱이 갱신)
]


def _existing_columns(engine) -> set[str]:
    inspector = inspect(engine)
    if TABLE not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(TABLE)}


def _add_columns(engine, *, dry_run: bool) -> list[str]:
    """없는 컬럼만 ADD. 반환: 추가된 컬럼 목록."""
    inspector = inspect(engine)
    if TABLE not in set(inspector.get_table_names()):
        logger.warning("[add] %s 테이블이 없음 — skip", TABLE)
        return []

    have = _existing_columns(engine)
    dialect = engine.dialect.name
    to_add = [c for c in NEW_COLUMNS if c[0] not in have]
    added = [c[0] for c in to_add]

    if not to_add:
        logger.info("[add] 대상 컬럼 모두 존재 — skip")
        return []

    logger.info("[add] dialect=%s 추가 대상=%s", dialect, added)
    if dry_run:
        logger.info("[add] DRY RUN — ALTER 미실행")
        return added

    with engine.begin() as conn:
        if dialect == "mssql":
            # 한 문장으로 다중 ADD (NULL 허용이라 기존 row 안전)
            parts = [f"{name} {mssql_type} NULL" for name, mssql_type, _sqlite in to_add]
            conn.execute(text(f"ALTER TABLE {TABLE} ADD " + ", ".join(parts)))
        else:
            # SQLite/기타: 컬럼 1개씩 ADD
            for name, _mssql, col_type in to_add:
                conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {col_type}"))

    logger.info("[add] DONE added=%s", added)
    return added


def _rollback(engine, *, dry_run: bool) -> None:
    """컬럼 DROP. 데이터 손실 — 명시적 --rollback 필요."""
    inspector = inspect(engine)
    if TABLE not in set(inspector.get_table_names()):
        logger.info("[rollback] %s 없음 — nothing to do", TABLE)
        return

    dialect = engine.dialect.name
    have = _existing_columns(engine)

    logger.warning("[rollback] DROP cols=%s", [c[0] for c in NEW_COLUMNS if c[0] in have])
    if dry_run:
        logger.info("[rollback] DRY RUN — 미실행")
        return

    with engine.begin() as conn:
        for name, *_ in NEW_COLUMNS:
            if name in have:
                if dialect == "mssql":
                    conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {name}"))
                else:
                    # SQLite 3.35+ DROP COLUMN 지원
                    conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {name}"))
    logger.info("[rollback] DONE")


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

    _add_columns(engine, dry_run=dry_run)
    # NOTE: version 유일성 필터드 인덱스(ux_roster_config_group_version)는 개발 마무리 후
    #   별도로 추가 예정. 지금은 컬럼만 추가한다.
    logger.info("migration done")


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="roster_config 프리셋 컬럼 마이그레이션 (2026-06-23)"
    )
    parser.add_argument("--dry-run", action="store_true", help="실행하지 않고 계획만 출력")
    parser.add_argument("--rollback", action="store_true", help="컬럼/인덱스 DROP (데이터 손실)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_migration(dry_run=args.dry_run, rollback=args.rollback)


if __name__ == "__main__":
    _cli()
