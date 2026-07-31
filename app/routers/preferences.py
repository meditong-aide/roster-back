from schemas.roster_schema import PreferenceData, PreferenceSubmit, WantedEntryItem
from services.wanted_service import WantedAnalysisError, analyze_wanted_text
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


def _with_analysis(result, analysis: Optional[dict]):
    """응답에 자연어 분석 상태를 얹는다. 성공(None)이면 그대로 둔다."""
    if analysis and isinstance(result, dict):
        result["analysis"] = analysis
    return result

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
        body = {"code": exc.code, "message": str(exc)}
        # 화면이 어느 날짜를 짚어 줄지 알 수 있게 부가 정보를 함께 내린다.
        if getattr(exc, "detail", None):
            body["detail"] = exc.detail
        raise HTTPException(status_code=422, detail=body)
    raise exc


async def _merge_analyzed_request(req: PreferenceData, current_user, db) -> Optional[dict]:
    """`req.request`(자연어)가 있으면 분석해 `req.wanted_entries` 에 병합한다.

    프론트가 `/wanted/invoke` → `/preferences` → `/preferences/submit` 로 나눠 부르던
    것을 한 번으로 합치기 위한 전처리다. invoke 자체는 모바일이 아직 쓰고 있어
    그대로 둔다(제거하면 동시 배포 없이 회귀).

    **캘린더로 찍은 항목이 우선**이다. 같은 날짜가 겹치면 분석 결과를 버린다 —
    사용자가 직접 고른 것이 문장 해석보다 확실하다.
    분석이 실패하면 저장은 그대로 진행하되 **상태를 돌려준다**(호출자가 응답에 실어
    화면이 "문장 해석에 실패했습니다" 를 띄우게). 조용히 넘기면 사용자는 저장된 줄
    안다. 성공이면 None.
    """
    text = (getattr(req, "request", None) or "").strip()
    if not text:
        return
    group_id = req.group_id or getattr(current_user, "group_id", None)
    if not group_id:
        return
    try:
        analyzed = await analyze_wanted_text(
            db, getattr(current_user, "nurse_id", None), group_id, text,
            req.year, req.month,
        )
    except WantedAnalysisError as exc:
        print(f"[preferences] 자연어 분석 실패 — 저장은 계속: {exc}")
        return {
            "ok": False,
            "code": "analysis_failed",
            "message": "문장을 해석하지 못했습니다. 달력에서 직접 선택하거나 다시 시도해 주세요.",
        }
    if not analyzed:
        return None
    picked = list(req.wanted_entries or [])
    taken = {e.date.isoformat() if hasattr(e.date, "isoformat") else str(e.date)
             for e in picked}
    merged = list(picked)
    for item in analyzed:
        if item["date"] in taken:
            continue
        taken.add(item["date"])
        merged.append(WantedEntryItem(**item))
    req.wanted_entries = merged
    print(f"[preferences] 자연어 분석 병합: {len(analyzed)}건 중 "
          f"{len(merged) - len(picked)}건 반영 (캘린더 {len(picked)}건 우선)")
    return None


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
        analysis = await _merge_analyzed_request(req, current_user, db)
        result = submit_preferences_service(req, current_user, db, is_draft=True)
        return _with_analysis(result, analysis)
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
        # 자연어가 함께 오면 분석해 병합한다 — 이 호출 하나로 분석·저장·제출이 끝난다.
        analysis = await _merge_analyzed_request(req, current_user, db)
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

        result = submit_preferences_service(req, current_user, db, is_draft=False)
        return _with_analysis(result, analysis)
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
