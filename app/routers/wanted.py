from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.encoders import jsonable_encoder
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone, date
import uuid

from schemas.roster_schema import (
    WantedInvokeRequest,
    WantedInvokeResponse,
    WantedDeadlineRequest,
    WantedConfigCreate,
    WantedConfig as WantedConfigSchema,
    FixedWantedCreate,
    AdjustmentResponse,
    FixedWantedListResponse,
    FixedWantedEntryResponse,
    ToggleEntryResponse,
)
from services.graph_service import graph_service
from services.group_access import resolve_effective_group, resolve_managed_group_ids, resolve_home_group_id, caller_is_head_nurse
from routers.auth import get_current_user_from_cookie
from utils.utils import send_wanted_close_push, send_wanted_deadline_update_push
from db.client2 import get_db
from db.models import (
    Wanted,
    NurseShiftRequest,
    WantedRequest,
    WantedConfig,
    Nurse,
    ShiftPreference,
    Group
)
from schemas.auth_schema import User as UserSchema
from services.wanted_service import (
    request_wanted_shifts_service,
    invoke_and_persist_wanted_service,
    get_wanted_config,
    upsert_wanted_config,
    delete_wanted_config,
    delete_wanted_config_by_month,
    validate_wanted_limits,
    get_over_limit_nurses,
    delete_excess_off_requests,
    get_wanted_adjustment_service,
    save_fixed_wanted_service,
    toggle_fixed_wanted_entry_service,
    get_fixed_wanted_for_roster_service,
    get_fixed_wanted_entries_service,
    reset_fixed_wanted_service,
    get_shift_requests_service,
)

router = APIRouter(
    prefix="/wanted",
    tags=["wanted"]
)

templates = Jinja2Templates(directory="app/templates")


