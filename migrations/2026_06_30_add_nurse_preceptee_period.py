"""Migration 2026-06-30 — nurse_preceptee_period (프리셉터↔프리셉티 시점 SSOT).

신규 1 테이블: nurse_preceptee_period
설계: docs/NURSE_PRECEPTEE_PERIOD_DESIGN.md

실행:
  cd roster-back
  PYTHONPATH=app python migrations/2026_06_30_add_nurse_preceptee_period.py
  PYTHONPATH=app python migrations/2026_06_30_add_nurse_preceptee_period.py --dry-run
  PYTHONPATH=app python migrations/2026_06_30_add_nurse_preceptee_period.py --no-backfill
  PYTHONPATH=app python migrations/2026_06_30_add_nurse_preceptee_period.py --rollback

운영(MSSQL) 적용:
  1. DB 백업(필수).
  2. 본 스크립트 실행(멱등성: 이미 있으면 skip) 또는 동명 .sql 직접 실행.
  3. 백필: 기존 active 프리셉티 assignment + nurses.preceptor_id → period row.
     cache-only 비대칭은 생성 안 하고 로그만(잘못된 이력 방지).
  4. 검증: 테이블 존재 + 백필 건수 확인.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from db.client2 import engine as default_engine  # noqa: E402
from db.models import Base, NursePrecepteePeriod  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

NEW_TABLES = [NursePrecepteePeriod.__table__]


def _create_missing_tables(engine, *, dry_run: bool) -> list[str]:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    to_create = [t for t in NEW_TABLES if t.name not in existing]
    created = [t.name for t in to_create]
    if not to_create:
        logger.info("[create] target table already exists — skip")
        return []
    logger.info("[create] target tables: %s", created)
    if dry_run:
        logger.info("[create] DRY RUN — no CREATE executed")
        return created
    Base.metadata.create_all(bind=engine, tables=to_create)
    logger.info("[create] DONE created=%s", created)
    return created


def _backfill(engine, *, dry_run: bool) -> dict:
    from services.preceptee_period import backfill_from_assignments
    with Session(engine) as s:
        res = backfill_from_assignments(s)
        logger.info(
            "[backfill] inserted=%s skipped=%s cache_orphans=%s",
            res["inserted"], res["skipped"], len(res["cache_orphans"]),
        )
        if res["cache_orphans"]:
            logger.info("[backfill] cache-only 비대칭(미생성, 수동검토): %s", res["cache_orphans"])
        if dry_run:
            logger.info("[backfill] DRY RUN — rollback")
            s.rollback()
        else:
            s.commit()
        return res


def _rollback(engine, *, dry_run: bool) -> list[str]:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    present = [t for t in NEW_TABLES if t.name in existing]
    if not present:
        logger.info("[rollback] 대상 테이블 없음 — skip")
        return []
    with engine.begin() as conn:
        for t in present:
            cnt = conn.execute(text(f"SELECT COUNT(*) FROM {t.name}")).scalar() or 0
            if cnt > 0:
                raise RuntimeError(
                    f"[rollback] {t.name} 에 {cnt} row 존재 — DROP 거부. 먼저 데이터 정리."
                )
    dropped = [t.name for t in present]
    if dry_run:
        logger.info("[rollback] DRY RUN — no DROP executed: %s", dropped)
        return dropped
    Base.metadata.drop_all(bind=engine, tables=present)
    logger.info("[rollback] DONE dropped=%s", dropped)
    return dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backfill", action="store_true", help="테이블만 생성, 백필 생략")
    parser.add_argument("--rollback", action="store_true", help="테이블 DROP(빈 테이블 한정)")
    args = parser.parse_args()

    engine = default_engine
    logger.info("[migration] dialect=%s", engine.dialect.name)
    if args.rollback:
        _rollback(engine, dry_run=args.dry_run)
    else:
        _create_missing_tables(engine, dry_run=args.dry_run)
        if not args.no_backfill:
            _backfill(engine, dry_run=args.dry_run)
    logger.info("[migration] 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
