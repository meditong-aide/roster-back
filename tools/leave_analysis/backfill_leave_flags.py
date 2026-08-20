"""휴가 대상 3-state 백필 — 마감본 실적 → `nurse_leave_period`.

`health_leave_eligible` 과 `pregnant` 만 채운다. **`sleep_off_eligible` 은 대상 아니다** —
미설정이 곧 "N 을 서는 전원" 이라 백필할 것이 없고, 실제 부여는 N 연번이 가른다.

## 왜 백필이 필요한가

두 값은 자동판정으로 못 맞춘다. 설계가 처음부터 "예외만 행을 만든다" 였는데
그 예외가 하나도 안 들어가 있으면 아래처럼 어긋난다.

    health_leave_eligible
        고정근무자는 자동판정에서 auto=False 다(health_leave_planner.eligible_nurses).
        그런데 실측은 고정근무 69명 중 34명(49.3%)이 보건휴가를 받는다.
        "켜면 35명 과잉·끄면 34명 누락" 이라 **줄 사람만 1 로 관리**하는 설계인데,
        그 1 이 비어 있으면 34명이 통째로 누락된다.

    pregnant
        자동판정 소스가 **없다**. 예전엔 확정 원티드 코드명이 "산전" 으로 시작하는지
        봤지만 병원마다 코드가 달라(VD·출산) 명시 관리로 바꿨다. 지금은 전원 NULL 이라
        임산부도 자동판정(여성·N전담 아님)을 통과해 보건휴가를 받는다 —
        실측은 정반대다(산전 17건 중 보건 수령 0건).

## 판별 근거

    health_leave_eligible=1   고정근무자(fixed_shift)이면서 마감본에서
                              `shifts.health_leave_target=True` 코드를 실제로 받은 사람
    pregnant=1                마감본에서 산전·출산 계열 코드를 쓴 사람

★ 코드 지목은 이름이 아니라 표식·name 으로 한다. `health_leave_target` 은 그룹당 1건
  표식이고, 산전 계열은 `shift_id` 가 병동마다 갈려(산전·출산·VD) `name` 에
  '산전'·'출산' 이 들어가는지로 잡는다.

★★ **`출산` 계열은 성별로 걸러야 한다.** 배우자 출산휴가(법정 20일)를 같은 코드로
  쓰기 때문이다 — 실측 2026-08-19 dev: 라일엽(남·102243e39fbf)이 `출산` 20일.
  코드만 보고 넣으면 남성이 임산부로 기록된다. 그래서 `gender='여'` 만 대상이다.

## 한계 — 그대로 반영하면 안 되는 것

★ `health_leave_eligible` 은 **과거 실적**이다. 7·8월에 받았다고 9월에도 받는다는
  뜻이 아니라 수간호사 재량이다. 이 백필은 "현재 관행을 시스템에 옮기는" 것이지
  규칙 확정이 아니다. 적용 전 병동에 확인받는 편이 안전하다.

★ `pregnant` 는 **끝이 있다.** 출산하면 해제해야 하는데 출산일을 모른다. 여기서는
  열린 구간(valid_to=NULL)으로 열고, 운영자가 닫는다. 산전휴가를 아직 안 쓴
  임신 초기는 잡히지 않는다.

★ 고정근무 판별은 `nurses.fixed_shift`(현재값)를 쓴다. SSOT 는
  `nurse_allowed_shift_period` 라 과거월과 어긋날 수 있다 — 최근 마감본을 대상으로
  삼으면 차이가 거의 없지만, 오래된 달을 넣으면 오판이 는다.

## 실행

    cd roster-back
    # dry-run (--valid-from 은 필수)
    .venv/bin/python tools/leave_analysis/backfill_leave_flags.py --valid-from 2026-09-01

    # 1단계 — pregnant 만 (기본값)
    .venv/bin/python tools/leave_analysis/backfill_leave_flags.py \
        --from 2026-07 --to 2026-08 --valid-from 2026-09-01 --apply

    # 2단계 — health_leave_eligible 은 병동 확인 후
    .venv/bin/python tools/leave_analysis/backfill_leave_flags.py \
        --from 2026-07 --to 2026-08 --valid-from 2026-09-01 --only health --apply

★ `--from/--to` 는 **판별 근거를 뽑을 달**, `--valid-from` 은 **언제부터 적용할지** 다.
  둘을 분리한 이유 — `valid_to=NULL` 인 열린 구간이라 한 번 넣으면 그 달 이후 **영구**
  적용된다(실증: 2026-08-01 구간이 2027-06 조회에도 잡힘). `--valid-from` 을 판별
  기간과 같게 두면 **이미 마감된 달까지 덮어** 재생성 시 결과가 바뀐다.

★ `--only` 기본값이 `pregnant` 인 이유 — 제외 방향이라 틀려도 "안 준" 상태고 나중에
  주면 복구된다. `health` 는 부여 방향이라 이미 나간 휴가를 회수할 수 없다.

★ 되돌리려면 같은 사람에게 `None` 을 명시해 미설정으로 되돌린다(행은 남고 값만 비는
  것이 정상이다). `source='inherited'` · `valid_from` 으로 백필분을 식별할 수 있다.

기본은 **dry-run** 이다. `--apply` 없이는 아무것도 쓰지 않는다.
`--all-groups` 를 주지 않으면 `roster_config.health_leave_enabled` 가 켜진 그룹만 본다
(꺼진 병동은 백필해도 아무 일이 안 일어난다).
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, "app")

from db.client2 import SessionLocal  # noqa: E402
from db.models import (  # noqa: E402
    Group,
    Nurse,
    NurseAllowedShiftPeriod,
    RosterConfig,
    Schedule,
    ScheduleEntry,
    Shift,
)
from services.leave.leave_eligibility import fetch_leave_flags, upsert_leave_period  # noqa: E402
from services.nurse_period_resolver import fetch_periods, resolve_asof  # noqa: E402
from sqlalchemy import and_, func  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

#: 산전·출산 계열 판별 — `shift_id` 는 병동마다 갈려서 `name` 으로 본다.
PREGNANCY_NAME_HINTS = ("산전", "출산")


def _parse_ym(s: str) -> tuple[int, int]:
    y, m = s.split("-")
    return int(y), int(m)


def _months(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    out, cur = [], a
    while cur <= b:
        out.append(cur)
        cur = (cur[0] + 1, 1) if cur[1] == 12 else (cur[0], cur[1] + 1)
    return out


def _enabled_groups(db: Session, office: str) -> set[str]:
    """`health_leave_enabled` 가 켜진 그룹 — 그룹별 **최신** config 기준."""
    # ★ 런타임 소비자(health_leave_postprocess·sleep_off_postprocess)는 전부
    #   `created_at` 최신 첫 건을 쓴다. 흉내내는 쪽이 규약을 맞춘다.
    # ★★ 공용 온프렘 DB 라 **전수 조회 금지**(CLAUDE.md 「최소 검증 원칙」).
    #   roster_config 는 버전 이력 테이블이라 저장 횟수만큼 쌓인다 — 그룹당 1행으로
    #   좁히고, 필요한 두 컬럼만 받는다(엔티티 전체 하이드레이션 금지).
    sub = (db.query(RosterConfig.group_id,
                    func.max(RosterConfig.created_at).label("ts"))
           .filter(RosterConfig.office_id == office)
           .group_by(RosterConfig.group_id).subquery())
    rows = (db.query(RosterConfig.group_id, RosterConfig.health_leave_enabled)
            .join(sub, and_(RosterConfig.group_id == sub.c.group_id,
                            RosterConfig.created_at == sub.c.ts))
            .filter(RosterConfig.office_id == office).all())
    return {str(g) for g, en in rows if bool(en)}


#: "구간이 없다" 와 "구간이 있는데 값이 NULL(명시적 해제)" 를 가르는 센티널.
_SENT = object()


def _fixed_asof(db: Session, nurse_ids: list[str], months: list[tuple[int, int]],
                meta: dict) -> dict[str, str]:
    """대상 기간에 **한 번이라도** 고정근무였던 사람 `{nurse_id: fixed_shift}`.

    ★ SSOT 는 `nurse_allowed_shift_period` 다(feedback: 컬럼 UPDATE 만으론 무효).
      7월엔 고정이었다가 8월에 풀린 사람을 캐시로만 보면 놓친다.

    ★★ **구간이 없으면 캐시(`nurses.fixed_shift`)로 떨어진다** — 생성기
      `_overlay_home_profile_asof` 와 같은 규약이다("gap → 캐시 유지, 비회귀").
      as-of 만 쓰면 period 가 안 만들어진 사람이 통째로 빠진다
      (실측 2026-08-19 dev: 안현정·이소미·노미선 3명이 캐시엔 'M' 인데 구간 0건).
      반대로 **구간이 있는데 값이 NULL 이면 명시적 해제**라 제외한다.
    """
    if not nurse_ids:
        return {}
    out: dict[str, str] = {}
    for y, m in months:
        ms = date(y, m, 1)
        rows = fetch_periods(db, NurseAllowedShiftPeriod, nurse_ids, ms,
                             date(y + (m == 12), (m % 12) + 1, 1))
        for nid in nurse_ids:
            if nid in out:
                continue
            fx = resolve_asof(rows.get(nid), ms, "fixed_shift", default=_SENT)
            if fx is _SENT:                       # 그 달 구간 없음 → 캐시 폴백
                fx = getattr(meta.get(nid), "fixed_shift", None)
            if str(fx or "").strip():
                out[nid] = str(fx).strip()
    return out


def _target_codes(db: Session, gids: list[str]) -> tuple[dict[str, str], set[str]]:
    """(그룹별 보건휴가 코드, 산전 계열 코드 집합)."""
    hl: dict[str, str] = {}
    preg: set[str] = set()
    for s in db.query(Shift).filter(Shift.group_id.in_(gids)).all():
        if bool(getattr(s, "health_leave_target", False)):
            hl[str(s.group_id)] = str(s.shift_id)
        name = str(getattr(s, "name", "") or "")
        if any(h in name for h in PREGNANCY_NAME_HINTS):
            preg.add(str(s.shift_id))
    return hl, preg


def collect(db: Session, office: str, months: list[tuple[int, int]],
            all_groups: bool) -> tuple[dict, dict, dict]:
    """마감본을 훑어 (보건휴가 수령자, 산전 사용자, 간호사 메타) 를 만든다."""
    gids = [str(g.group_id) for g in
            db.query(Group).filter(Group.office_id == office).all()]
    if not all_groups:
        on = _enabled_groups(db, office)
        gids = [g for g in gids if g in on]
    if not gids:
        return {}, {}, {}

    hl_code, preg_codes = _target_codes(db, gids)
    scheds = (db.query(Schedule)
              .filter(Schedule.group_id.in_(gids),
                      Schedule.status == "issued",
                      Schedule.dropped == False,  # noqa: E712
                      Schedule.year.in_({y for y, _ in months}))
              .all())
    scheds = [s for s in scheds if (int(s.year), int(s.month)) in set(months)]
    if not scheds:
        return {}, {}, {}
    gmap = {s.schedule_id: str(s.group_id) for s in scheds}

    hl_got: dict[str, set[str]] = defaultdict(set)     # nurse_id -> {group_id}
    preg_used: dict[str, set[str]] = defaultdict(set)
    rows = (db.query(ScheduleEntry.schedule_id, ScheduleEntry.nurse_id,
                     ScheduleEntry.shift_id)
            .filter(ScheduleEntry.schedule_id.in_(list(gmap))).all())
    for sid, nid, code in rows:
        gid = gmap.get(sid)
        c = str(code or "")
        if gid and hl_code.get(gid) and c == hl_code[gid]:
            hl_got[str(nid)].add(gid)
        if c in preg_codes:
            preg_used[str(nid)].add(gid or "")

    meta = {str(n.nurse_id): n for n in
            db.query(Nurse).filter(Nurse.group_id.in_(gids), Nurse.active == 1).all()}
    return hl_got, preg_used, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--office", default="102243")
    ap.add_argument("--from", dest="ym_from", default="2026-07")
    ap.add_argument("--to", dest="ym_to", default="2026-08")
    ap.add_argument("--valid-from", required=True,
                    help="적용 시작일(YYYY-MM-DD). **필수** — 판별 기간(--from/--to)과 "
                         "다르다. 과거 달을 덮지 않으려면 다음 달 1일을 준다.")
    ap.add_argument("--only", choices=["pregnant", "health", "all"], default="pregnant",
                    help="반영 대상. 기본 pregnant — 제외 방향이라 틀려도 복구된다. "
                         "health 는 부여 방향이라 회수가 안 되니 병동 확인 후에 준다.")
    ap.add_argument("--all-groups", action="store_true",
                    help="health_leave_enabled 가 꺼진 그룹도 포함")
    ap.add_argument("--apply", action="store_true", help="실제 반영(기본 dry-run)")
    args = ap.parse_args()

    a, b = _parse_ym(args.ym_from), _parse_ym(args.ym_to)
    months = _months(a, b)
    valid_from = date.fromisoformat(args.valid_from)

    db = SessionLocal()
    try:
        hl_got, preg_used, meta = collect(db, args.office, months, args.all_groups)
        if not meta:
            print("대상 그룹·마감본이 없습니다.")
            return 1

        # 고정근무자이면서 보건휴가를 받은 사람만 = auto 로는 못 맞추는 대상.
        # ★ 고정 여부는 그 달 as-of 로 본다(캐시 컬럼이 아니라 period SSOT).
        fixed_map = _fixed_asof(db, list(hl_got & meta.keys()), months, meta)
        hl_targets = set(fixed_map)
        # ★ 배우자 출산휴가가 같은 코드라 성별로 거른다(위 docstring 참조).
        preg_targets = {nid for nid in preg_used
                        if nid in meta
                        and str(getattr(meta[nid], "gender", "") or "").strip() == "여"}
        preg_dropped = {nid for nid in preg_used
                        if nid in meta and nid not in preg_targets}

        # ★★ 임산부는 health 백필 대상에서 뺀다.
        #   `is_health_leave_eligible` 이 explicit 를 **pregnant 보다 먼저** 보고 리턴하므로
        #   (leave_eligibility.py), 둘 다 1 이면 임산부가 보건휴가를 받는다. 운영자가 직접
        #   켠 경우라면 판단 존중이지만 백필은 자동 생성이라 그 예외를 만들면 안 된다.
        hl_vs_preg = hl_targets & preg_targets
        hl_targets -= preg_targets

        # 이미 값이 있는 사람은 건드리지 않는다 — 운영자가 손댄 것을 덮으면 안 된다.
        # ★ 조회 기준월은 **적용 시작월**이다. 판별 기간(a)으로 보면 valid_from 이 그보다
        #   뒤일 때 운영자가 그 사이에 넣은 설정이 구간에 안 걸려 그대로 덮인다.
        existing = fetch_leave_flags(db, list(meta), valid_from.year, valid_from.month)
        skip_hl = {n for n in hl_targets
                   if (existing.get(n) or {}).get("health_leave_eligible") is not None}
        skip_pg = {n for n in preg_targets
                   if (existing.get(n) or {}).get("pregnant") is not None}

        print(f"■ office={args.office} · {args.ym_from}~{args.ym_to} 마감본 기준 "
              f"· 그룹 {'전체' if args.all_groups else 'health_leave_enabled 만'}")
        print(f"   대상 간호사 {len(meta)}명 · valid_from={valid_from}\n")

        do_hl = args.only in ("health", "all")
        do_pg = args.only in ("pregnant", "all")

        def _show(title, ids, skips, extra, active=True):
            if not active:
                print(f"■ {title} {len(ids) - len(skips)}명 "
                      f"— **이번엔 미반영** (--only={args.only})\n")
                return
            print(f"■ {title} {len(ids) - len(skips)}명 "
                  f"(이미 설정돼 건너뜀 {len(skips)}명)")
            for nid in sorted(ids - skips,
                              key=lambda x: (str(getattr(meta[x], 'group_id', '')),
                                             str(getattr(meta[x], 'name', '')))):
                n = meta[nid]
                print(f"      {getattr(n, 'name', '?'):<8} {getattr(n, 'group_id', '?')} "
                      f"{extra(n)}")
            print()

        _show("health_leave_eligible=1 (고정근무 + 보건휴가 수령)", hl_targets, skip_hl,
              lambda n: f"fixed(as-of)={fixed_map.get(str(n.nurse_id))!r} "
                        f"cache={getattr(n, 'fixed_shift', None)!r} "
                        f"gender={getattr(n, 'gender', None)}", do_hl)
        _show("pregnant=1 (산전·출산 계열 사용 · 여성만)", preg_targets, skip_pg,
              lambda n: f"gender={getattr(n, 'gender', None)}", do_pg)
        if hl_vs_preg:
            print(f"■ health 제외 {len(hl_vs_preg)}명 — 임산부와 겹쳐 보건휴가 백필 대상에서 뺌")
            for nid in sorted(hl_vs_preg):
                n = meta[nid]
                print(f"      {getattr(n, 'name', '?'):<8} {getattr(n, 'group_id', '?')}")
            print()
        if preg_dropped:
            print(f"■ 제외 {len(preg_dropped)}명 — 산전·출산 코드를 썼으나 여성이 아님 "
                  f"(배우자 출산휴가로 본다)")
            for nid in sorted(preg_dropped):
                n = meta[nid]
                print(f"      {getattr(n, 'name', '?'):<8} {getattr(n, 'group_id', '?')} "
                      f"gender={getattr(n, 'gender', None)}")
            print()

        if not args.apply:
            print("※ dry-run 입니다. 실제 반영하려면 --apply 를 붙이세요.")
            return 0

        print(f"■ 반영 대상: --only={args.only} "
              f"(health={'O' if do_hl else 'X'} · pregnant={'O' if do_pg else 'X'})")
        wrote = 0
        if do_hl:
            for nid in sorted(hl_targets - skip_hl):
                upsert_leave_period(db, nid, valid_from,
                                    source="inherited", health_leave_eligible=True)
                wrote += 1
        if do_pg:
            for nid in sorted(preg_targets - skip_pg):
                upsert_leave_period(db, nid, valid_from,
                                    source="inherited", pregnant=True)
                wrote += 1
        db.commit()

        # ★ 검증은 **적용 시작월** 기준이다 — 판별 기간(a)으로 조회하면 valid_from 이
        #   그보다 뒤일 때 구간에 안 걸려 "누락" 으로 오판한다(실측 2026-08-19).
        after = fetch_leave_flags(db, list(meta), valid_from.year, valid_from.month)
        exp_hl = len(hl_targets - skip_hl) if do_hl else 0
        exp_pg = len(preg_targets - skip_pg) if do_pg else 0
        ok_hl = sum(1 for n in hl_targets - skip_hl
                    if (after.get(n) or {}).get("health_leave_eligible") is True)
        ok_pg = sum(1 for n in preg_targets - skip_pg
                    if (after.get(n) or {}).get("pregnant") is True)
        print(f"■ 반영 {wrote}건")
        print(f"   검증 health_leave_eligible=1 {ok_hl}/{exp_hl} · pregnant=1 {ok_pg}/{exp_pg}")
        assert ok_hl >= exp_hl, "보건휴가 반영 누락"
        assert ok_pg >= exp_pg, "임산부 반영 누락"
        print("OK")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
