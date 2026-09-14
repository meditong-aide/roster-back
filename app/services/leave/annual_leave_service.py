"""연차 잔여 장부 — 마감본 기준 차감과 이후 달 연쇄 갱신.

같은 폴더의 `night_cycle_service` 와 **같은 층위**다(월별 스냅샷 · 마감본이 진실 ·
과거가 바뀌면 이후가 전부 틀어지므로 연쇄 재계산). 그쪽에서 이미 밟은 함정을 그대로 피한다.

## 두 갈래를 혼동하지 말 것

    확정 장부 (`nurse_annual_leave_balance.used` / `.closing`)
        **마감(issued) 근무표**에서만 계산한다. draft 는 확정이 아니다.

    엑셀 표시 ('사용' / '당월잔여')
        **내려받는 그 근무표**에서 실시간으로 계산할 예정이다(draft 포함).
        수간호사가 "이대로 마감하면 잔여가 얼마가 되는지" 를 미리 봐야 하기 때문이다.
        → 그 용도로 `compute_leave_used()` 를 직접 부르고 장부의 `opening` 만 읽는다.

        ★ **아직 배선되지 않았다.** 엑셀 고도화(`excel_service.export_schedule_excel_bytes`)가
          이 함수를 부르는 것은 다음 단계다. 그때까지 `compute_leave_used()` 의 외부
          소비자는 없고 `rebuild_leave_balance_from()` 만 쓴다.

## 차감 대상은 코드가 아니라 **표식**으로 고른다

`shifts.annual_leave_unit` (NULL=대상아님 / 1.000 종일 / 0.500 반차)。
코드 문자열(`연`)을 박으면 병동마다 `FB`·`VY`·`연차` 로 갈려 동작하지 않는다.
`type='휴가'` 로도 못 가른다 — 전사 98종에 출산휴가·병가·교육까지 섞여 있다.

## 그룹웨어

`eun_gw` 를 **조회하지 않는다.** 연동은 아직 하지 않는다는 방침이고, 정렬은 스키마
모양(정밀도·키)으로만 맞춰 뒀다. 상세는 `NurseAnnualLeaveBalance` docstring 참조.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from db.models import (
    NurseAnnualLeaveBalance,
    Schedule,
    ScheduleEntry,
    Shift,
)

ZERO = Decimal("0")


def _prev_ym(year: int, month: int) -> tuple[int, int]:
    """직전 연월. `night_cycle_service._prev_ym` 과 같은 규약."""
    return (year - 1, 12) if month == 1 else (year, month - 1)


def compute_leave_used(db: Session, schedule: Schedule) -> dict[str, Decimal]:
    """그 근무표의 간호사별 연차 사용량. **draft 여도 계산한다.**

    엑셀 표시가 이 함수를 그대로 쓴다 — 확정 장부와 달리 "지금 이 안(案)" 기준이다.

    ★★ **SQL 조인을 쓰지 않는다.** `shifts` 는 PK·UNIQUE 가 없어 같은
      `(group_id, shift_id)` 가 여러 행 존재한다(실측: 중복 조합 **81개** — `D`·`E`·`N`·`O`
      가 각각 ×12). 코드로 조인하면 한 셀이 여러 행에 매칭돼 **사용량이 배수로 집계된다.**
      지금은 표식이 켜진 코드에 중복이 없어 드러나지 않지만, 다른 병동에 켜는 순간 터진다.

    ★ 그래서 우선순위를 둔다.
        ① `schedule_entries.id` → `shifts.id` : 저장 시 실제로 고른 그 행이 진실이다.
           그 행의 `annual_leave_unit` 이 NULL 이면 **대상이 아니다**(코드로 되돌아가지 않는다 —
           같은 코드의 다른 마스터 행 때문에 연차가 아닌 셀이 연차로 세어질 수 있다).
        ② `id` 가 없거나(전사 23,379셀) 고아면 코드로 폴백하되, 중복이면 `sequence`·`id`
           순의 **대표 1건**만 쓴다(결정적).
    """
    gid = str(schedule.group_id)
    shifts = (
        db.query(Shift)
        .filter(Shift.group_id == gid)
        .order_by(Shift.sequence.asc(), Shift.id.asc())
        .all()
    )
    by_id = {int(s.id): s.annual_leave_unit for s in shifts if s.id is not None}
    by_code: dict[str, object] = {}
    for s in shifts:                      # 정렬돼 있으므로 setdefault 가 곧 대표행
        code = str(s.shift_id or "").strip()
        if not code:
            continue
        # ★ `annual_leave_unit` 이 NULL 인 행도 **등록한다.** 걸러 버리면 대표행(첫 행)이
        #   연차가 아닐 때 뒤쪽 중복행이 대표 자리를 차지해, id 없는 셀이 통째로 연차로
        #   세어진다(전사 23,379셀). 대표행이 NULL 이면 "연차 아님" 이 정답이다.
        by_code.setdefault(code, s.annual_leave_unit)

    used: dict[str, Decimal] = {}
    for nurse_id, entry_shift_ref, code in db.query(
        ScheduleEntry.nurse_id, ScheduleEntry.id, ScheduleEntry.shift_id
    ).filter(ScheduleEntry.schedule_id == str(schedule.schedule_id)).all():
        if nurse_id is None:
            continue
        unit = None
        if entry_shift_ref is not None and int(entry_shift_ref) in by_id:
            unit = by_id[int(entry_shift_ref)]        # ① 저장된 그 행이 진실
        else:
            unit = by_code.get(str(code or "").strip())  # ② 폴백
        if unit is None:
            continue
        key = str(nurse_id)
        used[key] = used.get(key, ZERO) + Decimal(str(unit))
    return used


def _issued_by_month(db: Session, group_id: str, year: int, month: int) -> dict:
    """(year, month) 이상의 **살아있는 마감본**을 달별 1건으로. `night_cycle` 과 동일 규약.

    ★ `dropped=True` 는 제외한다 — `drop_schedule` 은 플래그만 세우고 `status` 는
      'issued' 로 남기므로, 안 거르면 **지운 근무표가 계속 차감된다.**
    ★ 같은 달에 issued 가 여러 건이면 **최신 1건만** 쓴다. 그대로 두면 같은 달을
      두 번 차감해 잔여가 깎인다.
    """
    def _pick_key(s: Schedule):
        # ★ `created_at` 은 nullable 이라 `(x or 0)` 로 비교하면 int 0 과 datetime 을
        #   견주다 TypeError 가 난다(레거시·임포트 행에 NULL 이 실재). 타입 안전한
        #   결정적 키로 고른다 — NULL 은 항상 뒤로, 동률이면 schedule_id 로 가른다.
        return (s.created_at is not None, s.created_at or datetime.min, str(s.schedule_id))

    per: dict[tuple[int, int], Schedule] = {}
    for s in (
        db.query(Schedule)
        .filter(
            Schedule.group_id == str(group_id),
            Schedule.status == "issued",
            Schedule.dropped == False,  # noqa: E712
        )
        .all()
    ):
        ym = (int(s.year), int(s.month))
        if ym < (int(year), int(month)):
            continue
        cur = per.get(ym)
        if cur is None or _pick_key(s) > _pick_key(cur):
            per[ym] = s
    return per


def rebuild_leave_balance_from(
    db: Session, group_id: str, year: int, month: int
) -> int:
    """(year, month) **부터 이후 모든 달**의 연차 장부를 다시 계산한다.

    ★ 왜 그 달만 갱신하면 안 되는가
      `opening` 은 전월 `closing` 을 이어받는다. 과거 달의 마감본이 바뀌면(재발행·
      마감취소·마감본 수정) 이후 달의 시작 잔여가 전부 틀어지므로 **연쇄로** 돌린다.

    ★ 잔액 행이 없는 간호사는 **만들지 않는다.** 전월 기록이 없으면 차감할 근거가 없고,
      집계도 하지 않는 것이 방침이다.

    ★ 마감본이 사라진 달은 행을 지우지 않고 `used`/`closing` 을 **NULL 로 되돌린다.**
      `opening` 은 시드이거나 전월에서 이어받은 값이라 살아 있어야 한다.
      (night_cycle 은 앵커 자체를 지우지만, 이쪽은 잔여가 남아야 하므로 다르다.)

    Returns:
        손댄 행 수.
    """
    gid = str(group_id)
    issued = _issued_by_month(db, gid, year, month)

    rows = (
        db.query(NurseAnnualLeaveBalance)
        .filter(NurseAnnualLeaveBalance.group_id == gid)
        .all()
    )
    if not rows:
        return 0

    by_cell = {
        (str(r.nurse_id), int(r.year), int(r.month)): r for r in rows
    }
    nurse_ids = sorted({str(r.nurse_id) for r in rows})

    # 대상 달 = 마감본이 있는 달 ∪ 이미 행이 있는 달, 모두 (year, month) 이상.
    months = {ym for ym in issued}
    months |= {
        (int(r.year), int(r.month))
        for r in rows
        if (int(r.year), int(r.month)) >= (int(year), int(month))
    }
    months_sorted = sorted(months)
    if not months_sorted:
        return 0

    # 달별 사용량은 한 번만 계산해 재사용한다(간호사 수만큼 재조회하지 않는다).
    used_by_month = {ym: compute_leave_used(db, sch) for ym, sch in issued.items()}

    # ★★ 재계산 시작월의 **직전 달** closing 을 먼저 읽는다.
    #   이게 없으면 "1월은 닫혀 있고 2월을 처음 마감" 하는 **정상 월 전환**에서
    #   2월 행이 없어 아무것도 만들어지지 않는다(Codex 지적·실재 결함).
    py, pm = _prev_ym(int(year), int(month))
    seed_closing: dict[str, Decimal | None] = {}
    for r in rows:
        if int(r.year) == py and int(r.month) == pm and r.closing is not None:
            seed_closing[str(r.nurse_id)] = Decimal(str(r.closing))

    office_id = str(rows[0].office_id)   # 같은 group 이면 office 는 하나다

    touched = 0
    for nid in nurse_ids:
        prev_closing: Decimal | None = seed_closing.get(nid)
        # 마지막으로 **처리한** 연월. 인접성 검사에 쓴다.
        last_ym: tuple[int, int] | None = (py, pm) if prev_closing is not None else None
        for ym in months_sorted:
            row = by_cell.get((nid, ym[0], ym[1]))
            sch = issued.get(ym)
            if row is None and sch is None:
                continue
            # ★★ `months_sorted` 에는 마감본도 행도 없는 달이 빠져 있어, 그냥 이어붙이면
            #   10월·12월만 있을 때 **12월이 10월 잔여를 물려받는다**(11월 사용분 증발).
            #   달력상 바로 앞 달을 처리한 경우에만 이어받고, 구멍이면 체인을 끊는다.
            #   11월이 나중에 마감되면 rebuild 가 다시 불려 그때 회복된다.
            if prev_closing is not None and last_ym is not None and _prev_ym(*ym) != last_ym:
                prev_closing = None
            if row is None:
                # 마감본은 있는데 그 달 행이 없다 → 전월 `closing` 을 이어받아 만든다.
                #   전월이 안 닫혔으면 시작 잔여를 모르므로 만들지 않는다.
                if prev_closing is None:
                    # 시작 잔여를 모른다 → 만들지 않는다. 뒤에 이 간호사의 시드 행이
                    # 나오면 거기서 독립 앵커로 다시 출발한다.
                    continue
                row = NurseAnnualLeaveBalance(
                    office_id=office_id,
                    group_id=gid,
                    nurse_id=nid,
                    year=ym[0],
                    month=ym[1],
                    opening=prev_closing,
                )
                row.source = "carried"
                db.add(row)
                by_cell[(nid, ym[0], ym[1])] = row
                opening_ok = True
            elif str(row.source or "") == "seed":
                # ★★ **시드는 승계보다 우선한다.** 사람이 명시적으로 정한 값이기 때문이다.
                #   연차는 **연 단위로 재부여**되므로, 1월 시드를 전년 12월 `closing` 으로
                #   덮으면 그 해 부여분이 통째로 사라지고 이후 달이 전부 어긋난다.
                #   이월이 필요하면 그건 1월 시드값 자체에 반영해 넣는다.
                #   ★ `source` 는 **opening 의 출처**다(시드냐 승계냐). 마감 여부를 여기
                #     담으면 재발행 때 시드 행이 덮여 앵커 자격을 잃는다.
                opening_ok = True
            elif prev_closing is not None:
                row.opening = prev_closing
                row.source = "carried"
                opening_ok = True
            else:
                # 승계 행(`carried`)인데 전월 `closing` 을 모른다 → 이 `opening` 은
                # 앞 달이 바뀐 지금 stale 일 수 있어 신뢰하지 않는다.
                opening_ok = False

            if sch is not None and opening_ok:
                u = used_by_month.get(ym, {}).get(nid, ZERO)
                row.used = u
                row.closing = Decimal(str(row.opening)) - u
                # ★ `source` 는 건드리지 않는다 — 위에서 정한 **opening 의 출처**이지
                #   마감 여부가 아니다. 마감 여부는 `source_schedule_id` 로 안다.
                row.source_schedule_id = str(sch.schedule_id)
            else:
                # ① 마감취소·삭제로 마감본이 없어졌거나
                # ② 앞 달이 안 닫혀 시작 잔여를 모른다(stale `opening` 으로 확정하면 안 된다).
                #    ★ 이후 달이 마감돼 있어도 확정하지 않는다 — 그 달이 다시 마감되면
                #      rebuild 가 다시 불려 그때부터 연쇄로 회복된다.
                row.used = None
                row.closing = None
                row.source_schedule_id = None
            touched += 1
            last_ym = ym
            prev_closing = row.closing

    # ★ night_cycle 과 같은 이유로 flush 한다 — 호출부가 여러 훅을 잇달아 부르고,
    #   세션에만 있는 값은 다음 조회가 못 본다.
    if touched:
        db.flush()
        print(
            f"[LeaveBalance] group={gid} {year}-{int(month):02d} 이후 "
            f"{len(months_sorted)}개월 · {len(nurse_ids)}명 연쇄 재계산 · {touched}행"
        )
    return touched
