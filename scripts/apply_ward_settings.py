#!/usr/bin/env python
"""병동 근무표 설정 일괄 적용 — 실제 운영 근무표 분석 결과를 시스템 설정으로 반영.

인천의료원 41병동-RN 이 첫 대상이며, 다른 병동도 인자만 바꿔 쓴다.

적용 항목
    1) 필요인원  — shift_manage(RN 템플릿) + daily_shift(대상월, 평일/주말 차등)
    2) 근무표 설정 — roster_config 지정 필드
    3) 고정근무   — nurse_allowed_shift_period(SSOT).fixed_shift + nurses 캐시 투영

★ 실행 순서가 중요하다
    필요인원(shift_manage) → roster_config 순으로 적용한다.
    `save_roster_config_service` 는 day_req/eve_req/nig_req 를 요청값이 아니라
    shift_manage 에서 재계산해 덮어쓴다(roster_service.py:298-326). 이 스크립트도
    같은 규칙을 따라 shift_manage 를 먼저 쓰고 그 값으로 config 를 채운다.
    순서를 뒤집으면 config 의 요구인원이 stale 로 남는다.

사용 예 (41병동-RN)
    cd roster-back
    EUN_DB_NAME=eun_roster .venv/bin/python scripts/apply_ward_settings.py \
        --group-id 1022438ea001 --year 2026 --month 9 \
        --weekday 5,4,4,0 --weekend 4,4,4,0 \
        --config not_one_night=true \
        --config two_offs_after_two_nig=true \
        --config two_offs_after_three_nig=true \
        --config max_conseq_work=5 \
        --config preceptee_on=true \
        --fixed-shift 291306=DE \
        --apply
"""
from __future__ import annotations

import argparse
import calendar
import os
import sys
from datetime import date, datetime
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(_APP))

from sqlalchemy import func  # noqa: E402

from db.client2 import SessionLocal  # noqa: E402
from db.models import (  # noqa: E402
    DailyShift,
    Group,
    Nurse,
    NurseAllowedShiftPeriod,
    RosterConfig,
    Shift,
    ShiftManage,
)

SLOT_BY_CODE = {"D": 1, "E": 2, "N": 3, "M": 5}
BOOL_TRUE = {"true", "1", "yes", "y", "t"}
BOOL_FALSE = {"false", "0", "no", "n", "f"}


def parse_den(raw: str) -> dict[str, int]:
    """'5,4,4,0' → {'D':5,'E':4,'N':4,'M':0}"""
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) not in (3, 4):
        raise SystemExit(f"D,E,N[,M] 형식이어야 합니다: {raw!r}")
    vals = [int(p) for p in parts]
    out = dict(zip(["D", "E", "N"], vals[:3]))
    out["M"] = vals[3] if len(vals) == 4 else 0
    return out


def coerce(model_col, raw: str):
    """roster_config 컬럼 타입에 맞춰 문자열 값을 변환."""
    low = raw.strip().lower()
    py = model_col.type.python_type
    if py is bool:
        if low in BOOL_TRUE:
            return True
        if low in BOOL_FALSE:
            return False
        raise SystemExit(f"bool 로 해석할 수 없습니다: {raw!r}")
    if py is int:
        return int(raw)
    if py is float:
        return float(raw)
    return raw


