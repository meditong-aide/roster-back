from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from schemas.daily_shift_schema import DailyShiftReplace, DailyTeamShiftReplace
from services.group_access import resolve_effective_group
from services.daily_shift_service import (
    get_or_init_month,
    replace_month_data,
    apply_bulk_to_days,
)
from services.daily_team_shift_service import (
    get_month_teams,
    replace_month_teams,
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
    - 반환: {office_id, group_id, year, month, month_summary, date, max_enabled}
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="인증 필요")
    # 그룹 스코프 가드: 토큰 group_id 대신 nurse_id→DB + hn_id 로 해석/검증(외부 병동 403).
    # office_id 도 caller 소속으로 고정해 (엉뚱한 office 로 seeding 되는 것 차단).
    target_group_id = resolve_effective_group(db, current_user, group_id)
    office_id = current_user.office_id
    try:
        return get_or_init_month(db, office_id, target_group_id, year, month)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"월 데이터 조회 실패: {e}")


@router.put("")
async def put_month(
    body: DailyShiftReplace,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """월 데이터를 일자별 배열로 교체합니다.
    - body 모양은 GET /daily-shift 응답과 동형. 저장 권위는 date 배열.
    - apply_globally=True 시 shift_manage 템플릿(RN 슬롯 1/2/3/5)을 day=1 값으로 동기화.
    - max_enabled=False 시 *_count_max 입력은 모두 0 으로 reset.
    - 응답: GET /daily-shift 와 동일 형태(저장 직후 재조회).
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="인증 필요")
    # 그룹 스코프 가드: body.group_id 를 해석/검증(외부 병동 403), office_id 는 caller 소속으로 고정.
    target_group_id = resolve_effective_group(db, current_user, body.group_id)
    office_id = current_user.office_id
    try:
        bulk = body.month_summary.model_dump() if body.month_summary is not None else None
        if body.apply_summary_to_days:
            return apply_bulk_to_days(
                db,
                office_id=office_id,
                group_id=target_group_id,
                year=body.year,
                month=body.month,
                bulk=bulk,
                apply_globally=body.apply_globally,
            )
        if body.date is None:
            raise HTTPException(status_code=400, detail="apply_summary_to_days=False 시 date 필드는 필수입니다.")
        d_list = body.date.D_count
        m_list = body.date.M_count if body.date.M_count else [0] * len(d_list)
        return replace_month_data(
            db,
            office_id=office_id,
            group_id=target_group_id,
            year=body.year,
            month=body.month,
            d_list=d_list,
            e_list=body.date.E_count,
            n_list=body.date.N_count,
            m_list=m_list,
            d_max_list=body.date.D_count_max,
            e_max_list=body.date.E_count_max,
            n_max_list=body.date.N_count_max,
            m_max_list=body.date.M_count_max,
            max_enabled=body.max_enabled,
            apply_globally=body.apply_globally,
            bulk=bulk,
        )
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"월 업데이트 실패: {e}")


@router.get("/teams")
async def get_month_active_teams(
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """일자별 가동 팀 조회.

    - 반환 date[i].teams 가 **None 이면 미설정 = 전 팀 가동**(현행 동작),
      리스트면 그 팀들만 가동한다(저장이 빈 목록을 거부하므로 항상 1개 이상).
    - 팀별 *_count 가 0 이면 인원 미지정 → min(필요인원, 가동팀수) 규칙에 위임.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="인증 필요")
    target_group_id = resolve_effective_group(db, current_user, group_id)
    office_id = current_user.office_id
    try:
        return get_month_teams(db, office_id, target_group_id, year, month)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"일자별 가동 팀 조회 실패: {e}")


@router.put("/teams")
async def put_month_active_teams(
    body: DailyTeamShiftReplace,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """일자별 가동 팀 저장(부분 교체).

    - body.days 에 **없는 날짜는 건드리지 않는다**. 특정 날짜를 미설정으로 되돌리려면
      그 날짜를 teams=null 로 명시해야 한다.
    - teams=[] 는 400. 저장하면 행 0개라 미설정과 구분이 안 되기 때문이다.
    - 비가동 팀 인원은 그날 강제 휴무가 되므로, 인원이 없는 팀을 지정하면 생성이
      실패할 수 있다. 막지는 않고 응답 warnings 로 알린다.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="인증 필요")
    target_group_id = resolve_effective_group(db, current_user, body.group_id)
    office_id = current_user.office_id
    try:
        return replace_month_teams(
            db,
            office_id=office_id,
            group_id=target_group_id,
            year=body.year,
            month=body.month,
            days=[d.model_dump() for d in body.days],
        )
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"일자별 가동 팀 저장 실패: {e}")
