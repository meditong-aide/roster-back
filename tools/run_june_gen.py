"""Run a one-shot generate_roster_service for 2026-06 and capture full stdout/stderr.

Used to inspect KLD distribution logs without disturbing the live uvicorn server.
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

from jose import jwt

TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJvZmZpY2VfaWQiOiIxMDEzNTgiLCJFbXBTZXFObyI6IjQ0NTg3MiIsImFjY291bnRfaWQiOiJhaV9uMDAwMyIsIkVtcEF1dGhHYm4iOiJNRU0iLCJpc19tYXN0ZXJfYWRtaW4iOmZhbHNlLCJudXJzZV9pZCI6IjQ0NTg3MiIsImdyb3VwX2lkIjoiMTAxMzU4NTdmOWY5IiwiaXNfaGVhZF9udXJzZSI6dHJ1ZSwibmFtZSI6Ilx1YzgwNFx1YjNjNFx1YzVmMCIsIm1iX3BhcnQiOiJCMDA0X00wMDNfUzAxMSIsIm9mZmljZV9uYW1lIjoiXHVjNzU4XHViOGNjXHViYzk1XHVjNzc4IFx1YjBhOFx1Y2QwY1x1Yzc1OFx1YjhjY1x1YzdhY1x1YjJlOCBcdWMyZGNcdWQ2NTRcdWJjZDFcdWM2ZDAiLCJtYl9wYXJ0X25hbWUiOiJcdWM5MTFcdWQ2NThcdWM3OTBcdWMyZTQxIiwiZ3dfdXNlWU4iOiJZIiwicXBpc191c2VZTiI6IlkiLCJvZmZpY2lhbF90aXRsZV9uYW1lIjoiXHVkMzAwXHVjNmQwIiwiaG5fYXV0aCI6IkhOIiwib3JpZ2luYWxfZ3JvdXBfaWQiOiIxMDEzNThkZGYwN2IiLCJleHAiOjE3NzkzMjcwOTB9."
    "OEBRnJHaDoEb7s_MY1EmO4G9LL1rY_uRGcCDfUO8Dtg"
)


def build_user():
    secret = os.getenv("SECRET_KEY")
    if not secret:
        raise SystemExit("SECRET_KEY env var missing.")
    payload = jwt.decode(TOKEN, secret, algorithms=["HS256"], options={"verify_exp": False})
    from schemas.auth_schema import User  # type: ignore
    return User(
        nurse_id=str(payload["nurse_id"]),
        account_id=str(payload["account_id"]),
        office_id=str(payload["office_id"]),
        group_id=str(payload["group_id"]),
        is_head_nurse=bool(payload.get("is_head_nurse", False)),
        is_master_admin=bool(payload.get("is_master_admin", False)),
        name=str(payload.get("name", "")),
        EmpSeqNo=str(payload.get("EmpSeqNo") or ""),
        EmpAuthGbn=str(payload.get("EmpAuthGbn") or "MEM"),
        mb_part=str(payload.get("mb_part", "")),
        office_name=str(payload.get("office_name", "")),
        mb_part_name=str(payload.get("mb_part_name", "")),
        gw_useYN=str(payload.get("gw_useYN", "Y")),
        qpis_useYN=str(payload.get("qpis_useYN", "Y")),
        official_title_name=payload.get("official_title_name"),
        hn_auth=payload.get("hn_auth"),
        original_group_id=payload.get("original_group_id"),
    )


def main() -> int:
    tag = os.getenv("RUN_TAG", "k3relax")
    log_path = ROOT / "artifacts" / "debug_june" / f"june_{tag}.log"
    json_path = ROOT / "artifacts" / "debug_june" / f"june_{tag}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Tee stdout/stderr to file
    log_fp = open(log_path, "w", buffering=1, encoding="utf-8")

    class Tee:
        def __init__(self, *streams):
            self.streams = streams
        def write(self, data):
            for s in self.streams:
                try:
                    s.write(data)
                except Exception:
                    pass
        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass

    sys.stdout = Tee(sys.__stdout__, log_fp)
    sys.stderr = Tee(sys.__stderr__, log_fp)

    print(f"[run_june_gen] start; log -> {log_path}")

    user = build_user()
    print(f"[run_june_gen] user nurse_id={user.nurse_id} group_id={user.group_id} is_head_nurse={user.is_head_nurse}")

    from db.client2 import SessionLocal  # type: ignore
    from schemas.roster_schema import RosterRequest  # type: ignore
    from services.roster_create_service import generate_roster_service  # type: ignore

    # cfg.off_cap_relax_extra default 값 확인 (TEMP=3, 배포 0)
    from db import roster_config as _rc  # type: ignore
    _tmp_cfg = _rc.NurseRosterConfig()
    print(f"[run_june_gen] off_cap_relax_extra (cfg default)={_tmp_cfg.off_cap_relax_extra}")

    # 시화병원 RosterConfig.off_swap_enabled 사전 조회
    try:
        from db.client2 import SessionLocal as _SL  # type: ignore
        from db.models import RosterConfig as _RC  # type: ignore
        _db = _SL()
        try:
            _rc_row = _db.query(_RC).filter(_RC.group_id == user.group_id).first()
            print(
                f"[run_june_gen] DB RosterConfig.off_swap_enabled="
                f"{getattr(_rc_row, 'off_swap_enabled', None) if _rc_row else 'NO_ROW'}"
            )
        finally:
            _db.close()
    except Exception as exc:
        print(f"[run_june_gen] off_swap_enabled probe failed: {exc}")

    req = RosterRequest(year=2026, month=6, grade_strategy="COMBINED")

    db = SessionLocal()
    try:
        result = generate_roster_service(req, user, db)
    finally:
        try:
            db.close()
        except Exception:
            pass

    print("[run_june_gen] generate_roster_service returned")
    # Persist response (best-effort)
    try:
        with open(json_path, "w", encoding="utf-8") as fp:
            json.dump(result, fp, ensure_ascii=False, default=str, indent=2)
        print(f"[run_june_gen] result saved -> {json_path}")
    except Exception as exc:
        print(f"[run_june_gen] failed to save result json: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