# [Wanted] - Wanted 작성 요청 생성 (수간호사용)
@router.post("/request")
async def request_wanted_shifts(
    payload: WantedDeadlineRequest,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # 관리자(HN/ADM)만. 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석.
    if not (
        caller_is_head_nurse(db, current_user)
        or getattr(current_user, 'is_master_admin', False)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    override_gid = resolve_effective_group(db, current_user, group_id)
    # [DEBUG] 프론트가 group_id 를 실제로 보냈는지 확정. raw=None 이면 프론트 미전송 → home 폴백.
    print(f"[/wanted/request] raw group_id={group_id!r} -> resolved={override_gid!r} "
          f"nurse_id={current_user.nurse_id} y={payload.year} m={payload.month}")
    try:
        result = request_wanted_shifts_service(payload, current_user, db, override_group_id=override_gid)
        return result
    except Exception as e:
        import traceback as _tb
        print(f"[/wanted/request] 500: {e}\n{_tb.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Wanted 작성 요청 실패: {str(e)}")


# [Wanted] - 특정 그룹의 Wanted 상태 조회
@router.get("/status")
async def get_wanted_status(
    year: int, month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 관리자(HN/ADM)만. 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석.
    if not (
        caller_is_head_nurse(db, current_user)
        or getattr(current_user, 'is_master_admin', False)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    target_group_id = resolve_effective_group(db, current_user, group_id)

    wanted = db.query(Wanted).filter(
        Wanted.group_id == target_group_id,
        Wanted.year == year,
        Wanted.month == month
    ).first()

    if not wanted:
        return {"status": None, "message": "wanted 작성 요청 전"}
    
    return {
        "status": wanted.status,
        "exp_date": wanted.exp_date,
        "message": "작성 가능" if wanted.status == 'requested' else "wanted 작성 요청이 마감되었습니다"
    }


# [Wanted] - 특정 스케줄의 모든 간호사 제출 현황 확인
@router.get("/{year}/{month}/submissions")
async def get_submission_statuses(
    year: int, month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 관리자(HN/ADM)만. 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석.
    if not (
        caller_is_head_nurse(db, current_user)
        or getattr(current_user, 'is_master_admin', False)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    target_group_id = resolve_effective_group(db, current_user, group_id)

    # Get all nurses in the target group
    nurses_in_group = db.query(Nurse.nurse_id).filter(Nurse.group_id == target_group_id).all()
    nurse_ids_in_group = {n[0] for n in nurses_in_group}

    # 각 간호사의 최신 제출 상태 확인
    submitted_nurse_ids = set()
    for nurse_id in nurse_ids_in_group:
        # 해당 간호사의 최신 제출된 선호도가 있는지 확인
        latest_submitted = db.query(ShiftPreference).filter(
            ShiftPreference.nurse_id == nurse_id,
            ShiftPreference.year == year,
            ShiftPreference.month == month,
            ShiftPreference.is_submitted == True
        ).order_by(ShiftPreference.submitted_at.desc()).first()
        
        if latest_submitted:
            submitted_nurse_ids.add(nurse_id)

    return {
        "submitted_nurses": list(submitted_nurse_ids),
    }


# [Wanted] - 현재 그룹의 모든 wanted 데이터 조회
@router.get("/all")
async def get_all_wanted(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(비ADM 은 managed 검증).
    target_group_id = resolve_effective_group(
        db, current_user, group_id, require_group=False
    )
    wanted_list = db.query(Wanted).filter(
        Wanted.group_id == target_group_id
    ).order_by(Wanted.year.desc(), Wanted.month.desc()).all()
    return [{
        "year": wanted.year,
        "month": wanted.month,
        "status": wanted.status,
        "exp_date": wanted.exp_date.isoformat() if wanted.exp_date else None,
        "created_at": wanted.created_at.isoformat() if wanted.created_at else None
    } for wanted in wanted_list]


# [Wanted] - Wanted 상태를 closed로 변경
@router.patch("/close")
async def close_wanted_request(
    year: int, month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400).
    target_group_id = resolve_effective_group(db, current_user, group_id)

    wanted = db.query(Wanted).filter(
        Wanted.group_id == target_group_id,
        Wanted.year == year,
        Wanted.month == month
    ).first()

    if not wanted:
        raise HTTPException(status_code=404, detail="해당 월의 wanted 요청을 찾을 수 없습니다.")
    
    wanted.status = 'closed'
    db.commit()

    nurses_in_group = db.query(Nurse.nurse_id).filter(Nurse.group_id == target_group_id).all()
    recipients = [nurse.nurse_id for nurse in nurses_in_group]
    send_wanted_close_push(
        year=wanted.year,
        month=wanted.month,
        recipients=recipients,
        office_code=current_user.office_id,
        sender_emp_seq_no=current_user.nurse_id,
        sender_member_id=current_user.account_id,
    )

    return {"message": "Wanted 요청이 마감되었습니다."}


# [Wanted] - Wanted 마감일 변경
@router.patch("/deadline")
async def update_wanted_deadline(
    req: WantedDeadlineRequest,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400). 관리자 게이트는 상단 pre-gate.
    target_group_id = resolve_effective_group(db, current_user, group_id)

    wanted = db.query(Wanted).filter(
        Wanted.group_id == target_group_id,
        Wanted.year == req.year,
        Wanted.month == req.month
    ).first()
    
    if not wanted:
        raise HTTPException(status_code=404, detail="해당 월의 wanted 요청을 찾을 수 없습니다.")
    
    if wanted.status == 'closed':
        raise HTTPException(status_code=400, detail="마감된 wanted 요청의 마감일은 변경할 수 없습니다.")
    
    payload = req.model_dump(exclude_unset=True)
    if "exp_date" in payload:
        wanted.exp_date = req.exp_date
        db.commit()
        db.refresh(wanted)
        message = "마감일이 제거되었습니다. (마감일 없음)" if req.exp_date is None else "마감일이 성공적으로 변경되었습니다."
        nurses_in_group = db.query(Nurse.nurse_id).filter(Nurse.group_id == target_group_id).all()
        recipients = [nurse.nurse_id for nurse in nurses_in_group]
        send_wanted_deadline_update_push(
            year=wanted.year,
            month=wanted.month,
            recipients=recipients,
            office_code=current_user.office_id,
            sender_emp_seq_no=current_user.nurse_id,
            sender_member_id=current_user.account_id,
            deadline=wanted.exp_date,
        )
    else:
        message = "마감일 변경 요청이 없어 기존 값이 유지됩니다."
    
    display_exp_date = "마감일 없음" if wanted.exp_date is None else wanted.exp_date.strftime("%Y-%m-%d")
    
    return {
        "message": message,
        "current_exp_date": wanted.exp_date.isoformat() if wanted.exp_date else None,
        "display_exp_date": display_exp_date
    }


# @router.post("/invoke", response_model=WantedInvokeResponse)
@router.post("/invoke")
async def invoke_graph(request: WantedInvokeRequest, current_user: UserSchema = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    """
    그래프를 실행하여 로스터 관련 요청을 처리합니다.
    """
    
    try:
        
        import uuid
        trace_id = str(uuid.uuid4())[:8]
        print(f"[INVOKE START] trace_id={trace_id} | nurse_id={current_user.nurse_id} | request={request.request}")
        result = await invoke_and_persist_wanted_service(request, current_user, db)
        
        # 서비스 끝난 후 강제 commit + flush
        db.flush()  # 변경 사항 즉시 반영
        db.commit() # 한번 더 강제 commit
        
        print(f"[INVOKE END] trace_id={trace_id} | 생성된 request_id={result.get('request_id')}")
        return {"response": result}
    except Exception as e:
        db.rollback()
        print(f'error', e)
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        pass


def parse_shift_results(
    response: List[List[Dict[str, Any]]]
) -> Dict[str, Dict[int, float]]:
    """
    2-중 리스트 구조의 shift_result → {shift: {date: score}} 형태로 통합.

    Parameters
    ----------
    response : List[List[Dict[str, Any]]]
        create_shift_analyzer 가 돌려준 전체 응답.

    Returns
    -------
    Dict[str, Dict[int, float]]
        {'D': {7: 0.0}, 'N': {10: 0.0}, ...}
    """
    parsed: Dict[str, Dict[int, float]] = {}

    # ── 1. 최상위는 여러 agent 그룹이 List 로 묶여있음 ─────────────────────
    for sub in response:
        if not isinstance(sub, list):
            continue

        # ── 2. 한 그룹 안에는 여러 entry 가 dict 로 존재 ──────────────────
        for entry in sub:
            shift_results = entry.get("shift_result", [])
            if not isinstance(shift_results, list):
                continue

            # ── 3. shift_result 의 각 항목 처리 ────────────────────────────
            for sr in shift_results:
                # ▷ 케이스 A : {'shift': 'D', 'date': [...], 'score': [...]}
                if {"shift", "date", "score"} <= sr.keys():
                    record = sr
                # ▷ 케이스 B : {'result': {...}}
                elif "result" in sr and isinstance(sr["result"], dict):
                    record = sr["result"]
                else:                       # 예상 외 구조 → skip
                    continue

                shift  = record.get("shift")
                dates  = record.get("date", [])
                scores = record.get("score", [])

                if not shift or not isinstance(dates, list) or not isinstance(scores, list):
                    continue

                bucket = parsed.setdefault(shift, {})
                for d, s in zip(dates, scores):
                    bucket[d] = float(s)

    return parsed


def parse_preferences(
    response: List[List[Dict[str, Any]]],
    schema: List[Dict[str, Any]] = None
) -> List[Dict[str, float]]:
    """
    2-중 리스트 구조의 preference_result 항목들을 전부 뽑아서
    [{'id': 12, 'weight': -1.5}, {'id': 13, 'weight': 1.5}, ...]
    형태의 리스트로 반환합니다. preference_result 가 없거나
    비어있어도 빈 리스트를 반환하며 에러는 발생하지 않습니다.
    schema가 제공되면 유효한 간호사 ID만 필터링합니다.
    """
    parsed: List[Dict[str, float]] = []
    
    # schema에서 유효한 간호사 ID 목록 추출
    valid_nurse_ids = set()
    if schema:
        for nurse in schema:
            if isinstance(nurse, dict) and 'nurse_id' in nurse:
                valid_nurse_ids.add(nurse['nurse_id'])

    # 최상위: 여러 agent 그룹
    for sub in response:
        if not isinstance(sub, list):
            continue
        # 그룹 내 각 entry
        for entry in sub:
            # preference_result 키로 가져오고, 없으면 빈 리스트
            pref_list = entry.get("preference_result", [])
            if not isinstance(pref_list, list):
                continue
            # 각 preference 항목
            for pr in pref_list:
                _id     = pr.get("id")
                weight  = pr.get("weight")
                # id 와 weight 모두 있을 때만
                if _id is None or weight is None:
                    continue
                
                # 빈 ID는 무시
                if not _id:
                    continue
                    
                # schema가 있고 ID가 유효하지 않으면 무시
                if valid_nurse_ids and _id not in valid_nurse_ids:
                    print(f"Parse Preferences: 무효한 간호사 ID '{_id}' 필터링됨")
                    continue
                    
                try:
                    parsed.append({"id": str(_id), "weight": float(weight)})
                except (ValueError, TypeError):
                    # 변환 불가능하면 skip
                    continue

    return parsed


# [Wanted] - 만료된 Wanted 자동 마감 처리
@router.post("/close-expired")
def close_expired_wanted_endpoint(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    현재 KST 기준으로 exp_date가 지난 Wanted 요청의 status를 'closed'로 일괄 변경하는 엔드포인트입니다.
    """
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_kst = (utc_now + timedelta(hours=9)).replace(tzinfo=None)

    query = (
        db.query(Wanted)
        .filter(
            Wanted.status == "requested",
            Wanted.exp_date.isnot(None),
            Wanted.exp_date < now_kst,
        )
    )

    updated_count = 0
    closed_wanteds = []
    try:
        for wanted in query:
            wanted.status = "closed"
            closed_wanteds.append(wanted)
            updated_count += 1

        if updated_count > 0:
            db.commit()
            print(
                f"Wanted 수동 마감 완료(KST 기준): {updated_count}건, now_kst={now_kst.isoformat()}"
            )
        else:
            db.flush()
    except Exception as exc:
        db.rollback()
        print(f"Wanted 수동 마감 중 오류: {exc}")
        raise

    for wanted in closed_wanteds:
        nurses_in_group = db.query(Nurse.nurse_id).filter(Nurse.group_id == wanted.group_id).all()
        recipients = [nurse.nurse_id for nurse in nurses_in_group]
        if not recipients:
            continue
        group = db.query(Group).filter(Group.group_id == wanted.group_id).first()
        office_id = group.office_id if group else None
        hn_id = (group.hn_id or [])[0] if group and group.hn_id else None
        if not office_id or not hn_id:
            continue
        hn = db.query(Nurse).filter(Nurse.nurse_id == hn_id).first()
        if not hn:
            continue
        send_wanted_close_push(
            year=wanted.year,
            month=wanted.month,
            recipients=recipients,
            office_code=office_id,
            sender_emp_seq_no=hn.nurse_id,
            sender_member_id=hn.account_id,
        )

    return {"now_kst": now_kst.isoformat(), "updated": updated_count}


# WantedConfig 관련 엔드포인트
@router.get("/config")
async def get_wanted_config_endpoint(
    year: Optional[int] = None,
    month: Optional[int] = None,
    target_date: Optional[str] = None,
    shift_type: Optional[str] = None,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    일자별 원티드 제한 설정 조회 (활성 설정만)
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400).
    target_group_id = resolve_effective_group(db, current_user, group_id)

    # 필터 구성
    filters = {}
    if year and month:
        filters['year'] = year
        filters['month'] = month
    if target_date:
        filters['target_date'] = target_date
    if shift_type:
        filters['shift_type'] = shift_type

    try:
        result = get_wanted_config(db, target_group_id, filters)
        return [WantedConfigSchema.model_validate(r) for r in result]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"설정 조회 실패: {str(e)}")


@router.post("/config")
async def upsert_wanted_config_endpoint(
    config_data: List[WantedConfigCreate],
    group_id: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    일자별 원티드 제한 설정 생성/수정
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 권한 확인 (수간호사 또는 관리자만)
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 대상 그룹 결정
    # 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400). 관리자 게이트는 상단 pre-gate.
    target_group_id = resolve_effective_group(db, current_user, group_id)

    try:
        configs_list = [c.model_dump(exclude_unset=True) for c in config_data]
        results = upsert_wanted_config(db, target_group_id, configs_list, year=year, month=month)
        return [WantedConfigSchema.model_validate(r) for r in results]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"설정 저장 실패: {str(e)}")


@router.delete("/config")
async def delete_wanted_config_endpoint(
    target_date: Optional[str] = None,
    shift_type: Optional[str] = None,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    일자별 원티드 제한 설정 삭제
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 권한 확인 (수간호사 또는 관리자만)
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 대상 그룹 결정
    # 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400). 관리자 게이트는 상단 pre-gate.
    target_group_id = resolve_effective_group(db, current_user, group_id)

    # 필터 구성
    filters = {}
    if target_date:
        filters['target_date'] = target_date
    if shift_type:
        filters['shift_type'] = shift_type

    try:
        deleted_count = delete_wanted_config(db, target_group_id, filters)
        return {
            "message": f"{deleted_count}건의 설정이 삭제되었습니다.",
            "deleted_count": deleted_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"설정 삭제 실패: {str(e)}")


@router.delete("/config/toggle")
async def delete_wanted_config_by_month_endpoint(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    일자별 원티드 제한 설정 일괄 삭제 (OFF 처리)
    - 해당 그룹/월의 모든 config 행을 삭제
    - 다시 ON 시에는 POST /config로 신규 데이터만 추가
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 대상 그룹 결정
    # 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400). 관리자 게이트는 상단 pre-gate.
    target_group_id = resolve_effective_group(db, current_user, group_id)

    try:
        deleted_count = delete_wanted_config_by_month(db, target_group_id, year, month)
        return {
            "message": f"{deleted_count}건의 설정이 삭제되었습니다.",
            "deleted_count": deleted_count,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"설정 삭제 실패: {str(e)}")


@router.post("/validate-limits")
async def validate_wanted_limits_endpoint(
    nurse_id: str,
    year: int,
    month: int,
    shift_date_str: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    원티드 요청 제한 검증
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 관리자(HN/ADM)만. 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석.
    if not (
        caller_is_head_nurse(db, current_user)
        or getattr(current_user, 'is_master_admin', False)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    target_group_id = resolve_effective_group(db, current_user, group_id)

    try:
        # 문자열 날짜를 date 객체로 변환
        shift_date = datetime.strptime(shift_date_str, '%Y-%m-%d').date()

        result = validate_wanted_limits(db, nurse_id, target_group_id, year, month, shift_date)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"날짜 형식 오류: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"검증 실패: {str(e)}")


# 간호사 원티드 개수 제한 초과분인 경우에 대한 조회 및 무조건적인 삭제 기능 서비스 함수
@router.get("/over-limit-nurses")
async def get_over_limit_nurses_api(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    # 관리자 권한 체크 (수간호사 여부는 토큰 대신 DB)
    if not (caller_is_head_nurse(db, current_user) or current_user.is_master_admin):
        raise HTTPException(403, "권한이 없습니다.")

    result = get_over_limit_nurses(db, year, month, group_id)
    return {"data": result, "count": len(result)}


@router.post("/delete-excess-off/{nurse_id}")
async def delete_excess_off_api(
    nurse_id: str,
    year: int,
    month: int,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if current_user.is_master_admin:
        pass
    elif caller_is_head_nurse(db, current_user):
        # 토큰 group 대신 nurse_id→DB + groups.hn_id 로 관리 그룹 판정.
        target_nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
        _managed = resolve_managed_group_ids(db, current_user)
        if not target_nurse or str(target_nurse.group_id) not in _managed:
            raise HTTPException(403, "관리하는 병동 내 간호사가 아닙니다.")
    else:
        raise HTTPException(403, "권한이 없습니다.")

    result = delete_excess_off_requests(db, nurse_id, year, month)
    return result


# Fixed Wanted 관련 엔드포인트
@router.get("/adjustment/{year}/{month}", response_model=AdjustmentResponse)
async def get_wanted_adjustment(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    원티드 조정판 데이터 조회 API
    - 기존 근무표 만들기와 동일한 구성
    - 각 간호사의 원티드 제출한 코드 내역들의 한달치 총 합 실시간 노출
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 그룹: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(그룹전환 안전).
    target_group_id = resolve_effective_group(db, current_user, group_id)

    try:
        result = get_wanted_adjustment_service(db, target_group_id, year, month)
        return JSONResponse(
            content=jsonable_encoder(result),
            media_type="application/json; charset=utf-8"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"원티드 조정판 조회 실패: {str(e)}")


@router.post("/adjustment", response_model=FixedWantedListResponse)
async def save_fixed_wanted(
    req: FixedWantedCreate,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    확정 원티드 저장 API (단일 테이블 구조)
    - 원티드 조정판에서 수정 후 저장하기 클릭시 호출
    - fixed_wanted_entries 테이블에 저장
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 대상 그룹 결정
    # 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400). 관리자 게이트는 상단 pre-gate.
    target_group_id = resolve_effective_group(db, current_user, group_id)

    try:
        entries = save_fixed_wanted_service(db, target_group_id, current_user.nurse_id, req)
        return FixedWantedListResponse(
            group_id=target_group_id,
            year=req.year,
            month=req.month,
            entries=[
                FixedWantedEntryResponse(
                    id=e.id,
                    group_id=e.group_id,
                    year=e.year,
                    month=e.month,
                    nurse_id=e.nurse_id,
                    shift_date=e.shift_date,
                    shift_id=e.shift_id,
                    shifts_table_id=e.shifts_table_id,
                    is_applied=e.is_applied,
                    source_type=e.source_type,
                    original_shift_id=e.original_shift_id,
                    reason=e.reason,
                    head_nurse_memo=e.head_nurse_memo,
                    created_by=e.created_by,
                ) for e in entries
            ],
            total_count=len(entries),
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"확정 원티드 저장 실패: {str(e)}")


@router.patch("/adjustment/entry/{entry_id}/toggle", response_model=ToggleEntryResponse)
async def toggle_fixed_wanted_entry(
    entry_id: int,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    확정 원티드 개별 항목 적용/미적용 토글 API
    - 수간호사는 본인 병동 entries만 토글 가능. master_admin은 office 범위로 허용.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 본인 병동(home)은 토큰 대신 nurse_id→DB 로 판정. ADM 은 None(office 범위).
    caller_group_id: Optional[str] = None
    if caller_is_head_nurse(db, current_user):
        caller_group_id = resolve_home_group_id(db, current_user)

    try:
        entry = toggle_fixed_wanted_entry_service(db, entry_id, caller_group_id=caller_group_id)
        return ToggleEntryResponse(
            id=entry.id,
            is_applied=entry.is_applied,
            message=f"항목이 {'적용' if entry.is_applied else '미적용'}으로 변경되었습니다."
        )
    except HTTPException:
        db.rollback()
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"토글 실패: {str(e)}")


@router.post("/adjustment/{year}/{month}/reset", response_model=AdjustmentResponse)
async def reset_fixed_wanted(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    확정 원티드 재설정 API
    - FixedWantedEntry 해당 월 데이터 전체 삭제
    - 원본 WantedRequest + NurseShiftRequest 기반 데이터로 복원하여 반환
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 대상 그룹 결정
    # 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400). 관리자 게이트는 상단 pre-gate.
    target_group_id = resolve_effective_group(db, current_user, group_id)

    try:
        result = reset_fixed_wanted_service(db, target_group_id, year, month)
        return result
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"확정 원티드 재설정 실패: {str(e)}")


@router.get("/fixed/{year}/{month}")
async def get_fixed_wanted(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    확정 원티드 조회 API (근무표 생성) - 단일 테이블 구조
    - is_applied=True인 항목만 반환
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (caller_is_head_nurse(db, current_user) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 대상 그룹: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400).
    target_group_id = resolve_effective_group(db, current_user, group_id)

    try:
        # 엔트리 목록 조회 (단일 테이블)
        all_entries = get_fixed_wanted_entries_service(db, target_group_id, year, month)
        if not all_entries:
            return {"exists": False, "entries": [], "total_count": 0}

        # 근무표 생성용 형식으로 변환 (is_applied=True만)
        entries = get_fixed_wanted_for_roster_service(db, target_group_id, year, month)

        # 첫 번째 엔트리에서 메타 정보 추출
        first_entry = all_entries[0] if all_entries else None
        return {
            "exists": True,
            "group_id": target_group_id,
            "year": year,
            "month": month,
            "created_at": first_entry.created_at.isoformat() if first_entry and first_entry.created_at else None,
            "updated_at": first_entry.updated_at.isoformat() if first_entry and first_entry.updated_at else None,
            "entries": entries,
            "total_count": len(all_entries),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"확정 원티드 조회 실패: {str(e)}")


# [Wanted] - 전체 원티드 제출 현황 + shift 내역 일괄 조회
@router.get("/{year:int}/{month:int}/shift-requests")
async def get_all_shift_requests(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    shift_type: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """해당 년/월 그룹 내 전체 간호사의 원티드 제출 여부 + nurse_shift_requests 내역 반환.
    shift_type: 특정 근무만 필터링 (예: 'O'면 OFF만)"""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹 결정
    # 관리자(HN/ADM)만 조회. 그룹은 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석.
    if not (
        caller_is_head_nurse(db, current_user)
        or getattr(current_user, 'is_master_admin', False)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    target_group_id = resolve_effective_group(db, current_user, group_id)

    results = get_shift_requests_service(db, target_group_id, year, month, shift_type)
    return {"results": results}
