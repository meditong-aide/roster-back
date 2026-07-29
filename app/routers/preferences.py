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
    PreferenceConflictError,
    PreferenceForbiddenError,
    PreferenceValidationError,
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


def _raise_domain_error(exc: Exception) -> None:
    """원티드 도메인 예외를 HTTP 상태로 매핑한다.

    - 403: 역할/그룹 불일치
    - 409: 마감 또는 이미 제출됨
    - 422: 날짜 중복 · 유효하지 않은 shift_id · 미지원 intent · 한도 초과
    매핑 대상이 아니면 그대로 재전파해 호출부가 500 으로 처리하게 둔다.
    """
    if isinstance(exc, PreferenceForbiddenError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, PreferenceConflictError):
        raise HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PreferenceValidationError):
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)}
        )
    raise exc


@router.post("")
async def save_preference_draft(
    req: PreferenceData,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    희망근무 초안 저장 (임시 저장)

    req.wanted_entries 를 보내면 이 호출 1회로 저장이 완결되고 canonical wanted
    snapshot 을 반환한다(/wanted/invoke 불필요).
    """
    try:
        return submit_preferences_service(req, current_user, db, is_draft=True)
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        db.rollback()
        _raise_domain_error(e)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"임시 저장 실패: {str(e)}")


@router.post("/submit")
async def submit_preferences(
    req: PreferenceData,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    희망근무 최종 제출

    req.wanted_entries 를 보내면 저장과 제출을 한 트랜잭션으로 원자 처리하고
    canonical wanted snapshot 을 반환한다.
    """
    try:
        # 허용 근무코드 검증 (기존 data 기반 경로 전용).
        # wanted_entries 경로는 서비스에서 422 로 검증한다.
        preferences = (
            [shift_id for shift_id in req.data.values() if shift_id]
            if req.wanted_entries is None else []
        )
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
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        db.rollback()
        _raise_domain_error(e)
    except Exception as e:
        db.rollback()
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
    except HTTPException:
        raise
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
    except HTTPException:
        raise
    except Exception as e:
        print('[preferences.py] error', e)
        raise HTTPException(status_code=500, detail=f"제출 철회 실패: {str(e)}")

# [Preferences] - 최신 선호도 데이터 조회
@router.get("/latest")
async def get_latest_preference(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    본인 원티드의 canonical snapshot 조회.

    작성 데이터(preference_data.wanted_entries), 제출/마감 상태(submission),
    마감시각(submission.deadline_at), 요청 한도(request_limit)를 한 번에 반환한다.
    """
    try:
        return get_latest_preference_service(
            year, month, current_user, db, override_group_id=group_id
        )
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        _raise_domain_error(e)
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
    except HTTPException:
        raise
    except Exception as e:
        print('[preferences.py] error', e)
        raise HTTPException(status_code=500, detail=f"전체 선호도 조회 실패: {str(e)}")
