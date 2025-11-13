from schemas.roster_schema import PreferenceData, PreferenceSubmit
from routers.auth import get_current_user_from_cookie
from db.client2 import get_db
from db.models import ShiftPreference, Nurse
from schemas.auth_schema import User as UserSchema
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from services.preferences_service import (
    # save_preference_draft_service,
    submit_preferences_service,
    submit_empty_preferences_service,
    retract_submission_service,
    get_latest_preference_service,
    get_all_preferences_service
)
from typing import Optional

router = APIRouter(
    prefix="/preferences",
    tags=["preferences"]
)

# [Preferences] - 간호사 개인의 선호도 초안 저장
@router.post("")
async def save_preference_draft(
    pref_data: PreferenceData,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    # try:
    #     return save_preference_draft_service(pref_data, current_user, db)
    # except Exception as e:
    #     raise HTTPException(status_code=500, detail=f"선호도 초안 저장 실패: {str(e)}")
    return

# [Preferences] - 선호도 최종 제출
@router.post("/submit")
async def submit_preferences(
    req: PreferenceSubmit,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        return submit_preferences_service(req, current_user, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"선호도 최종 제출 실패: {str(e)}")

# [Preferences] - 빈 선호도 최종 제출
@router.post("/submit/empty")
async def submit_empty_preferences(
    req: PreferenceSubmit,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        return submit_empty_preferences_service(req, current_user, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"빈 선호도 제출 실패: {str(e)}")

# [Preferences] - 최종 제출 철회 (수정)
@router.post("/retract")
async def retract_submission(
    req: PreferenceSubmit,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        return retract_submission_service(req, current_user, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"제출 철회 실패: {str(e)}")

# [Preferences] - 최신 선호도 데이터 조회
@router.get("/latest")
async def get_latest_preference(
    year: int, 
    month: int,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        return get_latest_preference_service(year, month, current_user, db)
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"최신 선호도 조회 실패: {str(e)}")

# [Preferences] - 모든 간호사의 희망사항 현황 조회
@router.get("/all")
async def get_all_preferences(
    year: int, 
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        # 대상 그룹 결정 (HN: 본인 그룹, ADM: 쿼리로 지정)
        # override_gid = None
        # if getattr(current_user, 'is_head_nurse', False) and current_user.group_id:
        #     override_gid = None
        # else:
        #     print('아님여긴가?', group_id)
        #     # if not getattr(current_user, 'is_master_admin', False):
        #     #     raise HTTPException(status_code=403, detail="Permission denied")
        #     # if not group_id:
        #     #     raise HTTPException(status_code=400, detail="group_id is required for admin")
        #     from db.models import Group
        #     g = db.query(Group).filter(Group.group_id == group_id).first()
        #     # if not g:
        #     #     raise HTTPException(status_code=404, detail="Group not found")
        #     # if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
        #     #     raise HTTPException(status_code=403, detail="Group does not belong to your office")
        #     override_gid = g.group_id
        return get_all_preferences_service(year, month, current_user, db, override_group_id=group_id)
    except Exception as e:
        print('[preferences.py] error', e)
        raise HTTPException(status_code=500, detail=f"전체 선호도 조회 실패: {str(e)}")