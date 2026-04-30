from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Dict, List

from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from schemas.daily_shift_schema import (
    DailyShiftMonthQuery,
    DailyShiftMonthlyUpdate,
    DailyShiftDailyUpdate,
    CalendarUpdateRequest,
)
from db.models import DailyShift
from services.daily_shift_service import (
    get_or_init_month,
    update_monthly,
    update_daily,
    manage_month_data,
    is_month_closed,
)

router = APIRouter(prefix="/daily-shift", tags=["daily-shift"])


@router.get("")
async def get_month(
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """월 데이터 조회(없으면 shift_manage 기반으로 생성).
    - 쿼리: office_id, group_id, year, month
    - 반환: month_summary + 일자별 리스트
    """
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="인증 필요")
        # return {get_or_init_month(db, office_id, group_id, year, month)}
        return get_or_init_month(db, office_id, group_id, year, month)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"월 데이터 조회 실패: {e}")


@router.put("/monthly")
async def put_monthly(
    body: DailyShiftMonthlyUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """월 전체 일괄 업데이트. shift_manage 템플릿과 daily_shift 월 데이터를 동기화합니다."""
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="인증 필요")
        result = update_monthly(
            db,
            office_id=body.office_id,
            group_id=body.group_id,
            year=body.year,
            month=body.month,
            day_cnt=body.day,
            eve_cnt=body.evening,
            nig_cnt=body.night,
            mid_cnt=body.mid,
            day_max=body.day_max,
            eve_max=body.evening_max,
            nig_max=body.night_max,
            mid_max=body.mid_max,
            apply_globally=body.apply_globally,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"월 업데이트 실패: {e}")


@router.put("/daily")
async def put_daily(
    body: DailyShiftDailyUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """일자별 배열 업데이트.
    - body: {D:[], E:[], N:[], M:[], D_max:[], E_max:[], N_max:[], M_max:[]}
    - *_max 미지정(빈 리스트) 시 0(상한 미설정)으로 저장.
    """
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="인증 필요")
        result = update_daily(
            db,
            office_id=body.office_id,
            group_id=body.group_id,
            year=body.year,
            month=body.month,
            d_list=body.D,
            e_list=body.E,
            n_list=body.N,
            m_list=body.M if body.M else [0] * len(body.D),
            d_max_list=body.D_max,
            e_max_list=body.E_max,
            n_max_list=body.N_max,
            m_max_list=body.M_max,
        )
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"일별 업데이트 실패: {e}")


# Daily-Shift 작업
@router.get("/calendar")
async def get_calendar(
    office_id: str = Query(..., description="오피스 ID"),
    group_id: str = Query(..., description="그룹 ID"),
    year: int = Query(..., description="연도"),
    month: int = Query(..., description="월"),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """다중 월 캘린더 데이터 조회.
    - 쿼리: office_id, group_id, year, month
    - 반환: 마감된 데이터와 미마감 최소값 월 데이터 (format_shift_calendar 형식)
    - 인증 필요: current_user 확인
    """
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="인증 필요")
        if not current_user.is_master_admin and (not hasattr(current_user, 'office_id') or current_user.office_id != office_id):
            raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")
        return manage_month_data(db, office_id, group_id, year, month, initialize=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"캘린더 데이터 조회 실패: {e}")

@router.post("/save-calendar")
async def save_calendar(
    request: CalendarUpdateRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """다중 월 캘린더 데이터 저장.
    - body: {office_id, group_id, years: {year: {month: [{day, d_count, e_count, n_count}]}}}
    - 반환: 저장 후 업데이트된 캘린더 데이터
    - 인증 필요: current_user 확인
    - 마감된 데이터는 업데이트 불가
    """
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="인증 필요")
        if not current_user.is_master_admin and (not hasattr(current_user, 'office_id') or current_user.office_id != request.office_id):
            raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

        office_id = request.office_id
        group_id = request.group_id
        year: int | None = None
        month: int | None = None

        for y, months_data in request.years.items():
            year = int(y)
            for m, shifts in months_data.items():
                month = int(m)
                # 마감된 월은 업데이트 무시
                if is_month_closed(db, office_id, group_id, year, month):
                    continue
                for shift in shifts:
                    day = shift.get("day")
                    if not day:
                        continue
                    d_count = shift.get("d_count", 0)
                    e_count = shift.get("e_count", 0)
                    n_count = shift.get("n_count", 0)
                    m_count = shift.get("m_count", 0)
                    # *_count_max 미지정(=0) 은 상한 미설정. max < min 케이스는 min 으로 clamp.
                    def _clamp_max(mx_raw: int, mn: int) -> int:
                        mx = int(mx_raw or 0)
                        if mx <= 0:
                            return 0
                        return mx if mx >= mn else mn
                    d_count_max = _clamp_max(shift.get("d_count_max", 0), int(d_count or 0))
                    e_count_max = _clamp_max(shift.get("e_count_max", 0), int(e_count or 0))
                    n_count_max = _clamp_max(shift.get("n_count_max", 0), int(n_count or 0))
                    m_count_max = _clamp_max(shift.get("m_count_max", 0), int(m_count or 0))

                    existing = (
                        db.query(DailyShift)
                        .filter(
                            DailyShift.office_id == office_id,
                            DailyShift.group_id == group_id,
                            DailyShift.year == year,
                            DailyShift.month == month,
                            DailyShift.day == day
                        )
                        .first()
                    )
                    if existing:
                        existing.d_count = max(0, d_count)
                        existing.e_count = max(0, e_count)
                        existing.n_count = max(0, n_count)
                        existing.m_count = max(0, m_count)
                        existing.d_count_max = d_count_max
                        existing.e_count_max = e_count_max
                        existing.n_count_max = n_count_max
                        existing.m_count_max = m_count_max
                    else:
                        db.add(
                            DailyShift(
                                office_id=office_id,
                                group_id=group_id,
                                year=year,
                                month=month,
                                day=day,
                                d_count=d_count,
                                e_count=e_count,
                                n_count=n_count,
                                m_count=m_count,
                                d_count_max=d_count_max,
                                e_count_max=e_count_max,
                                n_count_max=n_count_max,
                                m_count_max=m_count_max,
                            )
                        )
        db.commit()
        if year is None or month is None:
            raise HTTPException(status_code=400, detail="저장할 캘린더 데이터가 없습니다.")
        return manage_month_data(db, office_id, group_id, year, month)
    except HTTPException as e:
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"캘린더 데이터 저장 실패: {e}")
