"""상호 근무 배제(mutual work exclusion) 관계 시점(period) SSOT 리졸버 + write-through 헬퍼.

진실 = `nurse_mutual_exclusion_period`. 프리셉티↔프리셉터 '함께근무'의 배반적 개념으로,
두 간호사를 **같은 날 같은 근무조에 배정하지 않도록** 솔버가 소프트 회피한다.
규칙: `[valid_from, valid_to)` 반열림 · 1인 1파트너(동시점 1 open) · 무기한 = valid_to NULL(지속).
**양방향 저장**: A↔B 설정 시 (A→B)·(B→A) 두 row 를 함께 open/close 하여 근무자관리 양쪽에 노출.
캐시 컬럼 없음(프리셉터와 달리 nurses 투영 없음) — 조회는 resolve_partner_asof 오버레이.

설계: `preceptee_period.py`(프리셉티 SSOT) 패턴 미러.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import or_

from db.models import NurseMutualExclusionPeriod as _MEP


def _today() -> date:
    return date.today()


def end_date_to_valid_to(end_date: date | None) -> date | None:
    """inclusive 종료일 → exclusive valid_to. 무기한이면 None(지속)."""
    return None if end_date is None else end_date + timedelta(days=1)


# ── 읽기 (as-of) ──────────────────────────────────────────────────────────────
def resolve_partner_asof(db, nurse_ids, as_of: date) -> dict[str, str | None]:
    """nurse_id → 그 시점 상호배제 partner_id (없으면 None). 양방향 저장이라 nurse_id 컬럼만 조회."""
    ids = [str(n) for n in (nurse_ids or [])]
    out: dict[str, str | None] = {nid: None for nid in ids}
    if not ids:
        return out
    rows = db.query(_MEP).filter(
        _MEP.nurse_id.in_(ids),
        _MEP.valid_from <= as_of,
        or_(_MEP.valid_to.is_(None), _MEP.valid_to > as_of),
    ).all()
    for r in rows:
        out[str(r.nurse_id)] = str(r.partner_id)
    return out


def resolve_mutual_exclusion_days_for_month(db, nurse_ids, year: int, month: int) -> dict[str, dict]:
    """대상월과 겹치는 상호배제 구간 → {nid: {"partner_id": pid, "days": set(0-based)}}.

    엔진 config map(`mutual_exclusion_by_nurse_id`) 빌드용. 겹치지 않으면 키 부재(= 그 달 배제 없음).
    days 는 그 달 내 0-based day index. 양방향 저장이라 nurse_id 컬럼만 조회하면 양쪽 다 잡힌다.
    """
    ids = [str(n) for n in (nurse_ids or [])]
    if not ids:
        return {}
    ms = date(year, month, 1)
    me = date(year, month, monthrange(year, month)[1])  # 월말(포함)
    rows = db.query(_MEP).filter(
        _MEP.nurse_id.in_(ids),
        _MEP.valid_from < me + timedelta(days=1),
        or_(_MEP.valid_to.is_(None), _MEP.valid_to > ms),
    ).order_by(_MEP.valid_from.asc()).all()
    out: dict[str, dict] = {}
    for r in rows:
        s = max(r.valid_from, ms)
        # valid_to 배타 → 마지막 포함일 = valid_to - 1day. 무기한이면 월말까지.
        e = me if r.valid_to is None else min(r.valid_to - timedelta(days=1), me)
        if e < s:
            continue
        days = set(range((s - ms).days, (e - ms).days + 1))
        cur = out.setdefault(str(r.nurse_id), {"partner_id": str(r.partner_id), "days": set()})
        cur["partner_id"] = str(r.partner_id)  # 최신 구간(valid_from 큰 것)이 최종
        cur["days"] |= days
    return out


# ── 쓰기 (write-through) ───────────────────────────────────────────────────────
def _open_one(db, *, nurse_id, partner_id, office_id, valid_from: date,
              valid_to: date | None = None, source: str = "edited"):
    """단방향 1 row open + 같은 간호사의 기존 open 구간을 새 시작일로 닫는다(1:1)."""
    nid, pid = str(nurse_id), str(partner_id)
    for r in db.query(_MEP).filter(
        _MEP.nurse_id == nid, _MEP.valid_to.is_(None),
    ).all():
        r.valid_to = max(valid_from, r.valid_from)  # 음수구간 방지(백데이팅 시)
        r.end_reason = r.end_reason or "replaced"
    row = _MEP(nurse_id=nid, partner_id=pid, office_id=str(office_id),
               valid_from=valid_from, valid_to=valid_to, source=source)
    db.add(row)
    return row


def _close_one(db, *, nurse_id, close_date: date, end_reason: str = "cancelled",
               partner_id=None) -> list:
    """단방향: nurse_id 의 close_date 이후까지 걸친 구간을 close_date(exclusive)로 절단."""
    nid = str(nurse_id)
    q = db.query(_MEP).filter(
        _MEP.nurse_id == nid,
        or_(_MEP.valid_to.is_(None), _MEP.valid_to > close_date),
    )
    if partner_id is not None:
        q = q.filter(_MEP.partner_id == str(partner_id))
    closed = []
    for r in q.all():
        r.valid_to = max(close_date, r.valid_from)  # 음수구간 방지
        r.end_reason = end_reason
        closed.append(r)
    return closed


def set_mutual_exclusion(db, *, nurse_id, partner_id, office_id,
                         valid_from: date | None = None, today: date | None = None) -> dict:
    """상호배제 파트너 설정(양방향·대칭 1:1). partner_id=None 이면 해제.

    - 설정: (nurse→partner)·(partner→nurse) 양방향 open. 각 측 이전 파트너의 역방향 orphan 도 정리
      (예: nurse 가 C→B 로 바꾸면 옛 파트너 A 쪽의 A→nurse row 도 닫아 대칭 유지).
    - 해제: nurse 의 현재 파트너 P 와의 양방향 close.
    반환: {"opened": n, "closed": n}.
    """
    db.flush()
    today = today or _today()
    valid_from = valid_from or today
    nid = str(nurse_id)
    opened = closed = 0
    # 발효일(valid_from) 기준 현재 파트너 — 역방향 orphan 정리 대상 판정(백/미래 데이팅 안전)
    cur = resolve_partner_asof(db, [nid], valid_from).get(nid)
    # ── 해제 ──
    if partner_id is None:
        closed += len(_close_one(db, nurse_id=nid, close_date=valid_from, end_reason="released"))
        if cur is not None:
            closed += len(_close_one(db, nurse_id=cur, close_date=valid_from,
                                     end_reason="released", partner_id=nid))
        return {"opened": opened, "closed": closed}
    pid = str(partner_id)
    if pid == nid:
        return {"opened": 0, "closed": 0}  # self-exclusion 무의미
    # ── 설정: 양측 이전 파트너의 역방향 orphan 정리 ──
    if cur and cur != pid:
        closed += len(_close_one(db, nurse_id=cur, close_date=valid_from,
                                 end_reason="replaced", partner_id=nid))
    curp = resolve_partner_asof(db, [pid], valid_from).get(pid)  # partner 의 발효일 기준 현재 파트너
    if curp and curp != nid:
        closed += len(_close_one(db, nurse_id=curp, close_date=valid_from,
                                 end_reason="replaced", partner_id=pid))
    # ── 양방향 open (각 _open_one 이 자기 측 close-before-open 처리) ──
    _open_one(db, nurse_id=nid, partner_id=pid, office_id=office_id, valid_from=valid_from)
    _open_one(db, nurse_id=pid, partner_id=nid, office_id=office_id, valid_from=valid_from)
    opened += 2
    return {"opened": opened, "closed": closed}
