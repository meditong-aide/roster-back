"""Migration 2026-07-20 — roster_config.max_conseq_off 컬럼 추가.

신규 컬럼(roster_config):
  max_conseq_off  INT  NULL  — 연속 OFF 최대 개수(soft 상한).
    NULL/미설정 = 앱 기본 3 적용(=4연속+ OFF 만 벌점, 3 이하는 무차별).
    (k+1)연속 OFF 마다 고weight 벌점 → hard처럼 억제하되, OFF 과잉/하드 제약 충돌 시
    양보(soft라 절대 infeasible 유발 안 함). 솔버 dataclass 동명 필드로 매핑.
    ※ enforce_4o_hard(window=4 고정, 진짜 hard, 월경계 포함)와는 별개 메커니즘.

실행:
  cd roster-back
  PYTHONPATH=app python migrations/2026_07_20_add_roster_config_max_conseq_off.py
  PYTHONPATH=app python migrations/2026_07_20_add_roster_config_max_conseq_off.py --dry-run
  PYTHONPATH=app python migrations/2026_07_20_add_roster_config_max_conseq_off.py --rollback

운영 (MSSQL) 적용 절차:
  1. DB 백업.
  2. 본 스크립트 실행(멱등: 이미 있으면 skip). 또는 동봉 .sql 직접 실행.
  3. 검증: roster_config.max_conseq_off 컬럼 존재.

멱등성:
  - 이미 존재하는 컬럼은 skip. 데이터 손실 없음.
  - 기존 row 는 NULL 로 남는다(백필 없음). 읽을 때 앱이 NULL→3 로 해석하므로 무회귀.
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
    ("max_conseq_off", "INT", "INTEGER"),
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
            parts = [f"{name} {mssql_type} NULL" for name, mssql_type, _sqlite in to_add]
            conn.execute(text(f"ALTER TABLE {TABLE} ADD " + ", ".join(parts)))
        else:
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

    have = _existing_columns(engine)

    logger.warning("[rollback] DROP cols=%s", [c[0] for c in NEW_COLUMNS if c[0] in have])
    if dry_run:
        logger.info("[rollback] DRY RUN — 미실행")
        return

    with engine.begin() as conn:
        for name, *_ in NEW_COLUMNS:
            if name in have:
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
    logger.info("migration done")


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="roster_config.max_conseq_off 마이그레이션 (2026-07-20)"
    )
    parser.add_argument("--dry-run", action="store_true", help="실행하지 않고 계획만 출력")
    parser.add_argument("--rollback", action="store_true", help="컬럼 DROP (데이터 손실)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_migration(dry_run=args.dry_run, rollback=args.rollback)


if __name__ == "__main__":
    _cli()
