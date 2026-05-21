"""시화병원 RosterGradeConfig.constraints_max_json 토글.

기본 동작: 현재 값 출력만(probe).
--apply: grade 1·4의 D/E/N 상한을 1로 설정 (잉여 차단).
--revert: 빈 dict로 원복.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))

import dotenv

dotenv.load_dotenv(ROOT / ".env")
dotenv.load_dotenv(APP / ".env")

GROUP_ID = os.getenv("GROUP_ID", "10135857f9f9")

CAP_MAX = {
    "D": {"1": 1, "4": 1},
    "E": {"1": 1, "4": 1},
    "N": {"1": 1, "4": 1},
}


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
            print(f"[update_grade_max] NO ROW for group_id={GROUP_ID}")
            return 1
        current = row.constraints_max_json or {}
        print(
            f"[update_grade_max] group_id={GROUP_ID} config_id={row.config_id} "
            f"allow_soft_fallback={row.allow_soft_fallback} "
            f"constraints_max_json(current)={json.dumps(current, ensure_ascii=False)}"
        )
        if not (apply_set or revert):
            print("[update_grade_max] no --apply / --revert flag — probe only.")
            return 0
        new_val = {} if revert else CAP_MAX
        row.constraints_max_json = new_val
        db.commit()
        re = db.query(RosterGradeConfig).filter(
            RosterGradeConfig.group_id == GROUP_ID
        ).first()
        print(
            f"[update_grade_max] UPDATED -> {json.dumps(re.constraints_max_json, ensure_ascii=False)} "
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
