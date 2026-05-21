"""시화병원 RosterGradeConfig.allow_soft_fallback을 토글.

기본 동작: 현재 값을 출력만(probe). --apply 옵션 주면 1로 UPDATE.
복구하려면 --revert (=0).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))

import dotenv

dotenv.load_dotenv(ROOT / ".env")
dotenv.load_dotenv(APP / ".env")

GROUP_ID = os.getenv("GROUP_ID", "10135857f9f9")  # 시화병원


def main() -> int:
    args = sys.argv[1:]
    apply_set = "--apply" in args
    revert = "--revert" in args

    from db.client2 import SessionLocal  # type: ignore
    from db.models import RosterGradeConfig  # type: ignore

    db = SessionLocal()
    try:
        row = db.query(RosterGradeConfig).filter(
            RosterGradeConfig.group_id == GROUP_ID
        ).first()
        if row is None:
            print(f"[update_grade_soft] NO ROW for group_id={GROUP_ID}")
            return 1
        print(
            f"[update_grade_soft] group_id={GROUP_ID} config_id={row.config_id} "
            f"office_id={row.office_id} allow_soft_fallback(current)={row.allow_soft_fallback}"
        )
        if not (apply_set or revert):
            print("[update_grade_soft] no --apply / --revert flag — probe only.")
            return 0
        new_val = 0 if revert else 1
        old_val = int(row.allow_soft_fallback or 0)
        if old_val == new_val:
            print(f"[update_grade_soft] already at {new_val}; nothing to do.")
            return 0
        row.allow_soft_fallback = new_val
        db.commit()
        # 재조회 검증
        re = db.query(RosterGradeConfig).filter(
            RosterGradeConfig.group_id == GROUP_ID
        ).first()
        print(
            f"[update_grade_soft] UPDATED: {old_val} -> {re.allow_soft_fallback} "
            f"(revert={revert})"
        )
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