def main() -> None:
    ap = argparse.ArgumentParser(description="병동 근무표 설정 일괄 적용")
    ap.add_argument("--group-id", required=True)
    ap.add_argument("--year", type=int, required=True, help="daily_shift 대상 연도")
    ap.add_argument("--month", type=int, required=True, help="daily_shift 대상 월")
    ap.add_argument("--weekday", default=None, help="평일 인원 'D,E,N[,M]'")
    ap.add_argument("--weekend", default=None, help="주말 인원 'D,E,N[,M]' (미지정 시 평일과 동일)")
    ap.add_argument("--config", action="append", default=[], metavar="KEY=VALUE",
                    help="roster_config 필드 (반복 지정)")
    ap.add_argument("--fixed-shift", action="append", default=[], metavar="NURSE_ID=CODE",
                    help="고정근무 지정. CODE=none 이면 해제 (반복 지정)")
    ap.add_argument("--allowed-shifts", action="append", default=[], metavar="NURSE_ID=D,E",
                    help="허용 근무형 지정. 'NURSE_ID=N' = N전담, 'NURSE_ID=' = 제한없음 (반복)")
    ap.add_argument("--monthly-limit", action="append", default=[],
                    metavar="NURSE_ID=FIELD:VALUE",
                    help="대상월 개인 한도. 예 '372512=n_exact:15' (반복 지정)")
    ap.add_argument("--valid-from", default=None,
                    help="고정근무 발효일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--apply", action="store_true", help="실제 반영 (미지정 시 dry-run)")
    args = ap.parse_args()

    db_name = os.getenv("EUN_DB_NAME", "(미설정)")
    days = calendar.monthrange(args.year, args.month)[1]
    weekend_days = [d for d in range(1, days + 1)
                    if calendar.weekday(args.year, args.month, d) >= 5]

    print("=" * 78)
    print(f"대상 DB : {db_name}{'   ← 운영' if db_name == 'eun_roster' else ''}")
    print(f"그룹    : {args.group_id}")
    print(f"대상월  : {args.year}-{args.month:02d} ({days}일 · 주말 {len(weekend_days)}일 {weekend_days})")
    print(f"모드    : {'APPLY' if args.apply else 'DRY-RUN (쓰기 없음)'}")
    print("=" * 78)

    db = SessionLocal()
    try:
        group = db.query(Group).filter(Group.group_id == args.group_id).first()
        if not group:
            raise SystemExit(f"그룹을 찾을 수 없습니다: {args.group_id}")
        office_id = group.office_id
        print(f"병동    : {group.group_name} (office {office_id})\n")

        # ── 1) 필요인원 ────────────────────────────────────────────
        wk = parse_den(args.weekday) if args.weekday else None
        we = parse_den(args.weekend) if args.weekend else wk
        if wk:
            print("[1] 필요인원")
            print(f"    평일 D{wk['D']} E{wk['E']} N{wk['N']} M{wk['M']}"
                  f" / 주말 D{we['D']} E{we['E']} N{we['N']} M{we['M']}")

            existing_sm = {
                sm.shift_slot: sm for sm in db.query(ShiftManage).filter(
                    ShiftManage.office_id == office_id,
                    ShiftManage.group_id == args.group_id,
                    ShiftManage.nurse_class == "RN",
                ).all()
            }
            for code, slot in SLOT_BY_CODE.items():
                cur = existing_sm.get(slot)
                before = getattr(cur, "manpower", None) if cur else None
                print(f"    shift_manage slot{slot}({code}): {before} → {wk[code]}"
                      + ("" if cur else "  [신규]"))
                if not args.apply:
                    continue
                if cur:
                    cur.manpower = wk[code]
                    cur.updated_at = datetime.now()
                else:
                    # created_at 은 모델에 default 가 없는데 운영 DB 는 NOT NULL 이라
                    # 명시하지 않으면 IntegrityError(515) 가 난다.
                    _now = datetime.now()
                    db.add(ShiftManage(
                        office_id=office_id, group_id=args.group_id, nurse_class="RN",
                        shift_slot=slot, main_code=code, codes=[], manpower=wk[code],
                        created_at=_now, updated_at=_now,
                    ))

            ds_rows = {
                r.day: r for r in db.query(DailyShift).filter(
                    DailyShift.office_id == office_id,
                    DailyShift.group_id == args.group_id,
                    DailyShift.year == args.year,
                    DailyShift.month == args.month,
                ).all()
            }
            print(f"    daily_shift {args.year}-{args.month:02d}: 기존 {len(ds_rows)}행 → "
                  f"{days}행(day 1~{days})")
            if args.apply:
                for d in range(1, days + 1):
                    v = we if d in weekend_days else wk
                    row = ds_rows.get(d)
                    if row is None:
                        row = DailyShift(
                            office_id=office_id, group_id=args.group_id,
                            year=args.year, month=args.month, day=d,
                        )
                        db.add(row)
                    row.d_count, row.e_count = v["D"], v["E"]
                    row.n_count, row.m_count = v["N"], v["M"]
                    row.d_count_max = row.e_count_max = 0
                    row.n_count_max = row.m_count_max = 0
                    row.max_enabled = False
                db.flush()
            print()

        # ── 2) roster_config ──────────────────────────────────────
        cfg = (
            db.query(RosterConfig)
            .filter(RosterConfig.group_id == args.group_id)
            .order_by(RosterConfig.config_id.desc())
            .first()
        )
        if args.config or wk:
            if not cfg:
                raise SystemExit("roster_config 를 찾을 수 없습니다.")
            print(f"[2] roster_config (config_id={cfg.config_id} v{cfg.version} '{cfg.config_name}')")
            for item in args.config:
                if "=" not in item:
                    raise SystemExit(f"--config 는 KEY=VALUE 형식: {item!r}")
                key, raw = item.split("=", 1)
                key = key.strip()
                col = getattr(RosterConfig, key, None)
                if col is None or not hasattr(col, "type"):
                    raise SystemExit(f"roster_config 에 없는 필드: {key}")
                val = coerce(col, raw)
                before = getattr(cfg, key)
                mark = "" if before == val else "  ←변경"
                print(f"    {key}: {before} → {val}{mark}")
                if args.apply:
                    setattr(cfg, key, val)
            if wk:
                # save_roster_config_service 와 동일 규칙: 요구인원은 shift_manage 값으로 채운다
                print(f"    day_req/eve_req/nig_req: "
                      f"{cfg.day_req}/{cfg.eve_req}/{cfg.nig_req} → {wk['D']}/{wk['E']}/{wk['N']}"
                      "  (shift_manage 기준 재계산)")
                if args.apply:
                    cfg.day_req, cfg.eve_req, cfg.nig_req = wk["D"], wk["E"], wk["N"]
            print()

        # ── 3) 고정근무 ───────────────────────────────────────────
        if args.fixed_shift:
            from services.nurse_period_resolver import upsert_period

            vf = date.fromisoformat(args.valid_from) if args.valid_from else date.today()
            print(f"[3] 고정근무 (발효일 {vf})")
            valid_codes = {s.shift_id for s in
                           db.query(Shift).filter(Shift.group_id == args.group_id).all()}
            for item in args.fixed_shift:
                if "=" not in item:
                    raise SystemExit(f"--fixed-shift 는 NURSE_ID=CODE 형식: {item!r}")
                nid, code = item.split("=", 1)
                nid, code = nid.strip(), code.strip()
                nurse = db.query(Nurse).filter(Nurse.nurse_id == nid).first()
                if not nurse:
                    raise SystemExit(f"간호사를 찾을 수 없습니다: {nid}")
                new_val = None if code.lower() in ("none", "null", "") else code
                if new_val and new_val not in valid_codes:
                    raise SystemExit(f"그룹 shifts 에 없는 근무코드: {new_val}")
                print(f"    {nurse.name}({nid}): {nurse.fixed_shift} → {new_val}")
                if args.apply:
                    upsert_period(
                        db, NurseAllowedShiftPeriod, nid, vf,
                        "fixed_shift", new_val,
                        group_id=None, nurse=nurse, cache_attr="fixed_shift",
                        source="edited", carry_attrs=["allowed_shifts"],
                    )
            print()

        # ── 4) 허용 근무형 (N전담 등) ─────────────────────────────
        if args.allowed_shifts:
            from services.nurse_period_resolver import upsert_period

            vf = date.fromisoformat(args.valid_from) if args.valid_from else date.today()
            print(f"[4] 허용 근무형 (발효일 {vf})")
            valid_codes = {s.shift_id for s in
                           db.query(Shift).filter(Shift.group_id == args.group_id).all()}
            for item in args.allowed_shifts:
                if "=" not in item:
                    raise SystemExit(f"--allowed-shifts 는 NURSE_ID=CODE[,CODE] 형식: {item!r}")
                nid, raw = item.split("=", 1)
                nid = nid.strip()
                codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
                bad = [c for c in codes if c not in valid_codes]
                if bad:
                    raise SystemExit(f"그룹 shifts 에 없는 근무코드: {bad}")
                nurse = db.query(Nurse).filter(Nurse.nurse_id == nid).first()
                if not nurse:
                    raise SystemExit(f"간호사를 찾을 수 없습니다: {nid}")
                before = nurse.allowed_shifts
                label = codes if codes else "[] (제한없음)"
                print(f"    {nurse.name}({nid}): {before} → {label}")
                if args.apply:
                    upsert_period(
                        db, NurseAllowedShiftPeriod, nid, vf,
                        "allowed_shifts", codes,
                        group_id=None, nurse=nurse, cache_attr="allowed_shifts",
                        source="edited", carry_attrs=["fixed_shift"],
                    )
            print()

        # ── 5) 개인 월 한도 ───────────────────────────────────────
        if args.monthly_limit:
            from db.models import NurseMonthlyLimit

            print(f"[5] 개인 월 한도 ({args.year}-{args.month:02d})")
            for item in args.monthly_limit:
                if "=" not in item or ":" not in item:
                    raise SystemExit(f"--monthly-limit 는 NURSE_ID=FIELD:VALUE 형식: {item!r}")
                nid, spec = item.split("=", 1)
                field, raw = spec.split(":", 1)
                nid, field = nid.strip(), field.strip()
                col = getattr(NurseMonthlyLimit, field, None)
                if col is None or not hasattr(col, "type"):
                    raise SystemExit(f"nurse_monthly_limits 에 없는 필드: {field}")
                val = None if raw.strip().lower() in ("none", "null", "") else int(raw)
                nurse = db.query(Nurse).filter(Nurse.nurse_id == nid).first()
                if not nurse:
                    raise SystemExit(f"간호사를 찾을 수 없습니다: {nid}")
                row = (
                    db.query(NurseMonthlyLimit)
                    .filter(
                        NurseMonthlyLimit.nurse_id == nid,
                        NurseMonthlyLimit.group_id == args.group_id,
                        NurseMonthlyLimit.year == args.year,
                        NurseMonthlyLimit.month == args.month,
                    )
                    .first()
                )
                before = getattr(row, field, None) if row else None
                print(f"    {nurse.name}({nid}) {field}: {before} → {val}"
                      + ("" if row else "  [신규]"))
                if not args.apply:
                    continue
                _now = datetime.now()
                if row is None:
                    # created_at/updated_at 이 NOT NULL 이라 명시 필요(DB DEFAULT 는 있으나
                    # ORM 이 NULL 을 명시 전송하면 실패한다 — shift_manage 와 같은 패턴).
                    row = NurseMonthlyLimit(
                        nurse_id=nid, group_id=args.group_id,
                        year=args.year, month=args.month,
                        created_at=_now, updated_at=_now,
                    )
                    db.add(row)
                else:
                    row.updated_at = _now
                setattr(row, field, val)
            print()

        if not args.apply:
            print("DRY-RUN 종료 — 실제 반영하려면 --apply 를 붙이세요.")
            return

        db.commit()
        print("=" * 78)
        print("적용 완료")
        print("=" * 78)
    except SystemExit:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
