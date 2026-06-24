"""Migration 2026-06-22 — 간호사 속성 시점(effective-dated) 테이블 4종.

신규 4 테이블:
  nurse_grade_period, nurse_allowed_shift_period,
  nurse_weekendoff_period, nurse_fixedshift_period

설계: docs/NURSE_ATTRIBUTE_PERIOD_DESIGN.md (P0).

실행:
  cd roster-back
  PYTHONPATH=app python migrations/2026_06_22_add_nurse_attribute_period_tables.py
  PYTHONPATH=app python migrations/2026_06_22_add_nurse_attribute_period_tables.py --dry-run
  PYTHONPATH=app python migrations/2026_06_22_add_nurse_attribute_period_tables.py --rollback

운영(MSSQL) 적용:
  1. DB 백업(필수).
  2. 본 스크립트 실행(멱등성: 이미 있으면 skip). 또는 동명 .sql 직접 실행.
  3. 검증: 4 테이블 존재 확인.

멱등성:
  - 이미 존재하는 테이블은 CREATE skip. 데이터 손실 없음.
  - --rollback 은 빈 테이블 전제(데이터 있으면 거부) — 실수 방지.
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
    NurseGradePeriod,
    NurseAllowedShiftPeriod,
    NurseWeekendOffPeriod,
)
# NOTE: NurseFixedShiftPeriod 는 폐기됨 — fixed_shift 는 nurse_allowed_shift_period 컬럼으로 통합.

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


NEW_TABLES = [
    NurseGradePeriod.__table__,
    NurseAllowedShiftPeriod.__table__,
    NurseWeekendOffPeriod.__table__,
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


def _rollback(engine, *, dry_run: bool) -> list[str]:
    """4 테이블 DROP. 데이터가 있으면 거부(실수 방지)."""
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
    logger.info("[rollback] DROP 대상: %s", dropped)
    if dry_run:
        logger.info("[rollback] DRY RUN — no DROP executed")
        return dropped
    Base.metadata.drop_all(bind=engine, tables=present)
    logger.info("[rollback] DONE dropped=%s", dropped)
    return dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="실제 변경 없이 계획만 출력")
    parser.add_argument("--rollback", action="store_true", help="4 테이블 DROP(빈 테이블 한정)")
    args = parser.parse_args()

    engine = default_engine
    logger.info("[migration] dialect=%s", engine.dialect.name)
    if args.rollback:
        _rollback(engine, dry_run=args.dry_run)
    else:
        _create_missing_tables(engine, dry_run=args.dry_run)
    logger.info("[migration] 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
