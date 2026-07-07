"""프리셉터↔프리셉티 관계 시점(period) SSOT 리졸버 + write-through 헬퍼.

진실 = `nurse_preceptee_period`. `nurses.preceptor_id` = as-of 오늘 단방향 투영(앱 직접쓰기 금지).
규칙: `[valid_from, valid_to)` 반열림(valid_to 배타) · 겹침 금지 · 무기한 = valid_to NULL.

주의(경계 시맨틱): assignment.expected_end_date 는 **포함(inclusive)** 의미였다
(기존 roster_create_service step1 의 day range 가 _a_end 를 포함). 이 테이블의 valid_to 는
**배타(exclusive)** 이므로, 쓰기 호출부에서 valid_to = expected_end_date + 1day 로 변환해 저장한다.
→ 종료월(예: 7/14 종료) 다음달(8월) as-of 조회 시 자연히 제외되어 follow 누수 버그가 구조적으로 불가능.

설계: docs/NURSE_PRECEPTEE_PERIOD_DESIGN.md.
"""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import or_

from db.models import NursePrecepteePeriod, Nurse


def _today() -> date:
    return date.today()


def end_date_to_valid_to(expected_end_date: date | None) -> date | None:
    """assignment 의 inclusive 종료일 → period 의 exclusive valid_to. 무기한이면 None."""
    return None if expected_end_date is None else expected_end_date + timedelta(days=1)


# ── 읽기 (as-of) ──────────────────────────────────────────────────────────────
def _overlap_filter(q, day_lo: date, day_hi_excl: date):
    return q.filter(
        NursePrecepteePeriod.valid_from < day_hi_excl,
        or_(NursePrecepteePeriod.valid_to.is_(None),
            NursePrecepteePeriod.valid_to > day_lo),
    )


def _cancelled_source_assignment_ids(db, rows) -> set:
    """period rows 중 source_assignment_id 가 **cancelled 프리셉티 assignment** 를 가리키는
    것들의 assignment id 집합. → 취소된 관계는 솔버·화면에 주입하지 않는다(assignment 상태가 진실).
    valid_to close 여부와 무관하게 배제하므로 write-through 지연/누락에도 견고하다."""
    sids = {r.source_assignment_id for r in rows if r.source_assignment_id is not None}
    if not sids:
        return set()
    from db.models import NurseAssignment
    return {row[0] for row in db.query(NurseAssignment.id).filter(
        NurseAssignment.id.in_(list(sids)),
        NurseAssignment.status == "cancelled",
    ).all()}


def resolve_preceptor_asof(db, nurse_ids, as_of: date) -> dict[str, str | None]:
    """프리셉티 nurse_id → 그 시점 preceptor_id (관계 없으면 None).

    cancelled 프리셉티 assignment 로 취소된 관계는 제외한다(솔버·화면 공통 게이트).
    """
    ids = [str(n) for n in (nurse_ids or [])]
    out: dict[str, str | None] = {nid: None for nid in ids}
    if not ids:
        return out
    rows = db.query(NursePrecepteePeriod).filter(
        NursePrecepteePeriod.nurse_id.in_(ids),
        NursePrecepteePeriod.valid_from <= as_of,
        or_(NursePrecepteePeriod.valid_to.is_(None),
            NursePrecepteePeriod.valid_to > as_of),
    ).all()
    _cancelled = _cancelled_source_assignment_ids(db, rows)
    for r in rows:
        if r.source_assignment_id in _cancelled:
            continue
        out[str(r.nurse_id)] = str(r.preceptor_id)
    return out


def resolve_preceptees_asof(db, preceptor_ids, as_of: date) -> dict[str, list[str]]:
    """프리셉터 nurse_id → 그 시점 프리셉티 nurse_id 리스트.

    cancelled 프리셉티 assignment 로 취소된 관계는 제외(resolve_preceptor_asof 미러).
    """
    ids = [str(p) for p in (preceptor_ids or [])]
    if not ids:
        return {}
    rows = db.query(NursePrecepteePeriod).filter(
        NursePrecepteePeriod.preceptor_id.in_(ids),
        NursePrecepteePeriod.valid_from <= as_of,
        or_(NursePrecepteePeriod.valid_to.is_(None),
            NursePrecepteePeriod.valid_to > as_of),
    ).all()
    _cancelled = _cancelled_source_assignment_ids(db, rows)
    out: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r.source_assignment_id in _cancelled:
            continue
        out[str(r.preceptor_id)].append(str(r.nurse_id))
    return dict(out)


