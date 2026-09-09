"""근무표 생성 후처리 — 고정근무자 휴직 가림막.

## 왜 필요한가 — 솔버 가림막이 고정근무자에게는 닿지 않는다

`nurse_assignment` 기반 차단은 이미 있다. 다만 **솔버 전용**이다 —
`roster_create_service.py:4990` 이 `build_blocked_days()` 로 `blocked_by_nurse_id` 를 만들어
`config_dict` 에 실어 CP-SAT 에 넘긴다. 그런데 `fixed_shift` 를 가진 사람은
`_split_fixed_nurses()` 로 갈라져 **솔버를 아예 타지 않고** 고정근무 전개(평일=fixed_shift,
주말=OFF)로 채워지므로 그 차단이 적용되지 않는다.

  실측(2026-08-18): 수술실 한주영 — `leave` 2026-08-15~11-30 을 `status='active'` 로
  되살린 뒤 재생성해도 `D1` 22 · `O` 8 그대로. 7·8월(엑셀 기반)만 `육휴` 였다.

그래서 저장 직전에 같은 규칙으로 한 번 더 덮는다.

## 판정은 `day_windows.build_blocked_days` 와 동일하게

★ `status == 'cancelled'` 는 반드시 건너뛴다. 취소된 휴직까지 가리면 **정상 근무자가
  근무표에서 사라진다**(2026-08-18 에 실제로 밟았다 — 한주영의 휴직은 등록 9분 뒤
  취소된 상태였는데 그것까지 가려 버렸다).
★ 종료일은 `end_date or expected_end_date` 순으로 본다. 둘 다 없으면 월말까지로 본다.
★ `source_group_id == group_id` 인 것만 — 타 병동 휴직이 이 병동 근무표를 가리면 안 된다.

## 어떤 코드로 덮는가

**그룹에 있는 코드만 쓴다.** 없으면 아무것도 하지 않는다 — 존재하지 않는 코드를 넣으면
`schedule_entries` 무결성이 깨진다(`shifts.call_base_id` 와 같은 원칙).
우선순위는 **직전 달에 그 사람이 실제로 쓰던 휴직 코드**다. `reason` 은 '휴직' 한 단어라
육아휴직인지 일반 휴직인지 구분되지 않는데, 직전 달 근무표에는 병원이 실제로 쓴 코드가
남아 있다(한주영 = `육휴`).
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

#: 직전 달 근거가 없을 때 쓰는 폴백 후보(그룹에 실제로 있는 것만 선택).
_FALLBACK_CODES = ("육휴", "휴직", "산휴", "출산")

#: `day_windows.build_blocked_days` 아웃바운드 분기와 동일한 사유 집합 중 휴직만.
_LEAVE_REASONS = ("휴직",)


def _leave_spans(
    db: Session, group_id: str, nurse_ids: list[str], m_start: date, m_end: date,
) -> dict[str, list[tuple[date, date]]]:
    """그 달에 걸치는 **유효** 휴직 구간 — 월 범위로 잘라서 돌려준다."""
    from db.models import NurseAssignment

    if not nurse_ids:
        return {}
    rows = db.query(NurseAssignment).filter(
        NurseAssignment.nurse_id.in_(nurse_ids),
        NurseAssignment.reason.in_(_LEAVE_REASONS),
    ).all()
    out: dict[str, list[tuple[date, date]]] = {}
    for a in rows:
        if str(a.status or "").strip() == "cancelled":
            continue                                  # ★ 취소된 휴직은 무시
        if str(a.source_group_id or "") != str(group_id):
            continue                                  # 이 병동 소속 건만
        a_start = a.start_date
        a_end = a.end_date or a.expected_end_date or m_end
        if a_start is None or a_end < m_start or a_start > m_end:
            continue
        out.setdefault(str(a.nurse_id), []).append(
            (max(a_start, m_start), min(a_end, m_end)))
    return out


def _resolve_leave_code(
    db: Session, group_id: str, nurse_id: str, year: int, month: int,
    available: set[str],
) -> Optional[str]:
    """덮을 코드 — 직전 달 실제 사용분 우선, 없으면 그룹 보유 폴백."""
    from db.models import Schedule, ScheduleEntry

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    sids = [s.schedule_id for s in db.query(Schedule).filter(
        Schedule.group_id == group_id,
        Schedule.year == prev_y, Schedule.month == prev_m).all()]
    if sids:
        used = [str(e.shift_id or "").strip() for e in db.query(ScheduleEntry).filter(
            ScheduleEntry.schedule_id.in_(sids),
            ScheduleEntry.nurse_id == nurse_id).all()]
        cand = [c for c in used if c in available and c in _FALLBACK_CODES]
        if cand:
            return max(set(cand), key=cand.count)
    for c in _FALLBACK_CODES:
        if c in available:
            return c
    return None


def postprocess_leave_mask(
    db: Session, schedule, generated: dict, current_user, req,
) -> dict:
    """고정근무 전개로 채워진 휴직자 셀을 휴직 코드로 덮는다."""
    from db.models import Shift

    if not isinstance(generated, dict) or not generated:
        return generated
    group_id = str(getattr(current_user, "group_id", "") or "")
    if not group_id:
        return generated

    year, month = int(req.year), int(req.month)
    days_in_month = calendar.monthrange(year, month)[1]
    m_start, m_end = date(year, month, 1), date(year, month, days_in_month)

    spans_by_nurse = _leave_spans(db, group_id, [str(k) for k in generated], m_start, m_end)
    if not spans_by_nurse:
        return generated

    available = {str(s.shift_id).strip() for s in
                 db.query(Shift).filter(Shift.group_id == group_id).all()}

    masked = 0
    for nid, spans in spans_by_nurse.items():
        days = generated.get(nid)
        if not isinstance(days, list):
            continue
        code = _resolve_leave_code(db, group_id, nid, year, month, available)
        if code is None:
            print(f"[LeaveMask] {nid}: 그룹에 휴직 코드가 없어 건너뜀 {list(_FALLBACK_CODES)}")
            continue
        hit = 0
        for lo, hi in spans:
            for d in range(lo.day, hi.day + 1):
                if 0 <= d - 1 < len(days) and str(days[d - 1] or "").strip() != code:
                    days[d - 1] = code
                    hit += 1
        if hit:
            masked += hit
            print(f"[LeaveMask] {nid} → {code} {hit}셀 "
                  f"({', '.join(f'{lo}~{hi}' for lo, hi in spans)})")
    if masked:
        print(f"[LeaveMask] 총 {masked}셀 — 고정근무 전개로 채워진 휴직자를 덮었다")
    return generated
