"""
shift_manage 중복행 1회 정리(dedup) 스크립트 — DB팀 실행용.

배경/근거:
  실 DB 의 shift_manage 는 PK=id 만 있고 (office_id, group_id, nurse_class, shift_slot)
  UNIQUE 제약이 없어 같은 슬롯에 중복행이 다수 존재한다(약 37% 슬롯). 솔버는
  슬롯당 1행을 가정하므로 중복 시 manpower 가 비결정적으로 읽혀 coverage 수요가 오염된다.

정리 규칙(사용자 결정):
  - 유일성 키 = (office_id, group_id, TRIM(nurse_class), shift_slot)  [year/month 무시·글로벌]
  - manpower  = 최대 id(가장 최근 저장) 행의 값을 채택
  - codes     = 같은 키의 모든 중복행 codes 의 합집합(순서 보존) — 흩어진 코드 손실 방지
  - nurse_class 는 보존 행에서 공백 제거(TRIM)
  - junk 클래스('save' 등 유효하지 않은 클래스) 행은 삭제

사용법(roster-back 디렉터리에서):
  uv run python scripts/dedup_shift_manage.py            # DRY-RUN(변경 없음, 계획만 출력)
  uv run python scripts/dedup_shift_manage.py --apply    # 실제 적용(파괴적)
  uv run python scripts/dedup_shift_manage.py --group 102560184a40   # 특정 group 만

주의:
  - .env 의 DB 접속정보(EUN_DB_NAME)가 가리키는 DB(eun_roster_dev/prod)에 그대로 실행된다.
  - 적용 후 scripts/shift_manage_unique.sql 의 UNIQUE 제약을 추가해야 재발이 막힌다.
"""
from __future__ import annotations

import argparse
import sys
from collections import OrderedDict

sys.path.insert(0, "app")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()  # db.client2 가 import 시 os.getenv 로 접속정보를 읽으므로 먼저 로드

from db.client2 import SessionLocal  # noqa: E402
from db.models import ShiftManage  # noqa: E402

# 유효 간호사 클래스(이외 = junk 로 삭제). routers/shifts.py VALID_NURSE_CLASSES 와 동일.
VALID_NURSE_CLASSES = {"RN", "AN", "보조"}


def _union_codes(rows) -> list:
    """id 오름차순 행들의 codes 합집합(첫 등장 순서 보존)."""
    merged: "OrderedDict[str, None]" = OrderedDict()
    for r in rows:
        for code in (r.codes or []):
            if code not in merged:
                merged[code] = None
    return list(merged.keys())


def main() -> int:
    parser = argparse.ArgumentParser(description="shift_manage 중복행 dedup")
    parser.add_argument("--apply", action="store_true", help="실제 적용(미지정 시 dry-run)")
    parser.add_argument("--group", default=None, help="특정 group_id 만 처리")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        q = db.query(ShiftManage)
        if args.group:
            q = q.filter(ShiftManage.group_id == args.group)
        rows = q.order_by(
            ShiftManage.office_id,
            ShiftManage.group_id,
            ShiftManage.shift_slot,
            ShiftManage.id,
        ).all()

        # 1) junk 클래스 분리
        junk = [r for r in rows if (r.nurse_class or "").strip() not in VALID_NURSE_CLASSES]
        valid = [r for r in rows if (r.nurse_class or "").strip() in VALID_NURSE_CLASSES]

        # 2) (office, group, TRIM(class), slot) 그룹핑
        groups: "OrderedDict[tuple, list]" = OrderedDict()
        for r in valid:
            key = (r.office_id, r.group_id, (r.nurse_class or "").strip(), r.shift_slot)
            groups.setdefault(key, []).append(r)

        to_delete = []          # 중복으로 삭제될 행
        survivors_changed = []  # codes/class 갱신될 보존 행
        for key, grp in groups.items():
            grp.sort(key=lambda r: r.id)  # id 오름차순
            survivor = grp[-1]            # 최대 id = 최근 저장(manpower 채택)
            union = _union_codes(grp)
            trimmed_class = key[2]
            changed = False
            if (survivor.codes or []) != union:
                changed = True
            if (survivor.nurse_class or "") != trimmed_class:
                changed = True
            if changed:
                survivors_changed.append((survivor, union, trimmed_class, grp))
            for extra in grp[:-1]:
                to_delete.append(extra)

        # 3) 리포트
        print("=== shift_manage dedup %s ===" % ("APPLY" if args.apply else "DRY-RUN"))
        print(f"전체 대상 행: {len(rows)} | 유효: {len(valid)} | junk: {len(junk)}")
        print(f"유일 슬롯(키): {len(groups)} | 삭제될 중복행: {len(to_delete)} | 보존행 갱신: {len(survivors_changed)}")
        if junk:
            print("\n[junk 삭제 행]")
            for r in junk:
                print(f"  id={r.id} {r.office_id}/{r.group_id} slot={r.shift_slot} class={r.nurse_class!r} codes={r.codes}")
        if survivors_changed:
            print("\n[codes 합집합/클래스 정정 보존행 (샘플 최대 20)]")
            for survivor, union, tclass, grp in survivors_changed[:20]:
                olds = [f"id{r.id}:{r.codes}" for r in grp]
                print(f"  keep id={survivor.id} {survivor.group_id} slot={survivor.shift_slot} "
                      f"class->{tclass!r} mp={survivor.manpower} codes->{union}  (from {olds})")

        # 4) 적용
        if not args.apply:
            print("\nDRY-RUN: 변경 없음. 실제 적용하려면 --apply 를 붙이세요.")
            return 0

        for survivor, union, tclass, _grp in survivors_changed:
            survivor.codes = union
            survivor.nurse_class = tclass
        for r in to_delete:
            db.delete(r)
        for r in junk:
            db.delete(r)
        db.commit()
        print(f"\nAPPLIED: 중복 {len(to_delete)}행 + junk {len(junk)}행 삭제, 보존행 {len(survivors_changed)}건 갱신 완료.")
        print("다음 단계: scripts/shift_manage_unique.sql 의 UNIQUE 제약을 추가하세요.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