def resolve_preceptee_days_for_month(db, nurse_ids, year: int, month: int) -> dict[str, dict]:
    """대상월과 겹치는 프리셉티 구간 → {nid: {"preceptor_id": pid, "days": set(0-based)}}.

    엔진 config map(`preceptee_period_by_nurse_id`) 빌드용. 겹치지 않으면 키 자체가 없다
    (= 그 달 프리셉티 아님 = 독립 배정). days 는 그 달 내 0-based day index.

    cancelled 프리셉티 assignment 로 취소된 관계는 제외한다 — 취소된 관계는 솔버에 주입하지
    않는다(assignment 상태가 진실, valid_to close 여부 무관).
    """
    ids = [str(n) for n in (nurse_ids or [])]
    if not ids:
        return {}
    ms = date(year, month, 1)
    me = date(year, month, monthrange(year, month)[1])  # 월말(포함)
    # valid_from 오름차순 → 월중 프리셉터 교체(드묾) 시 days 는 합집합, preceptor_id 는
    # 가장 늦게 시작한(=최신) 구간 것으로 결정(deterministic last-wins).
    rows = _overlap_filter(
        db.query(NursePrecepteePeriod).filter(NursePrecepteePeriod.nurse_id.in_(ids)),
        ms, me + timedelta(days=1),
    ).order_by(NursePrecepteePeriod.valid_from.asc()).all()
    _cancelled = _cancelled_source_assignment_ids(db, rows)
    out: dict[str, dict] = {}
    for r in rows:
        # 취소된 관계(source assignment=cancelled) 제외 — 솔버에 주입 안 함.
        if r.source_assignment_id in _cancelled:
            continue
        s = max(r.valid_from, ms)
        # valid_to 배타 → 마지막 포함일 = valid_to - 1day. 무기한이면 월말까지.
        e = me if r.valid_to is None else min(r.valid_to - timedelta(days=1), me)
        if e < s:
            continue
        days = set(range((s - ms).days, (e - ms).days + 1))
        cur = out.setdefault(str(r.nurse_id), {"preceptor_id": str(r.preceptor_id), "days": set()})
        cur["preceptor_id"] = str(r.preceptor_id)  # 최신 구간(valid_from 큰 것)이 최종
        cur["days"] |= days
    return out


# ── 쓰기 (write-through) ───────────────────────────────────────────────────────
def open_preceptee_period(db, *, nurse_id, preceptor_id, office_id, valid_from: date,
                          valid_to: date | None = None, source_assignment_id: int | None = None,
                          source: str = "edited", nurse=None, today: date | None = None):
    """새 프리셉티 구간 open + as-of 캐시 투영. 기존 open 구간이 있으면 닫고 연다(1:1)."""
    db.flush()
    today = today or _today()
    nid, pid = str(nurse_id), str(preceptor_id)
    # 1:1 방어: 같은 간호사의 기존 open 구간을 새 시작일로 닫는다.
    for r in db.query(NursePrecepteePeriod).filter(
        NursePrecepteePeriod.nurse_id == nid,
        NursePrecepteePeriod.valid_to.is_(None),
    ).all():
        r.valid_to = max(valid_from, r.valid_from)  # 음수구간 방지(백데이팅 시)
        r.end_reason = r.end_reason or "released"
    row = NursePrecepteePeriod(
        nurse_id=nid, preceptor_id=pid, office_id=str(office_id),
        valid_from=valid_from, valid_to=valid_to,
        source=source, source_assignment_id=source_assignment_id,
    )
    db.add(row)
    # 단방향 캐시 투영: 오늘이 구간 내면 set. 미래발효/이미종료는 투영 안 함(roll/close 가 처리).
    if nurse is not None and valid_from <= today and (valid_to is None or today < valid_to):
        nurse.preceptor_id = pid
    return row


def close_preceptee_period(db, *, nurse_id, close_date: date, end_reason: str = "cancelled",
                           preceptor_id=None, nurse=None, today: date | None = None):
    """간호사의 open 프리셉티 구간을 close_date(exclusive valid_to)로 닫는다. 캐시 투영."""
    db.flush()
    today = today or _today()
    nid = str(nurse_id)
    # close_date 이후까지 걸친 구간(open/finite-covering/future)을 close_date 로 절단.
    #   future 구간(valid_from>close_date)은 max() 가드로 zero-width → 사실상 void(cancel 대응).
    #   이미 close_date 이전에 끝난 구간은 미매칭(불변).
    q = db.query(NursePrecepteePeriod).filter(
        NursePrecepteePeriod.nurse_id == nid,
        or_(NursePrecepteePeriod.valid_to.is_(None),
            NursePrecepteePeriod.valid_to > close_date),
    )
    if preceptor_id is not None:
        q = q.filter(NursePrecepteePeriod.preceptor_id == str(preceptor_id))
    closed = []
    for r in q.all():
        r.valid_to = max(close_date, r.valid_from)  # 음수구간 방지
        r.end_reason = end_reason
        closed.append(r)
    # 캐시 투영: 관계 종료(close<=today)면 None. period row 유무와 무관
    # (전환기 asymmetry: 캐시만 있고 period 미생성인 경우도 캐시는 비워야 함).
    if nurse is not None and close_date <= today:
        nurse.preceptor_id = None
    return closed


