from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from db.client2 import get_db
from schemas.auth_schema import User as UserSchema
from routers.auth import get_current_user_from_cookie
from schemas.weekly_off_schema import (
    WeeklyOffSettingUpdate,
    WeeklyOffSettingResponse,
    WeeklyOffNurseUpdatePayload,
    WeeklyOffNurseListResponse
)
from services.weekly_off_service import (
    get_weekly_off_settings_service,
    update_weekly_off_settings_service,
    get_nurses_weekly_off_service,
    update_nurses_weekly_off_service,
    get_my_weekly_off_service
)

router = APIRouter(
    prefix="/weekly-off",
    tags=["weekly-off"]
)

@router.get("/settings", response_model=WeeklyOffSettingResponse)
async def get_weekly_off_settings(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    주휴 설정 조회 (수간호사/관리자)
    """
    return get_weekly_off_settings_service(current_user, db, group_id)

@router.put("/settings", response_model=WeeklyOffSettingResponse)
async def update_weekly_off_settings(
    payload: WeeklyOffSettingUpdate,
    group_id: Optional[str] = Query(None),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    주휴 설정 저장 (수간호사/관리자)
    """
    return update_weekly_off_settings_service(payload, current_user, db, group_id)

@router.get("/nurses", response_model=WeeklyOffNurseListResponse)
async def get_nurses_weekly_off(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    간호사 주휴 설정 목록 및 미리보기 조회
    """
    return get_nurses_weekly_off_service(year, month, current_user, db, group_id)

@router.put("/nurses")
async def update_nurses_weekly_off(
    payload: WeeklyOffNurseUpdatePayload,
    group_id: Optional[str] = Query(None),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    간호사별 주휴 설정 저장
    (저장 시점의 연/월이 기준 시점으로 갱신됨)
    """
    return update_nurses_weekly_off_service(payload, current_user, db, group_id)


@router.get("/my")
async def get_my_weekly_off(
    year: int,
    month: int,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    로그인한 간호사 본인의 주휴일정만 조회
    """
    return get_my_weekly_off_service(year, month, current_user, db)