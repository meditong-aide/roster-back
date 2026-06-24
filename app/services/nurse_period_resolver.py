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
            cur.valid_to = valid_from               # close-before-open
        kwargs: dict[str, Any] = {
            "nurse_id": nurse_id, "valid_from": valid_from, "valid_to": None,
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
