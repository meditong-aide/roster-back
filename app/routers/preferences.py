from schemas.roster_schema import PreferenceData, PreferenceSubmit
from routers.auth import get_current_user_from_cookie
from services.group_access import resolve_home_group_id
from db.client2 import get_db
from db.models import ShiftPreference, Nurse, Shift
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


@router.post("")
async def save_preference_draft(
    req: PreferenceData,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    희망근무 초안 저장 (임시 저장)
    """
    try:
        return submit_preferences_service(req, current_user, db, is_draft=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"임시 저장 실패: {str(e)}")


@router.post("/submit")
async def submit_preferences(
    req: PreferenceData,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        # 허용 근무코드 검증
        print('req', )
        preferences = [shift_id for shift_id in req.data.values() if shift_id]
        if preferences:
            allowed_shifts = {
                row[0] for row in db.query(Shift.shift_id).filter(
                    Shift.group_id == resolve_home_group_id(db, current_user),
                    Shift.show_in_preference == True
                ).all()
            }
            invalid_shifts = [s for s in preferences if s not in allowed_shifts]
            if invalid_shifts:
                raise HTTPException(
                    status_code=400,
                    detail=f"허용되지 않은 근무코드: {', '.join(set(invalid_shifts))}"
                )
        
        return submit_preferences_service(req, current_user, db, is_draft=False)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"제출 실패: {str(e)}")


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
        print('[preferences.py] error', e)
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
        return get_all_preferences_service(year, month, current_user, db, override_group_id=group_id)
    except Exception as e:
        print('[preferences.py] error', e)
        raise HTTPException(status_code=500, detail=f"전체 선호도 조회 실패: {str(e)}")