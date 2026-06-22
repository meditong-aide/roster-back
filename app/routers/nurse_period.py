# app/routers/nurse_period.py
"""간호사 시점 속성(period) 쓰기 엔드포인트.

- POST /nurse-period/backfill : 현 nurses 캐시값을 period 테이블 첫 구간(open span)으로
  시드한다. 일회성 데이터 이행이지만 멱등(upsert_period)이라 재호출 안전.

설계: docs/NURSE_ATTRIBUTE_PERIOD_DESIGN.md (P2).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.client2 import get_db
from db.models import (
    Nurse,
    NurseGradePeriod,
    NurseAllowedShiftPeriod,
    NurseWeekendOffPeriod,
    NurseFixedShiftPeriod,
)
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.group_access import assert_caller_can_access_group
from services.nurse_period_resolver import upsert_period
from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes

router = APIRouter(prefix="/nurse-period", tags=["nurse-period"])


def _allowed_value(n: Nurse):
    # is_night_nurse(JSON) → 허용 근무형 집합으로 정규화(현 솔버와 동일 의미)
    return sorted(normalize_allowed_shift_codes(getattr(n, "is_night_nurse", None)))


# 속성별 backfill 스펙: model · 값컬럼 · ward귀속 · nurses→value 변환
_ATTR_SPECS: dict[str, dict] = {
    "allowed_shifts": dict(model=NurseAllowedShiftPeriod, value_attr="allowed_shifts",
                           group_bound=False, value_fn=_allowed_value),
    "weekend_off": dict(model=NurseWeekendOffPeriod, value_attr="weekend_off",
                        group_bound=False,
                        value_fn=lambda n: 1 if getattr(n, "is_weekend_off", False) else 0),
    "fixed_shift": dict(model=NurseFixedShiftPeriod, value_attr="fixed_shift",
                        group_bound=False,
                        value_fn=lambda n: getattr(n, "fixed_shift", None)),
    "grade": dict(model=NurseGradePeriod, value_attr="grade",
                  group_bound=True, value_fn=lambda n: getattr(n, "grade", None)),
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
                source="inherited",
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