def backfill_from_assignments(db, *, today: date | None = None) -> dict:
    """기존 active 프리셉티 NurseAssignment + nurses.preceptor_id → period row 백필.

    멱등: 이미 open 구간이 있는 간호사는 skip. 권위 소스 = assignment 날짜
    (valid_from=start_date, valid_to=expected_end_date+1day 배타, 무기한이면 NULL).
    cache-only 비대칭(preceptor_id set인데 active 프리셉티 assignment 없음)은
    valid_from 을 알 수 없어 **생성하지 않고 로그만** 남긴다(잘못된 이력 방지 — flush/orphan-cleanup 이 캐시 정리).
    반환: {"inserted", "skipped", "cache_orphans"}.
    """
    from db.models import NurseAssignment
    inserted = skipped = 0
    rows = (
        db.query(NurseAssignment, Nurse)
        .join(Nurse, Nurse.nurse_id == NurseAssignment.nurse_id)
        .filter(
            NurseAssignment.reason == "프리셉티",
            NurseAssignment.status == "active",
            Nurse.preceptor_id.isnot(None),
        )
        .all()
    )
    opened_this_run: set[str] = set()
    for a, n in rows:
        nid = str(a.nurse_id)
        # 멱등: 이 assignment 로 이미 만든 period 가 있으면 skip (open/closed 무관).
        if db.query(NursePrecepteePeriod.id).filter(
            NursePrecepteePeriod.source_assignment_id == a.id,
        ).first():
            skipped += 1
            continue
        valid_to = end_date_to_valid_to(a.expected_end_date)
        # 1:1 방어: 이미 open 구간(다른 active assignment 포함)이 있으면 새 open 생성 금지
        #   (둘째 active 프리셉티 assignment = 더티 데이터 → filtered-unique 위반/abort 방지).
        if valid_to is None:
            has_open = nid in opened_this_run or db.query(NursePrecepteePeriod.id).filter(
                NursePrecepteePeriod.nurse_id == nid,
                NursePrecepteePeriod.valid_to.is_(None),
            ).first() is not None
            if has_open:
                skipped += 1
                continue
            opened_this_run.add(nid)
        db.add(NursePrecepteePeriod(
            nurse_id=nid, preceptor_id=str(n.preceptor_id), office_id=str(a.office_id),
            valid_from=a.start_date, valid_to=valid_to,
            source="backfill", source_assignment_id=a.id,
        ))
        inserted += 1
    # cache-only 비대칭: preceptor_id set인데 period row 자체가 전무(생성 안 함, 수동검토).
    cache_orphans = [
        str(n.nurse_id) for n in db.query(Nurse).filter(Nurse.preceptor_id.isnot(None)).all()
        if not db.query(NursePrecepteePeriod.id).filter(
            NursePrecepteePeriod.nurse_id == str(n.nurse_id)).first()
    ]
    return {"inserted": inserted, "skipped": skipped, "cache_orphans": cache_orphans}


def close_all_for_preceptor(db, *, preceptor_id, close_date: date,
                            end_reason: str = "preceptor_transfer", today: date | None = None):
    """프리셉터의 모든 open 프리셉티 구간 일괄 close (병동이동 등). 각 프리셉티 캐시 투영."""
    db.flush()
    today = today or _today()
    pid = str(preceptor_id)
    rows = db.query(NursePrecepteePeriod).filter(
        NursePrecepteePeriod.preceptor_id == pid,
        NursePrecepteePeriod.valid_to.is_(None),
    ).all()
    closed = []
    for r in rows:
        r.valid_to = max(close_date, r.valid_from)  # 음수구간 방지
        r.end_reason = end_reason
        closed.append(r)
        if close_date <= today:
            nu = db.query(Nurse).filter(Nurse.nurse_id == r.nurse_id).first()
            if nu is not None:
                nu.preceptor_id = None
    return closed
