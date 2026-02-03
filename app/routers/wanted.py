from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime
from schemas.roster_schema import WantedInvokeRequest, WantedInvokeResponse, WantedDeadlineRequest
from services.graph_service import graph_service
from pydantic import BaseModel
from routers.auth import get_current_user_from_cookie
from db.client2 import get_db
from db.models import Wanted, NurseShiftRequest
from schemas.auth_schema import User as UserSchema
from db.models import Nurse, ShiftPreference
from services.wanted_service import request_wanted_shifts_service
from services.wanted_service import invoke_and_persist_wanted_service
from services.wanted_service import close_expired_wanted
from db.models import Group
from datetime import datetime, timedelta, timezone
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
    
    # 추가
    close_expired_wanted(db)
    
    # 대상 그룹 결정 (HN: 본인 그룹, ADM: 쿼리로 지정)
    if getattr(current_user, 'is_head_nurse', False) and current_user.group_id:
        override_gid = None
    else:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        from db.models import Group
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        override_gid = g.group_id
    try:
        result = request_wanted_shifts_service(payload, current_user, db, override_group_id=override_gid)
        return result
    except Exception as e:
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

    # 대상 그룹 결정 (HN: 본인 그룹, ADM: 쿼리로 지정)
    if getattr(current_user, 'is_head_nurse', False) and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        from db.models import Group
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id

    # 추가
    close_expired_wanted(db)
    
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

    # 대상 그룹 결정 (HN: 본인 그룹, ADM: 쿼리로 지정)
    if getattr(current_user, 'is_head_nurse', False) and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        from db.models import Group
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id

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
    
    # 추가
    close_expired_wanted(db)

    # 대상 그룹 결정
    if getattr(current_user, 'is_head_nurse', False) and current_user.group_id:
        target_group_id = current_user.group_id
    wanted_list = db.query(Wanted).filter(
        Wanted.group_id == current_user.group_id
    ).order_by(Wanted.year.desc(), Wanted.month.desc()).all()
    # print([{
    #     "year": wanted.year,
    #     "month": wanted.month,
    #     "status": wanted.status,
    #     "exp_date": wanted.exp_date.isoformat() if wanted.exp_date else None,
    #     "created_at": wanted.created_at.isoformat() if wanted.created_at else None
    # } for wanted in wanted_list])
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
    if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    if getattr(current_user, 'is_head_nurse', False) and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        from db.models import Group
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id

    # 추가
    close_expired_wanted(db)
    
    wanted = db.query(Wanted).filter(
        Wanted.group_id == target_group_id,
        Wanted.year == year,
        Wanted.month == month
    ).first()
    
    if not wanted:
        raise HTTPException(status_code=404, detail="해당 월의 wanted 요청을 찾을 수 없습니다.")
    
    # if wanted.status == 'closed':
    #     raise HTTPException(status_cdoe=400, detail="이미 마감된 wanted 입니다.")
    
    # 수동 마감 추가 : exp_date 현재 시점으로 설정 및 status : closed 로 업데이트
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    now = utc_now + timedelta(hours=9)
    wanted.exp_date = now
    wanted.status = 'closed'
    db.commit()
    db.refresh(wanted)
    
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
    if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    if getattr(current_user, 'is_head_nurse', False) and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        from db.models import Group
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id

    wanted = db.query(Wanted).filter(
        Wanted.group_id == target_group_id,
        Wanted.year == req.year,
        Wanted.month == req.month
    ).first()
    
    if not wanted:
        raise HTTPException(status_code=404, detail="해당 월의 wanted 요청을 찾을 수 없습니다.")
    
    # if wanted.status == 'closed':
    #     raise HTTPException(status_code=400, detail="마감된 wanted 요청의 마감일은 변경할 수 없습니다.")
    
    payload = req.model_dump(exclude_unset=True)
    if "exp_date" in payload:
        wanted.exp_date = req.exp_date
        db.commit()
        db.refresh(wanted)
        close_expired_wanted(db)
        message = "마감일이 제거되었습니다. (마감일 없음)" if req.exp_date is None else "마감일이 성공적으로 변경되었습니다."
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
    # 추가
    close_expired_wanted(db)
    
    try:
        
        import uuid
        trace_id = str(uuid.uuid4())[:8]
        print(f"[INVOKE START] trace_id={trace_id} | nurse_id={current_user.nurse_id} | request={request.request}")
        result = await invoke_and_persist_wanted_service(request, current_user, db)
        
        # 서비스 끝난 후 강제 commit + flush
        db.flush()  # 변경 사항 즉시 반영
        db.commit() # 한번 더 강제 commit
        
        print(f"[INVOKE END] trace_id={trace_id} | 생성된 request_id={result.get('request_id')}")
        # return WantedInvokeResponse(response=result)
        return result
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


@router.post("/close-expired")
def close_expired_wanted_endpoint(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    현재 KST 기준으로 exp_date가 지난 Wanted 요청의 status를 'closed'로 일괄 변경하는 엔드포인트입니다.

    인자:
        db: FastAPI DI로 주입되는 DB 세션 객체.

    반환:
        dict: {"now_kst": "...", "updated": n} 형태의 결과를 반환합니다.
              예: now_kst="2025-01-01T00:00:00", updated=3 이면
              2025-01-01 00:00 (KST) 기준으로 3건의 Wanted가 'closed'로 변경된 것입니다.
    """
    # UTC 기준 현재 시각을 구한 뒤 +9시간을 더해 KST 현재 시각을 계산합니다.
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
    try:
        for wanted in query:
            wanted.status = "closed"
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

    return {"now_kst": now_kst.isoformat(), "updated": updated_count}
