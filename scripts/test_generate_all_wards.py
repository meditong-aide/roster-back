#!/usr/bin/env python
"""전 병동 근무표 생성 테스트 (in-process).

★ 알림 없음 — 3중 차단
   ① `push_enabled()` 가 ENVIRONMENT=production 을 요구한다(로컬은 development).
   ② 파견/병동이동 assignment 가 있어야 S09 알림이 나가는데 office 102243 은 0건.
   ③ 그래도 안전하게 `utils.utils` 의 send_*push / set_app_push 를 전부 no-op 으로 갈아끼운다.
      (roster_create_service 가 함수 안에서 지연 import 하므로 모듈 속성 교체가 유효하다)

생성 결과는 schedules 에 draft 로 남는다. 정리는 --cleanup 으로.

사용 예
    cd roster-back
    EUN_DB_NAME=eun_roster .venv/bin/python scripts/test_generate_all_wards.py --year 2026 --month 9
    EUN_DB_NAME=eun_roster .venv/bin/python scripts/test_generate_all_wards.py --year 2026 --month 9 --cleanup
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from collections import Counter
from datetime import date
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(_APP))

# ── 푸시 전면 차단 (import 보다 먼저) ─────────────────────────────────────────
import utils.utils as _U  # noqa: E402


def _noop(*_a, **_kw):
    return {"result": "skip", "message": "push disabled by test harness"}


_PATCHED = []
for _name in dir(_U):
    if _name.startswith("send_") or _name in ("set_app_push",):
        if callable(getattr(_U, _name, None)):
            setattr(_U, _name, _noop)
            _PATCHED.append(_name)
_U.push_enabled = lambda: False

from sqlalchemy import text  # noqa: E402

from db.client2 import SessionLocal  # noqa: E402
from schemas.auth_schema import User  # noqa: E402
from schemas.roster_schema import RosterRequest  # noqa: E402
from services.roster_create_service import generate_roster_service  # noqa: E402

WARDS = [
    ("1022438ea001", "41-RN"), ("102243d0df40", "41-AN"), ("1022432916e6", "별관1"),
    ("102243131010", "51-RN"), ("102243861f62", "51-AN"), ("102243e39fbf", "42-RN"),
    ("102243643176", "응급실-RN"), ("102243766d82", "응급실-AN"), ("102243e9f69a", "중환자실"),
    ("102243e8f6ba", "호스피스"), ("102243ebe67c", "52-RN"), ("1022438722db", "52-AN"),
]


def build_user(db, group_id: str) -> User | None:
    """그 그룹의 수간호사(is_head_nurse)로 호출자를 만든다. 없으면 None."""
    row = db.execute(text("""
        SELECT TOP 1 n.nurse_id, n.account_id, n.office_id, n.name
        FROM nurses n WHERE n.group_id=:g AND n.is_head_nurse=1
        ORDER BY n.active DESC, n.sequence
    """), {"g": group_id}).first()
    if not row:
        # 그룹 소속 HN 이 없으면 hn_id 에 등록된 관리자로 대체(41-AN 등 RN 파트장 겸임 구조)
        import json
        hn = db.execute(text("SELECT hn_id FROM groups WHERE group_id=:g"), {"g": group_id}).scalar()
        for nid in json.loads(hn or "[]"):
            row = db.execute(text("""
                SELECT TOP 1 nurse_id, account_id, office_id, name FROM nurses
                WHERE nurse_id=:i AND is_head_nurse=1
            """), {"i": nid}).first()
            if row:
                break
    if not row:
        return None
    return User(
        nurse_id=str(row[0]), account_id=str(row[1] or ""), office_id=str(row[2]),
        group_id=group_id, is_head_nurse=True, is_master_admin=False, name=str(row[3]),
        mb_part="", office_name="", mb_part_name="", gw_useYN="Y", qpis_useYN="Y",
        official_title_name=None, hn_auth="HN",
    )


def violations(db, schedule_id: str, group_id: str) -> dict:
    """하드락 정책 위반을 센다(연속근무 5·N연속 3·ND/ED/NE 패턴·1N)."""
    rows = db.execute(text("""
        SELECT e.nurse_id, e.work_date, e.shift_id FROM schedule_entries e
        WHERE e.schedule_id=:s ORDER BY e.nurse_id, e.work_date
    """), {"s": schedule_id}).fetchall()
    by = {}
    for nid, wd, sid in rows:
        d = wd.date() if hasattr(wd, "date") else wd
        by.setdefault(str(nid), {})[d.day] = sid
    WORK = {"D", "E", "N", "M", "DE"}
    v = Counter()
    for nid, days in by.items():
        seq = [days.get(d, "") for d in range(1, max(days) + 1)]
        run_w = run_n = 0
        for i, c in enumerate(seq):
            run_w = run_w + 1 if c in WORK else 0
            run_n = run_n + 1 if c == "N" else 0
            if run_w > 5:
                v["연속근무6일+"] += 1
            if run_n > 3:
                v["N4연속+"] += 1
            if i > 0:
                prev = seq[i - 1]
                if prev == "N" and c == "D":
                    v["ND"] += 1
                if prev == "E" and c == "D":
                    v["ED"] += 1
                if prev == "N" and c == "E":
                    v["NE"] += 1
        # 단독 1N
        for i, c in enumerate(seq):
            if c == "N" and (i == 0 or seq[i - 1] != "N") and (i + 1 >= len(seq) or seq[i + 1] != "N"):
                v["1N단독"] += 1
    return dict(v)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--month", type=int, default=9)
    ap.add_argument("--only", default=None, help="특정 그룹만 (라벨 일부 일치)")
    ap.add_argument("--cleanup", action="store_true", help="생성분(draft) 삭제만 하고 종료")
    args = ap.parse_args()

    print("=" * 78)
    print(f"대상 DB : {os.getenv('EUN_DB_NAME', '(미설정)')}")
    print(f"연월    : {args.year}-{args.month:02d}")
    print(f"푸시    : 전면 차단 ({len(_PATCHED)}개 함수 no-op · push_enabled=False)")
    print("=" * 78)

    targets = [(g, l) for g, l in WARDS if not args.only or args.only in l]

    if args.cleanup:
        db = SessionLocal()
        for gid, lbl in targets:
            ids = [r[0] for r in db.execute(text("""
                SELECT schedule_id FROM schedules
                WHERE group_id=:g AND year=:y AND month=:m AND status='draft'
            """), {"g": gid, "y": args.year, "m": args.month})]
            for sid in ids:
                db.execute(text("DELETE FROM issued_roster_snapshot WHERE schedule_id=:s"), {"s": sid})
                db.execute(text("DELETE FROM issued_roster WHERE schedule_id=:s"), {"s": sid})
                db.execute(text("DELETE FROM schedule_entries WHERE schedule_id=:s"), {"s": sid})
                db.execute(text("DELETE FROM schedules WHERE schedule_id=:s"), {"s": sid})
            db.commit()
            print(f"  {lbl:<11} draft {len(ids)}건 삭제")
        db.close()
        return

    results = []
    for gid, lbl in targets:
        db = SessionLocal()
        t0 = time.time()
        try:
            user = build_user(db, gid)
            if user is None:
                print(f"  {lbl:<11} ✗ 수간호사(호출자)를 찾지 못함 — 건너뜀")
                results.append((lbl, "SKIP", 0, "HN 없음", {}))
                db.close()
                continue
            req = RosterRequest(year=args.year, month=args.month, group_id=gid)
            out = generate_roster_service(req, user, db)
            el = time.time() - t0
            sid = None
            if isinstance(out, dict):
                sid = out.get("schedule_id") or (out.get("schedule") or {}).get("schedule_id")
            if not sid:
                sid = db.execute(text("""
                    SELECT TOP 1 schedule_id FROM schedules
                    WHERE group_id=:g AND year=:y AND month=:m ORDER BY created_at DESC
                """), {"g": gid, "y": args.year, "m": args.month}).scalar()
            cnt = db.execute(text("SELECT COUNT(*) FROM schedule_entries WHERE schedule_id=:s"),
                             {"s": sid}).scalar() if sid else 0
            vio = violations(db, sid, gid) if sid else {}
            print(f"  {lbl:<11} ✓ {el:6.1f}s · {sid} · entries {cnt}"
                  + (f" · 위반 {vio}" if vio else " · 위반 없음"))
            results.append((lbl, "OK", el, sid, vio))
        except Exception as e:
            el = time.time() - t0
            msg = str(e).split("\n")[0][:120]
            print(f"  {lbl:<11} ✗ {el:6.1f}s · {type(e).__name__}: {msg}")
            traceback.print_exc(limit=3)
            results.append((lbl, "FAIL", el, msg, {}))
        finally:
            db.close()

    print()
    print("=" * 78)
    ok = [r for r in results if r[1] == "OK"]
    print(f"성공 {len(ok)} / 실패 {len([r for r in results if r[1]=='FAIL'])}"
          f" / 건너뜀 {len([r for r in results if r[1]=='SKIP'])}  (총 {len(results)})")
    bad = [(r[0], r[4]) for r in ok if r[4]]
    if bad:
        print("하드락 위반:")
        for lbl, v in bad:
            print(f"  {lbl}: {v}")
    else:
        print("하드락 위반: 없음")
    print("=" * 78)


if __name__ == "__main__":
    main()
