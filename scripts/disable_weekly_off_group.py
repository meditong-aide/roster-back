"""그룹 주휴(weekly-off) 끄기. **dry-run 기본**, `--apply` 로만 write.

## 왜 세 곳을 한 번에 쓰는가
주휴는 저장 자리가 셋이지만 **스위치는 `roster_config.weekly_off_group` 하나**다.
근무표 생성 때마다 `apply_config_side_effects`(roster_service.py:54)가 config 값을
나머지 둘로 **단방향 동기화**한다.

    roster_config.weekly_off_group
          ├→ weekly_off_settings.activate
          └→ nurses.weekly_off_enabled   ← 그룹 전원 일괄 UPDATE

★ 그래서 `nurses` 컬럼만 끄면(=주휴 관리 화면의 개인별 토글, `PUT /weekly-off/nurses`)
  **다음 생성 한 번에 전원 되살아난다.** config 를 끄지 않으면 무의미하다.
★ 반대로 config 만 끄면 다음 생성 전까지 화면에 옛 값이 보인다. 그래서 셋 다 쓴다
  (생성이 할 일을 미리 해두는 것이라 동기화 방향과 어긋나지 않는다).

## 왜 weekly_off_weekday 도 지우는가
precheck 두 곳(`monthly_limit_validator.py:569` · `runtime_bridge.py:252`)이
`weekly_off_enabled` 를 **보지 않고** `weekly_off_weekday` 만으로 강제 OFF 일수를 센다.
요일을 남기면 솔버는 주휴를 안 넣는데 precheck 는 쉰다고 계산해 가용일수를 깎아
**인원부족 오탐**이 난다. 앱의 주휴 EP(`weekly_off_service.py:351`)도 끌 때 NULL 로 지운다.

## 건드리지 않는 것
- `schedule_entries` — **이미 만들어진 근무표의 '주' 배정은 그대로 남는다.**
  지우려면 해당 근무표를 다시 만들어야 한다.
- `nurses.active` · sequence · 그 외 개인 속성.

## 알림
라우터를 타지 않고 ORM 직접 write · 푸시 모듈 import 없음.

사용:
  EUN_DB_NAME=eun_roster uv run python scripts/disable_weekly_off_group.py \
      --group-id 1025604f8279 [--apply]
  (여러 그룹은 콤마로: --group-id a,b,c)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from db.client2 import SessionLocal  # noqa: E402
from db.models import Group, Nurse, RosterConfig, WeeklyOffSetting  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group-id", required=True, help="콤마로 여러 개 지정 가능")
    ap.add_argument("--apply", action="store_true", help="지정해야만 DB 에 쓴다")
    a = ap.parse_args()

    group_ids = [g.strip() for g in a.group_id.split(",") if g.strip()]
    db = SessionLocal()
    try:
        print(f"DB={db.get_bind().url.database}\n")
        total = {"config": 0, "setting": 0, "nurse_enabled": 0, "nurse_weekday": 0}

        for gid in group_ids:
            grp = db.query(Group).filter(Group.group_id == gid).first()
            if not grp:
                raise SystemExit(f"중단 — 그룹 없음: {gid}")
            print(f"── {grp.group_name} ({gid}) ─────────────────────")

            # ① roster_config.weekly_off_group — 진짜 스위치. 그룹의 **모든** 프리셋.
            #    하나만 끄면 수간호사가 다른 버전을 고른 생성에서 되살아난다.
            cfgs = db.query(RosterConfig).filter(RosterConfig.group_id == gid).all()
            cfg_on = [c for c in cfgs if c.weekly_off_group]
            print(f"  ① roster_config      : {len(cfgs)}개 중 켜짐 {len(cfg_on)}개 → 0")
            for c in cfg_on:
                print(f"       - config_id={c.config_id} {c.config_name!r}")

            # ② weekly_off_settings.activate
            st = db.query(WeeklyOffSetting).filter(
                WeeklyOffSetting.group_id == gid).first()
            if st is None:
                print("  ② weekly_off_settings: 행 없음 (건너뜀)")
            else:
                print(f"  ② weekly_off_settings: activate {st.activate} → 0")

            # ③ nurses — 생성 동기화가 active 무관 전원을 덮으므로 여기도 전원.
            nurses = db.query(Nurse).filter(Nurse.group_id == gid).all()
            n_en = [n for n in nurses if n.weekly_off_enabled]
            n_wd = [n for n in nurses if n.weekly_off_weekday is not None]
            print(f"  ③ nurses             : {len(nurses)}명 중 "
                  f"enabled {len(n_en)}명 → 0 · weekday {len(n_wd)}명 → NULL")
            for n in n_wd:
                print(f"       - {n.name}({n.nurse_id}) weekday={n.weekly_off_weekday}")

            if a.apply:
                for c in cfg_on:
                    c.weekly_off_group = 0
                if st is not None:
                    st.activate = 0
                for n in nurses:
                    n.weekly_off_enabled = 0
                    n.weekly_off_weekday = None

            total["config"] += len(cfg_on)
            total["setting"] += 0 if st is None else 1
            total["nurse_enabled"] += len(n_en)
            total["nurse_weekday"] += len(n_wd)
            print()

        if not a.apply:
            print("[dry-run] --apply 를 붙여야 실제로 씁니다.")
            return 0

        db.commit()
        print(f"[적용] config {total['config']}행 · settings {total['setting']}행 · "
              f"nurses enabled {total['nurse_enabled']}명 / weekday "
              f"{total['nurse_weekday']}명 커밋")
        print("★ 기존 근무표의 '주' 배정은 지우지 않았습니다 — 다시 생성해야 빠집니다.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
