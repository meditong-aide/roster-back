"""마감된 근무표의 휴가·교육 코드를 확정 원티드(`fixed_wanted_entries`)로 역주입한다.

**dry-run 기본**, `--apply` 로만 write.

## 왜 필요한가 — 재현 검증의 입력을 맞추기 위해

과거 근무표는 병원이 엑셀로 만들어 넣은 것이라 원티드 기록이 없는 경우가 있다
(2026-08-18 실측: 수술실 8월 `wanted_requests` 0행 · `fixed_wanted_entries` 0건인데
마감본에는 보건 11 · 휴 8 · 휴PM 8 · 노조교육 4 · 산전 1 이 들어 있다).

이 상태로 같은 달을 다시 생성하면 그 휴가들이 사라져 **근무표가 원본과 벌어지고**,
콜 배정 재현율도 함께 떨어진다. 마감본에 남아 있는 코드를 확정 원티드로 되돌려
넣으면 생성이 원본을 따라간다.

## 무엇을 넣고 무엇을 빼는가

기본 근무·콜·휴직은 제외한다 — 그건 엔진이 만들어내는 값이지 개인 신청이 아니다.
`--exclude` 로 조정할 수 있고, 기본값은 D1/O/M 계열과 콜 코드, 휴직 코드다.

## ★ 알림은 나가지 않는다

라우터·서비스를 타지 않고 ORM 으로 직접 write 하며 **푸시 모듈을 import 조차 하지 않는다.**
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from db.client2 import SessionLocal  # noqa: E402
from db.models import (  # noqa: E402
    FixedWantedEntry, Nurse, Schedule, ScheduleEntry, Shift,
)
from services.oncall_assign import load_call_code_map  # noqa: E402

#: 개인 신청이 아니라 엔진 산출물인 코드 — 확정 원티드로 넣지 않는다.
_DEFAULT_EXCLUDE = ("D", "E", "N", "M", "O", "주", "D1", "육휴", "휴직", "산휴", "출산")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule-id", required=True)
    ap.add_argument("--exclude", default="", help="추가 제외 코드(콤마 구분)")
    ap.add_argument("--apply", action="store_true", help="지정해야만 DB 에 쓴다")
    a = ap.parse_args()

    db = SessionLocal()
    try:
        sch = db.query(Schedule).filter(Schedule.schedule_id == a.schedule_id).first()
        if sch is None:
            raise SystemExit(f"schedule 없음: {a.schedule_id}")
        gid, year, month = sch.group_id, sch.year, sch.month
        print(f"DB={db.get_bind().url.database}  schedule={a.schedule_id} "
              f"{year}-{month:02d} status={sch.status} group={gid}")

        exclude = set(_DEFAULT_EXCLUDE)
        exclude |= {s.strip() for s in a.exclude.split(",") if s.strip()}
        exclude |= set(load_call_code_map(db, gid).values())   # 콜 코드도 제외
        print(f"[제외] {sorted(exclude)}")

        names = {str(n.nurse_id): n.name for n in
                 db.query(Nurse).filter(Nurse.group_id == gid).all()}
        pk = {s.shift_id: s.id for s in db.query(Shift).filter(Shift.group_id == gid).all()}
        have = {(str(f.nurse_id), f.shift_date) for f in db.query(FixedWantedEntry).filter(
            FixedWantedEntry.group_id == gid,
            FixedWantedEntry.year == year, FixedWantedEntry.month == month).all()}

        plan, skipped = [], []
        for e in db.query(ScheduleEntry).filter(
                ScheduleEntry.schedule_id == a.schedule_id).all():
            code = str(e.shift_id or "").strip()
            nid = str(e.nurse_id)
            if code in exclude or not code:
                continue
            if nid not in names:
                skipped.append((nid, code, "nurses 에 없음"))
                continue
            wd = e.work_date.date() if hasattr(e.work_date, "date") else e.work_date
            if (nid, wd) in have:
                skipped.append((names[nid], code, "이미 확정원티드 있음"))
                continue
            plan.append((nid, wd, code))

        print(f"\n[계획] {len(plan)}건 추가 · 건너뜀 {len(skipped)}")
        print(f"   {dict(Counter(c for _n, _d, c in plan))}")
        for nid, wd, code in sorted(plan, key=lambda x: (x[1], names.get(x[0], ""))):
            print(f"   {wd} {names[nid]:<8} {code}")
        if skipped:
            print(f"\n[건너뜀] {skipped[:8]}{' …' if len(skipped) > 8 else ''}")

        if not a.apply:
            print("\n[dry-run] --apply 를 붙여야 실제로 씁니다.")
            return 0

        for nid, wd, code in plan:
            db.add(FixedWantedEntry(
                group_id=gid, year=year, month=month, nurse_id=nid,
                shift_date=wd, shift_id=code, shifts_table_id=pk.get(code),
                is_applied=True, source_type="original",
                reason="마감 근무표 역주입", created_by=None,
            ))
        db.commit()
        print(f"\n[적용] fixed_wanted_entries {len(plan)}건 커밋 (알림 없음)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
