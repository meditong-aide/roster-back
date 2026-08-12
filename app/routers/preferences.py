from schemas.roster_schema import PreferenceData, PreferenceSubmit, WantedEntryItem
from services.wanted_service import WantedAnalysisError, analyze_wanted_text
from routers.auth import get_current_user_from_cookie
from services.group_access import (
    resolve_home_group_id,
    caller_is_head_nurse,
    assert_caller_can_access_group,
)
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
    get_all_preferences_service,
    get_monthly_memo_service,
    save_monthly_memo_service,
    list_group_monthly_memos_service,
)
from pydantic import BaseModel, Field
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

    ★★ **단, 극성이 반대면 문장이 이긴다.**
      `wanted_entries` 로 실려오는 캘린더 상태는 "이번에 찍은 것" 이 아니라
      **이전에 저장돼 다시 실려온 것**이다. 그래서 날짜만 보고 버리면
      "N 금지" 뒤에 "N 주세요" 라고 말해도 옛 기피가 이겨 **문장이 통째로 무시된다**
      (실측: 분석은 want 8건을 정확히 뽑았는데 "8건 중 0건 반영"으로 전량 폐기).
      방금 한 말이 이전 상태보다 최신 의사이므로, 요청↔금지가 뒤집히는 경우에는
      분석 결과로 교체한다. 같은 극성이면(코드만 다름) 기존대로 캘린더 우선이다.
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

    def _key(d) -> str:
        return d.isoformat() if hasattr(d, "isoformat") else str(d)

    by_date = {_key(e.date): e for e in picked}
    added = flipped = 0
    for item in analyzed:
        existing = by_date.get(item["date"])
        if existing is None:
            by_date[item["date"]] = WantedEntryItem(**item)
            added += 1
        elif getattr(existing, "intent", "wanted") != item.get("intent", "wanted"):
            # 요청↔금지가 뒤집힌 경우 — 방금 한 말이 이전 저장분보다 최신 의사다.
            by_date[item["date"]] = WantedEntryItem(**item)
            flipped += 1
        # 같은 극성이면 캘린더(직접 선택) 우선 — 기존 규칙 유지.

    req.wanted_entries = [by_date[k] for k in sorted(by_date)]
    print(f"[preferences] 자연어 분석 병합: {len(analyzed)}건 중 "
          f"신규 {added}건 · 극성전환 {flipped}건 반영 "
          f"(캘린더 {len(picked)}건 중 {len(picked) - flipped}건 유지)")
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


# ──────────────────────────────────────────────────────────────
# [Preferences] 원티드 월별 메모
#   ★ 원티드 저장 경로(POST /preferences)와 **완전히 분리된 통로**다.
#     그 경로는 저장 한 번에 BannedWantedEntry · NurseShiftRequest ·
#     NursePairRequest 를 delete-then-insert 한다. 메모는 입력 중 디바운스로 자주
#     저장되므로 같이 태우면 원티드가 통째로 지워질 위험이 크다.
#   ★ year·month 는 항상 요청에서 받는다. 서버가 현재 월 등으로 추론하지 않는다.
# ──────────────────────────────────────────────────────────────
class MonthlyMemoUpdate(BaseModel):
    year: int
    month: int
    group_id: Optional[str] = None
    monthly_memo: Optional[str] = Field(
        default=None,
        description="월별 메모. null 또는 공백만이면 삭제로 처리한다.",
    )


@router.get("/monthly-memo")
async def get_monthly_memo(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """본인의 그 달 원티드 메모. 저장된 적 없으면 monthly_memo=null."""
    try:
        return get_monthly_memo_service(
            year, month, current_user, db, override_group_id=group_id
        )
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        _raise_domain_error(e)
    except Exception as e:
        print('[preferences.py] monthly-memo 조회 실패', e)
        raise HTTPException(status_code=500, detail=f"월별 메모 조회 실패: {str(e)}")


@router.patch("/monthly-memo")
async def patch_monthly_memo(
    req: MonthlyMemoUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """월별 메모 저장. wanted_monthly_memo 외의 어떤 테이블도 건드리지 않는다."""
    # ★ 미전송과 명시적 null 을 가른다. PATCH 이므로 안 보낸 필드는 "그대로 두라"는
    #   뜻이고, 삭제는 null 을 **명시**해야 한다. 안 가르면 {year, month} 만 보낸
    #   요청이 메모를 지운다.
    if "monthly_memo" not in req.model_fields_set:
        raise HTTPException(
            status_code=400,
            detail="monthly_memo 를 명시해야 합니다(삭제는 null).",
        )
    try:
        return save_monthly_memo_service(
            req.year, req.month, req.monthly_memo, current_user, db,
            override_group_id=req.group_id,
        )
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        db.rollback()
        _raise_domain_error(e)
    except Exception as e:
        db.rollback()
        print('[preferences.py] monthly-memo 저장 실패', e)
        raise HTTPException(status_code=500, detail=f"월별 메모 저장 실패: {str(e)}")


@router.get("/monthly-memo/group")
async def list_group_monthly_memos(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """관리보드용 — 그룹에서 메모를 쓴 사람만 모아 돌려준다.

    ★ 개인이 쓴 내용이라 수간호사·관리자만 볼 수 있다.
    ★ 메모가 없는 사람은 응답에서 제외한다. 화면이 이름 위 호버로 보여 주므로
      "메모가 있는가" 가 곧 표시 조건이다.
    """
    if not (caller_is_head_nurse(db, current_user)
            or getattr(current_user, "is_master_admin", False)):
        raise HTTPException(status_code=403, detail="Permission denied")
    if group_id:
        assert_caller_can_access_group(db, current_user, group_id)
    elif getattr(current_user, "is_master_admin", False):
        # ★ 관리자는 홈 그룹이 없을 수 있다. 그때 group_id 없이 부르면 대상 그룹을
        #   정할 수 없으므로 400 으로 명확히 돌려준다(500 방지).
        raise HTTPException(status_code=400, detail="group_id 가 필요합니다.")
    try:
        return list_group_monthly_memos_service(
            year, month, current_user, db, override_group_id=group_id
        )
    except HTTPException:
        raise
    except (PreferenceForbiddenError, PreferenceConflictError, PreferenceValidationError) as e:
        _raise_domain_error(e)
    except Exception as e:
        print('[preferences.py] monthly-memo/group 조회 실패', e)
        raise HTTPException(status_code=500, detail=f"월별 메모 목록 조회 실패: {str(e)}")
