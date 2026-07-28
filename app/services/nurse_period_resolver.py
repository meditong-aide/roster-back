# app/services/nurse_period_resolver.py
"""간호사 시점 속성(effective-dated period) 제너릭 리졸버/upsert.

테이블별 코드 중복 없이 4종 period 테이블(grade/allowed_shift/weekendoff/fixedshift)을
같은 함수로 다룬다. 설계: docs/NURSE_ATTRIBUTE_PERIOD_DESIGN.md.

규칙(불변식):
    - `[valid_from, valid_to)` 반열림 · 겹침 금지 · gap(미지정) 허용
    - 변경 = close-before-open (옛 구간 valid_to 닫고 새 구간 open, 삭제 금지)
    - 진실 = period 테이블. `nurses` 캐시 컬럼 = 단방향 투영(앱 직접쓰기 금지) →
      upsert 가 같은 txn 에서 오늘값을 컬럼에 투영.

I/O: 읽기는 fetch_periods 로 테이블당 1쿼리(간호사 수 무관) 후 메모리에서 resolve.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import or_


def _today() -> date:
    return date.today()


# ── 읽기 ──────────────────────────────────────────────────────────────────────
def fetch_periods(db, Model, nurse_ids, month_start: date, month_end: date,
                  group_id: str | None = None) -> dict[str, list]:
    """대상 월[month_start, month_end)과 겹치는 구간을 간호사별로 bulk fetch.

    테이블당 1쿼리. 반환: {nurse_id: [row,...]} (valid_from 오름차순).
    group_id 주어지고 Model 에 group_id 있으면 ward 필터(team/grade).
    """
    if not nurse_ids:
        return {}
    q = db.query(Model).filter(
        Model.nurse_id.in_(list(nurse_ids)),
        Model.valid_from < month_end,
        or_(Model.valid_to.is_(None), Model.valid_to > month_start),
    )
    if group_id is not None and hasattr(Model, "group_id"):
        q = q.filter(Model.group_id == group_id)
    by_nurse: dict[str, list] = defaultdict(list)
    for r in q.order_by(Model.valid_from.asc()).all():
        by_nurse[str(r.nurse_id)].append(r)
    return by_nurse


def resolve_asof(rows_for_nurse: list | None, day: date, value_attr: str,
                 default: Any = None) -> Any:
    """그 간호사의 구간들에서 day 를 덮는 값(겹침 없음→최대 1개). gap=default."""
    for r in rows_for_nurse or []:
        if r.valid_from <= day and (r.valid_to is None or day < r.valid_to):
            return getattr(r, value_attr)
    return default


def is_weekend_off_asof(db, nurse_id, day: date | None = None) -> bool:
    """단건: nurse_id 의 주말휴무 여부(as-of day, 기본 today). SSOT=nurse_weekendoff_period.

    nurses.is_weekend_off 컬럼을 절대 읽지 않는다.
    """
    from datetime import timedelta
    from db.models import NurseWeekendOffPeriod
    if not nurse_id:
        return False
    d = day or date.today()
    # fetch_periods 는 valid_from < month_end 필터라, valid_from==d 구간 포함 위해 +1일.
    rows = fetch_periods(db, NurseWeekendOffPeriod, [str(nurse_id)], d, d + timedelta(days=1))
    return bool(resolve_asof(rows.get(str(nurse_id)), d, "weekend_off", default=None))


def weekend_off_ids_asof(db, nurse_ids, year: int, month: int) -> set[str]:
    """as-of (year, month) 기준 주말휴무 대상 nurse_id 집합.

    SSOT = nurse_weekendoff_period. nurses.is_weekend_off 컬럼에 의존하지 않는다.
    (컬럼은 단방향 캐시일 뿐이고, 도려내는 중이므로 읽기는 period 로 일원화.)
    """
    import calendar
    from db.models import NurseWeekendOffPeriod
    ids = [str(x) for x in (nurse_ids or []) if x is not None and str(x)]
    if not ids:
        return set()
    ms = date(year, month, 1)
    me = date(year, month, calendar.monthrange(year, month)[1])
    wp = fetch_periods(db, NurseWeekendOffPeriod, ids, ms, me)
    out: set[str] = set()
    for nid in ids:
        if resolve_asof(wp.get(nid), ms, "weekend_off", default=None):
            out.add(nid)
    return out


# ── 쓰기 ──────────────────────────────────────────────────────────────────────
def open_span_covering(db, Model, nurse_id: str, valid_from: date,
                       group_id: str | None = None):
    """valid_from 을 덮는 구간(valid_from 이후까지 걸친) 중 최신 1개. 없으면 None."""
    q = db.query(Model).filter(
        Model.nurse_id == nurse_id,
        Model.valid_from <= valid_from,
        or_(Model.valid_to.is_(None), Model.valid_to > valid_from),
    )
    if group_id is not None and hasattr(Model, "group_id"):
        q = q.filter(Model.group_id == group_id)
    return q.order_by(Model.valid_from.desc()).first()


def _next_span_start(db, Model, nurse_id: str, valid_from: date,
                     group_id: str | None = None) -> date | None:
    """valid_from 보다 뒤에서 시작하는 가장 가까운 구간의 시작일. 없으면 None.

    gap(앞쪽 미지정 구간)에 새 구간을 열 때, 뒤에 이미 있는 변경점을 삼키지 않도록
    새 구간의 valid_to 를 여기까지로 잘라준다.
    """
    q = db.query(Model).filter(
        Model.nurse_id == nurse_id,
        Model.valid_from > valid_from,
    )
    if group_id is not None and hasattr(Model, "group_id"):
        q = q.filter(Model.group_id == group_id)
    nxt = q.order_by(Model.valid_from.asc()).first()
    return nxt.valid_from if nxt is not None else None


def upsert_period(db, Model, nurse_id: str, valid_from: date, value_attr: str,
                  value: Any, group_id: str | None = None, *,
                  nurse=None, cache_attr: str | None = None,
                  source: str = "edited", today: date | None = None,
                  carry_attrs: list[str] | None = None):
    """valid_from 부터 value 로 변경(close-before-open). 단방향 캐시 투영 포함.

    - 같은 시작일 구간이 이미 있으면 제자리 갱신(empty span 방지).
    - 직전 열린 구간은 valid_to=valid_from 으로 닫음.
    - 동일값이면 no-op.
    - nurse+cache_attr 주어지고 valid_from<=today 면 nurses 컬럼에 동기 투영.
    - carry_attrs: 한 row 가 여러 value 컬럼을 갖는 satellite(예: allowed_shifts+fixed_shift)에서,
      새 구간을 열 때 명시 안 한 형제 컬럼을 직전 구간(cur)에서 승계(carry-forward)한다.
    반환: 생성/갱신된 row.
    """
    # 같은 txn 의 직전 pending 쓰기(예: 통합 satellite 의 allowed→fixed 순차 upsert)를
    # open_span_covering 이 보도록 강제 flush. SessionLocal(autoflush=False)에서도 자기일관.
    db.flush()
    cur = open_span_covering(db, Model, nurse_id, valid_from, group_id)

    if cur is not None and getattr(cur, value_attr) == value:
        row = cur                                   # 동일값 = no-op
    elif cur is not None and cur.valid_from == valid_from:
        setattr(cur, value_attr, value)             # 같은 시작일 → 제자리 갱신
        row = cur
    else:
        if cur is not None:
            # 분할: 새 구간은 기존 구간의 끝을 승계해야 뒤의 변경점(예: 8/1)을 보존한다.
            # 과거엔 valid_to=None 으로 열어 [6/1,8/1) 중간(7/1) 편집 시 새 [7/1,None) 이
            # 기존 [8/1,...) 를 덮어 8월 편집이 영영 안 보이는 겹침 버그가 났다.
            new_valid_to = cur.valid_to
            cur.valid_to = valid_from               # close-before-open
        else:
            # gap(앞쪽 미지정)에 새 구간 — 뒤에 이미 있는 변경점까지만 연다.
            new_valid_to = _next_span_start(db, Model, nurse_id, valid_from, group_id)
        kwargs: dict[str, Any] = {
            "nurse_id": nurse_id, "valid_from": valid_from, "valid_to": new_valid_to,
            value_attr: value, "source": source,
        }
        if group_id is not None and hasattr(Model, "group_id"):
            kwargs["group_id"] = group_id
        # 형제 value 컬럼 carry-forward (다컬럼 satellite)
        if carry_attrs and cur is not None:
            for _a in carry_attrs:
                if _a != value_attr:
                    kwargs.setdefault(_a, getattr(cur, _a))
        row = Model(**kwargs)
        db.add(row)

    # 단방향 동기 투영 (앱은 컬럼을 여기서만 갱신). 미래발효는 일일 roll job 이 처리.
    if nurse is not None and cache_attr and valid_from <= (today or _today()):
        setattr(nurse, cache_attr, value)
    return row
