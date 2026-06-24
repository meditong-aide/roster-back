# app/routers/nurse_period.py
"""간호사 시점 속성(period) 쓰기 엔드포인트.

- POST /nurse-period/backfill : 현 nurses 캐시값을 period 테이블 첫 구간(open span)으로
  시드한다. 일회성 데이터 이행이지만 멱등(upsert_period)이라 재호출 안전.

설계: docs/NURSE_ATTRIBUTE_PERIOD_DESIGN.md (P2).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.client2 import get_db
from db.models import (
    Nurse,
    NurseGradePeriod,
    NurseAllowedShiftPeriod,
    NurseWeekendOffPeriod,
)
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.group_access import assert_caller_can_access_group
from services.nurse_period_resolver import upsert_period, resolve_asof, fetch_periods
from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes

router = APIRouter(prefix="/nurse-period", tags=["nurse-period"])


def _allowed_value(n: Nurse):
    # is_night_nurse(JSON) → 허용 근무형 집합으로 정규화(현 솔버와 동일 의미)
    return sorted(normalize_allowed_shift_codes(getattr(n, "is_night_nurse", None)))


# 속성별 스펙: model · 값컬럼 · ward귀속 · nurses캐시컬럼(투영) · nurses→value 변환(backfill용)
#   · carry_attrs: 같은 satellite 의 형제 value 컬럼(새 구간 열 때 carry-forward)
# allowed_shifts 와 fixed_shift 는 한 테이블(nurse_allowed_shift_period)의 두 컬럼 = 결합 satellite.
_ATTR_SPECS: dict[str, dict] = {
    "allowed_shifts": dict(model=NurseAllowedShiftPeriod, value_attr="allowed_shifts",
                           group_bound=False, cache_attr="is_night_nurse",
                           value_fn=_allowed_value, carry_attrs=["fixed_shift"]),
    "fixed_shift": dict(model=NurseAllowedShiftPeriod, value_attr="fixed_shift",
                        group_bound=False, cache_attr="fixed_shift",
                        value_fn=lambda n: getattr(n, "fixed_shift", None),
                        carry_attrs=["allowed_shifts"]),
    "weekend_off": dict(model=NurseWeekendOffPeriod, value_attr="weekend_off",
                        group_bound=False, cache_attr="is_weekend_off",
                        value_fn=lambda n: 1 if getattr(n, "is_weekend_off", False) else 0),
    "grade": dict(model=NurseGradePeriod, value_attr="grade",
                  group_bound=True, cache_attr="grade",
                  value_fn=lambda n: getattr(n, "grade", None)),
}


class BackfillRequest(BaseModel):
    group_id: Optional[str] = None
    valid_from: Optional[date] = None          # 첫 구간 시작일(기본=오늘)
    attributes: Optional[list[str]] = None     # 기본=전체 4종


class BackfillResult(BaseModel):
    group_id: str
    valid_from: date
    nurse_count: int
    rows: dict[str, int]                        # 속성 → backfill 후 해당 간호사들의 period row 수


@router.post("/backfill", response_model=BackfillResult)
async def backfill_nurse_periods(
    payload: BackfillRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """그룹 소속 active 간호사의 현재 속성값을 period 첫 구간으로 시드(멱등)."""
    group_id = payload.group_id or getattr(current_user, "group_id", None)
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id 가 필요합니다")
    assert_caller_can_access_group(db, current_user, group_id)

    attrs = payload.attributes or list(_ATTR_SPECS.keys())
    unknown = [a for a in attrs if a not in _ATTR_SPECS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"알 수 없는 속성: {unknown}")

    valid_from = payload.valid_from or date.today()
    nurses = (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )

    for n in nurses:
        for a in attrs:
            spec = _ATTR_SPECS[a]
            upsert_period(
                db, spec["model"], n.nurse_id, valid_from,
                spec["value_attr"], spec["value_fn"](n),
                group_id=group_id if spec["group_bound"] else None,
                source="inherited", carry_attrs=spec.get("carry_attrs"),
            )
    db.commit()

    nurse_ids = [n.nurse_id for n in nurses]
    rows = {
        a: (
            db.query(_ATTR_SPECS[a]["model"])
            .filter(_ATTR_SPECS[a]["model"].nurse_id.in_(nurse_ids))
            .count()
            if nurse_ids else 0
        )
        for a in attrs
    }
    return BackfillResult(group_id=group_id, valid_from=valid_from,
                          nurse_count=len(nurses), rows=rows)


class ChangeRequest(BaseModel):
    attribute: str                             # allowed_shifts | weekend_off | fixed_shift | grade
    nurse_id: str
    valid_from: date                           # 이 날부터 value 적용(close-before-open)
    value: Any = None                          # allowed_shifts=list / weekend_off=0|1 / fixed_shift=str / grade=int
    group_id: Optional[str] = None             # grade(ward귀속)에 필요
    note: Optional[str] = None


class ChangeResult(BaseModel):
    attribute: str
    nurse_id: str
    valid_from: date
    value: Any
    today_value: Any                           # 변경 후 오늘 기준 as-of 값(투영 확인용)


@router.post("/change", response_model=ChangeResult)
async def change_nurse_period(
    payload: ChangeRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """한 간호사의 한 속성을 valid_from 부터 변경(close-before-open) + 단방향 캐시 투영."""
    spec = _ATTR_SPECS.get(payload.attribute)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"알 수 없는 속성: {payload.attribute}")

    nurse = db.query(Nurse).filter(Nurse.nurse_id == payload.nurse_id).first()
    if nurse is None:
        raise HTTPException(status_code=404, detail=f"간호사 없음: {payload.nurse_id}")

    # 권한: ward귀속(grade)은 명시 group_id, 그 외는 간호사 소속 그룹 기준
    group_id = payload.group_id or getattr(nurse, "group_id", None)
    assert_caller_can_access_group(db, current_user, group_id)

    if payload.attribute == "allowed_shifts" and not isinstance(payload.value, list):
        raise HTTPException(status_code=400, detail="allowed_shifts 는 리스트여야 합니다")

    # cross-attribute 모순 검증(저장 시점 hard gate): 그 달 월한도·고정근무와 충돌이면 422.
    if payload.attribute == "allowed_shifts":
        from services.nurse_period_validator import validate_allowed_shift_period
        issues = validate_allowed_shift_period(
            db, payload.nurse_id, group_id, payload.value, payload.valid_from,
        )
        blocking = [i for i in issues if i.get("severity", "blocking") == "blocking"]
        if blocking:
            raise HTTPException(status_code=422, detail={
                "message": "허용 근무형이 월 한도/고정근무 설정과 모순됩니다.",
                "issues": blocking,
            })

    upsert_period(
        db, spec["model"], payload.nurse_id, payload.valid_from,
        spec["value_attr"], payload.value,
        group_id=group_id if spec["group_bound"] else None,
        nurse=nurse, cache_attr=spec["cache_attr"], source="edited",
        carry_attrs=spec.get("carry_attrs"),
    )
    db.commit()

    # 변경 후 오늘 기준 as-of 값(미래발효면 gap/직전 구간, 캐시 투영 검증용)
    rows = (
        db.query(spec["model"]).filter(spec["model"].nurse_id == payload.nurse_id)
        .order_by(spec["model"].valid_from.asc()).all()
    )
    today_value = resolve_asof(rows, date.today(), spec["value_attr"], default=None)

    return ChangeResult(attribute=payload.attribute, nurse_id=payload.nurse_id,
                        valid_from=payload.valid_from, value=payload.value,
                        today_value=today_value)


class RollRequest(BaseModel):
    group_id: Optional[str] = None
    as_of: Optional[date] = None               # 기준일(기본=오늘). 미래발효 발효일 처리용
    attributes: Optional[list[str]] = None


class RollResult(BaseModel):
    group_id: str
    as_of: date
    nurse_count: int
    updated: dict[str, int]                    # 속성 → 캐시값이 바뀐 간호사 수


@router.post("/roll", response_model=RollResult)
async def roll_nurse_cache(
    payload: RollRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """as_of(기본 오늘) 기준 period 값을 nurses 캐시 컬럼에 투영(단방향 동기화).

    미래발효 변경이 발효일에 캐시에 반영되도록 일일 호출(cron)용. 멱등.
    구간이 as_of 를 안 덮으면(gap) 캐시는 건드리지 않는다.
    """
    group_id = payload.group_id or getattr(current_user, "group_id", None)
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id 가 필요합니다")
    assert_caller_can_access_group(db, current_user, group_id)

    attrs = payload.attributes or list(_ATTR_SPECS.keys())
    unknown = [a for a in attrs if a not in _ATTR_SPECS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"알 수 없는 속성: {unknown}")

    as_of = payload.as_of or date.today()
    nurses = (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )
    nurse_ids = [n.nurse_id for n in nurses]
    nurse_by_id = {n.nurse_id: n for n in nurses}

    updated = {a: 0 for a in attrs}
    for a in attrs:
        spec = _ATTR_SPECS[a]
        by_nurse = fetch_periods(
            db, spec["model"], nurse_ids, as_of, as_of + timedelta(days=1),
            group_id=group_id if spec["group_bound"] else None,
        ) if nurse_ids else {}
        for nid, rows in by_nurse.items():
            val = resolve_asof(rows, as_of, spec["value_attr"], default=None)
            if val is None:
                continue                       # gap → 캐시 유지
            n = nurse_by_id.get(nid)
            if n is not None and getattr(n, spec["cache_attr"], None) != val:
                setattr(n, spec["cache_attr"], val)
                updated[a] += 1
    db.commit()

    return RollResult(group_id=group_id, as_of=as_of,
                      nurse_count=len(nurses), updated=updated)
