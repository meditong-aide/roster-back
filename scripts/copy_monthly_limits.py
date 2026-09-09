"""개인별 월 근무 제한(`nurse_monthly_limits`)을 다른 달로 복사한다.

**dry-run 기본**, `--apply` 로만 write.

## 왜 필요한가

`nurse_monthly_limits` 는 **(간호사, 년, 월)** 단위라 이월되지 않는다. 수간호사가
"나이트 15개로 고정" 같은 설정을 8월에 넣어도 9월 근무표를 생성하면 그대로 풀린다.
(실측 2026-08-18: 7B병동 8월 4건 · 9월 0건 — 같은 문의가 매달 재발할 수 있는 구조)

## ★ 알림은 나가지 않는다

라우터·서비스를 타지 않고 ORM 으로 직접 write 하며 **푸시 모듈을 import 조차 하지 않는다.**
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from db.client2 import SessionLocal  # noqa: E402
from db.models import Nurse, NurseMonthlyLimit  # noqa: E402

#: 복사 대상에서 뺄 컬럼(식별자·타임스탬프).
_SKIP = {"id", "year", "month", "created_at", "updated_at"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group-ids", required=True, help="콤마 구분")
    ap.add_argument("--from", dest="src", required=True, help="예: 2026-08")
    ap.add_argument("--to", dest="dst", required=True, help="예: 2026-09")
    ap.add_argument("--apply", action="store_true", help="지정해야만 DB 에 쓴다")
    a = ap.parse_args()
    gids = [g.strip() for g in a.group_ids.split(",") if g.strip()]
    sy, sm = (int(x) for x in a.src.split("-"))
    dy, dm = (int(x) for x in a.dst.split("-"))

    cols = [c.name for c in NurseMonthlyLimit.__table__.columns if c.name not in _SKIP]

    db = SessionLocal()
    try:
        print(f"DB={db.get_bind().url.database}  {a.src} → {a.dst}  groups={gids}")
        names = {str(n.nurse_id): n.name for n in
                 db.query(Nurse).filter(Nurse.group_id.in_(gids)).all()}

        src = db.query(NurseMonthlyLimit).filter(
            NurseMonthlyLimit.group_id.in_(gids),
            NurseMonthlyLimit.year == sy, NurseMonthlyLimit.month == sm).all()
        dst = db.query(NurseMonthlyLimit).filter(
            NurseMonthlyLimit.group_id.in_(gids),
            NurseMonthlyLimit.year == dy, NurseMonthlyLimit.month == dm).all()
        have = {str(r.nurse_id) for r in dst}
        print(f"[현황] 원본 {len(src)}행 · 대상월 기존 {len(dst)}행")

        if not src:
            raise SystemExit("원본이 없습니다 — 중단")

        plan = []
        for r in src:
            nid = str(r.nurse_id)
            vals = {c: getattr(r, c) for c in cols}
            body = {k: v for k, v in vals.items()
                    if k not in ("nurse_id", "group_id") and v is not None}
            if nid in have:
                print(f"   · 건너뜀 {names.get(nid, nid)} — 대상월에 이미 있음")
                continue
            # ★ `nurses` 에 없는 고아 행은 복사하지 않는다. 삭제된 인원의 제한을
            #   다음 달로 끌고 가면 아무도 모르는 채로 계속 남는다.
            #   (실측: 454640 — nurses 에 없고 2026-08 `n_max=3` 한 건만 존재)
            if nid not in names:
                print(f"   · 건너뜀 {nid} — nurses 에 없음(삭제된 인원) {body}")
                continue
            plan.append((r, vals))
            print(f"   + {names.get(nid, nid):<8} {body}")

        print(f"\n[계획] {len(plan)}행 추가")
        if not a.apply:
            print("[dry-run] --apply 를 붙여야 실제로 씁니다.")
            return 0

        for _r, vals in plan:
            db.add(NurseMonthlyLimit(year=dy, month=dm, **vals))
        db.commit()
        print(f"[적용] nurse_monthly_limits {len(plan)}행 커밋 (알림 없음)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
