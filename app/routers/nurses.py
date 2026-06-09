from fastapi import APIRouter, Depends, HTTPException
from fastapi import UploadFile, File, Query
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from typing import List, Optional, Dict
import uuid
import tempfile
import os
import random
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
import traceback
import string

from db.client2 import get_db, msdb_manager
from db.models import Nurse as NurseModel
from db.models import Office as OfficeModel
from db.models import Team as TeamModel
from db.models import Group as GroupModel
from db.models import NurseAssignment
from schemas.roster_schema import (
    NurseProfile,
    MoveNurseRequest,
    IntegratedRegisterRequest,
    ExcelValidationRequest,
    NurseSequenceUpdate,
    ReorderPayload,
    ExcelConfirmRequest,
    PersonnelUpdate,
    NurseProfileUpdate,
    PasswordChangeRequest,
    PhoneChangeRequest,
    AddToGroupRequest,
    NurseAssignmentCreate,
    NurseAssignmentUpdate,
    NurseAssignmentResponse,
    NurseAssignmentListResponse,
    NurseMonthlyLimitBulkUpsertRequest,
    NurseMonthlyLimitListResponse,
)
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.assignment_service import (
    create_assignment,
    update_assignment,
    cancel_assignment,
    get_assignments,
    get_assignment_status_counts,
    flush_pending_transfers,
    flush_expired_preceptees,
    flush_expired_dispatches,
    flush_expired_leaves,
    preview_assignment_impact,
)
from services.nurse_service import (
    get_nurses_in_group_service,
    bulk_update_nurses_service,
    move_nurse_service,
    move_nurse_with_active_service,
    reorder_nurses_service,
    get_nurses_filtered_service,
    get_personnel_basic_info_service,
    get_available_members_service,
    add_nurses_to_group_service,
    delete_nurse_service,
    upload_profile_image_service,
    get_profile_image_info_service,
    delete_profile_image_service,
    get_profile_image_url,
    update_nurse_profile_service,
)
from services.nurse_monthly_limit_service import (
    list_nurse_monthly_limits_service,
    upsert_nurse_monthly_limits_service,
)
from services.group_access import resolve_home_group_id, resolve_effective_group, caller_is_head_nurse, resolve_managed_group_ids
from services.excel_service import (
    create_nurse_template,
    # process_excel_upload,
    validate_excel_data,
    save_excel_data,
    create_groups_and_save_data,
    create_nurse_template2,
    # process_excel_upload2,
    upload2_validate,
    upload2_confirm,
    export_members_excel_bytes,
)
from datalayer.member import Member
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse
from utils.utils import set_sms


router = APIRouter(prefix="/nurses", tags=["nurses"])


@router.get("/managed-groups/summary")
async def get_managed_groups_summary(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """현재 사용자가 관리 가능한 그룹 목록 + 간호사 카운트 요약.

    권한별 노출 그룹:
    - ADM(is_master_admin): 같은 office_id 의 모든 그룹
    - HN(hn_auth=='HN'): home group + group.hn_id 에 등록된 그룹
    - 그 외(수간호사·일반 간호사, HN 권한 없음): 토큰 group_id 1개

    카운트 기준 (group_id 축, distinct nurse_id):
    - active_nurse_count: home 소속(nurses.group_id==G_x) + inbound
      (NurseAssignment.target_group_id==G_x AND status='active') 중
      nurses.resignation_date IS NULL 인 간호사. 같은 nurse 가 home·inbound
      양쪽에 잡혀도 1회만 카운트.
    - inactive_nurse_count: home 소속이며 resignation_date IS NOT NULL.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    group_ids = resolve_managed_group_ids(db, current_user)
    if not group_ids:
        return JSONResponse(
            content=jsonable_encoder([]),
            media_type="application/json; charset=utf-8",
        )

    groups = (
        db.query(GroupModel)
        .filter(GroupModel.group_id.in_(group_ids))
        .all()
    )
    groups_map = {g.group_id: g for g in groups}

    home_rows = (
        db.query(
            NurseModel.nurse_id,
            NurseModel.group_id,
            NurseModel.resignation_date,
            NurseModel.active,
        )
        .filter(NurseModel.group_id.in_(group_ids))
        .all()
    )

    inbound_rows = (
        db.query(NurseAssignment.nurse_id, NurseAssignment.target_group_id)
        .join(NurseModel, NurseModel.nurse_id == NurseAssignment.nurse_id)
        .filter(
            NurseAssignment.target_group_id.in_(group_ids),
            NurseAssignment.status == "active",
            NurseModel.resignation_date.is_(None),
            NurseModel.active == 1,
        )
        .all()
    )

    active_set: dict = {gid: set() for gid in group_ids}
    inactive_set: dict = {gid: set() for gid in group_ids}

    # 비활성 정의: active=0 OR resignation_date IS NOT NULL (합집합)
    for nid, gid, resigned, is_active in home_rows:
        if gid not in active_set:
            continue
        if (resigned is not None) or (not bool(is_active)):
            inactive_set[gid].add(nid)
        else:
            active_set[gid].add(nid)

    for nid, tgid in inbound_rows:
        if tgid in active_set:
            active_set[tgid].add(nid)

    data = []
    for gid in group_ids:
        g = groups_map.get(gid)
        if not g:
            continue
        data.append({
            "group_id": str(g.group_id),
            "group_name": str(g.group_name),
            "office_id": str(g.office_id),
            "active_nurse_count": len(active_set[gid]),
            "inactive_nurse_count": len(inactive_set[gid]),
        })

    return JSONResponse(
        content=jsonable_encoder(data),
        media_type="application/json; charset=utf-8",
    )


@router.get("/monthly-limits", response_model=NurseMonthlyLimitListResponse)
async def get_monthly_limits(
    group_id: str,
    nurse_id: str,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    items = list_nurse_monthly_limits_service(
        db=db,
        current_user=current_user,
        group_id=group_id,
        nurse_id=nurse_id,
    )
    return NurseMonthlyLimitListResponse(items=items, meta=None, warnings=None)


@router.put("/monthly-limits", response_model=NurseMonthlyLimitListResponse)
async def put_monthly_limits(
    body: NurseMonthlyLimitBulkUpsertRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    items = upsert_nurse_monthly_limits_service(
        db=db,
        current_user=current_user,
        year=body.year,
        month=body.month,
        limits=[x.model_dump() for x in body.limits],
    )
    _items, _meta, _warnings = items
    return NurseMonthlyLimitListResponse(items=_items, meta=_meta, warnings=_warnings)


def _ensure_office_exists(
    db: Session, office_id: Optional[str], office_name: Optional[str]
) -> None:
    """
    오피스 ID가 있는데 DB `offices` 테이블에 레코드가 없으면 생성합니다.

    - 인자:
        - db(Session): SQLAlchemy 세션
        - office_id(Optional[str]): 오피스 ID
        - office_name(Optional[str]): 오피스명(미입력 시 생성하지 않음)
    - 반환: 없음
    - 예외:
        - sqlalchemy.exc.IntegrityError: 동시 생성 레이스 컨디션 등으로 중복 삽입이 발생한 경우
    - 예시:
        - office_id="A1", office_name="서울병원" → offices에 (A1, 서울병원) 레코드가 없으면 생성
    """
    print("여기다", office_id, office_name)
    if not office_id:
        return
    if not office_name:
        return

    exists = (
        db.query(OfficeModel.office_id)
        .filter(OfficeModel.office_id == office_id)
        .first()
    )
    if exists:
        return

    try:
        db.add(OfficeModel(office_id=office_id, office_name=office_name))
        db.commit()
    except IntegrityError:
        # 동시 요청 등으로 이미 생성된 경우를 대비
        db.rollback()


@router.get("", response_model=List[NurseProfile])
async def get_nurses_in_group(
    office_id: Optional[str] = None,
    group_id: Optional[str] = None,
    nurse_id: Optional[str] = None,  # 신규 파라미터
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    _ensure_office_exists(
        db,
        getattr(current_user, "office_id", None),
        getattr(current_user, "office_name", None),
    )
    # 그룹 스코프: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(비ADM 은 managed 검증).
    _group = resolve_effective_group(db, current_user, group_id, require_group=False)
    # 병동이동 레이지 체크
    if _group:
        flush_pending_transfers(db, _group)
    # 프리셉티 만료 레이지 체크
    flush_expired_preceptees(db)
    # 파견 만료 레이지 체크 (status change only, 안전 작업)
    flush_expired_dispatches(db)
    # 휴직 만료 레이지 체크 (status change only, 안전 작업)
    flush_expired_leaves(db)
    print(
        "current_user",
        current_user.nurse_id,
        current_user.group_id,
        current_user.office_id,
    )
    try:
        # ADM: 필터링 옵션 자유. HN: resolve_managed_group_ids 안의 그룹만 query 허용.
        if current_user.is_master_admin:
            response = get_nurses_filtered_service(
                current_user,
                db,
                office_id=office_id,
                group_id=group_id,
                nurse_id=nurse_id,  # nurse_id 전달
            )
            return response
        # HN/수간호사/일반: query group_id 있으면 권한 검증 후 override, 없으면 토큰 group_id
        override_gid: Optional[str] = None
        if group_id:
            allowed = resolve_managed_group_ids(db, current_user)
            if group_id not in allowed:
                raise HTTPException(
                    status_code=403, detail="해당 병동에 접근할 수 없습니다."
                )
            override_gid = group_id
        return get_nurses_in_group_service(
            current_user,
            db,
            nurse_id=nurse_id,  # nurse_id 전달
            view_group_id=_group,
        )
    except Exception as e:
        print("[DEBUG] [nurses.py - get_nurses_in_group] office_id", office_id)
        print("[DEBUG] [nurses.py - get_nurses_in_group] group_id", group_id)
        print("[DEBUG] [nurses.py - get_nurses_in_group] nurse_id", nurse_id)
        print(
            "[DEBUG] [nurses.py - get_nurses_in_group] current_user",
            current_user.__dict__,
        )
        print("[DEBUG] [nurses.py - get_nurses_in_group] error", e)
        # raise HTTPException(status_code=500, detail=f"간호사 목록 조회 실패: {str(e)}")


# @router.get("/personnel-basic-info")
# async def get_personnel_basic_info(
#     current_user: UserSchema = Depends(get_current_user_from_cookie),
#     db: Session = Depends(get_db)
# ):
#     try:
#         return get_personnel_basic_info_service(current_user, db)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"간호사 기본 정보 조회 실패: {str(e)}")


@router.post("/sequence/save")
async def save_nurse_sequence(
    req: NurseSequenceUpdate,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    단일 간호사 이동/상태변경 (드래그앤드롭 중간 저장 용도)
    """
    try:
        # 그룹: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(그룹전환 안전).
        gid = resolve_effective_group(db, current_user, group_id)
        return move_nurse_with_active_service(
            req.nurse_id, req.new_sequence, req.active, current_user, db, group_id=gid
        )
    except HTTPException:
        raise  # 403/400(권한·그룹) 은 그대로 전파
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"간호사 순서 변경 실패: {str(e)}")


@router.post("/sequence/reorder")
async def reorder_nurses(
    payload: ReorderPayload,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    드래그앤드롭 종료 시점에 한 번 호출하여 서버 기준으로 순서를 확정.
    프론트에서는 active 리스트와 inactive 리스트의 nurse_id 배열을 넘겨주세요.
    """
    try:
        # 그룹: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(그룹전환 안전).
        gid = resolve_effective_group(db, current_user, group_id)
        return reorder_nurses_service(
            payload.active_order, payload.inactive_order, current_user, db, group_id=gid
        )
    except HTTPException:
        raise  # 403/400(권한·그룹) 은 그대로 전파
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"일괄 재정렬 실패: {str(e)}")


@router.post("/bulk-update")
async def bulk_update_nurses(
    nurses_data: List[NurseProfile],
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    try:
        # 그룹: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석. ADM 무지정 시 None(서비스 폴백).
        gid = resolve_effective_group(db, current_user, group_id, require_group=False)
        return bulk_update_nurses_service(
            nurses_data, current_user, db, override_group_id=gid
        )
    except HTTPException:
        raise  # 403/400(권한·그룹) 은 그대로 전파
    except Exception as e:
        print("error1", e)
        raise HTTPException(
            status_code=500, detail=f"간호사 일괄 업데이트 실패: {str(e)}"
        )


@router.get("/template-download")
async def download_template(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """엑셀 템플릿 파일 다운로드"""
    try:
        if not current_user or not caller_is_head_nurse(db, current_user):
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        template_path = create_nurse_template()
        return FileResponse(
            path=template_path,
            filename="간호사_정보_템플릿.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"템플릿 생성 실패: {str(e)}")


@router.get("/template2-download")
async def download_template2(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
):
    """신규 엑셀 템플릿2 (계정ID/이름만) 다운로드 - ADM 전용"""
    try:
        # if not current_user or not current_user.is_master_admin:
        #     raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")
        template_path = create_nurse_template2()
        return FileResponse(
            path=template_path,
            filename="간호사_업로드2_템플릿.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        print("error", e)
        raise HTTPException(status_code=500, detail=f"템플릿2 생성 실패: {str(e)}")


# @router.post("/upload-excel")
# async def upload_excel(
#     file: UploadFile = File(...),
#     current_user: UserSchema = Depends(get_current_user_from_cookie),
#     db: Session = Depends(get_db)
# ):
#     """엑셀 파일 업로드 및 검증"""
#     try:
#         if not current_user or not current_user.is_head_nurse:
#             raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
#         if file.size and file.size > 10 * 1024 * 1024:
#             raise HTTPException(status_code=400, detail="파일 크기는 10MB를 초과할 수 없습니다.")
#         if not file.filename.lower().endswith(('.xlsx', '.xls')):
#             raise HTTPException(status_code=400, detail="지원되지 않는 파일 형식입니다. (.xlsx, .xls만 지원)")
#         with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
#             content = await file.read()
#             tmp_file.write(content)
#             tmp_file_path = tmp_file.name
#         try:
#             result = process_excel_upload(tmp_file_path, current_user, db)
#             return result
#         finally:
#             os.unlink(tmp_file_path)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"엑셀 업로드 실패: {str(e)}")


class Upload2ConfirmRequest(BaseModel):
    rows: List[dict]
    group_id: Optional[str] = None


@router.post("/upload2-validate")
async def upload2_validate_endpoint(
    file: UploadFile = File(...),
    group_id: str = Query(..., description="병동 그룹 ID (필수)"),  # 추가
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """업로드2 - 검증 전용. 오류 목록과 정규화된 행을 반환한다."""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name
        try:
            result = upload2_validate(
                tmp_file_path, current_user, db, group_id=group_id
            )
            return result
        finally:
            os.unlink(tmp_file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"검증 실패: {str(e)}")


# @router.post("/upload2-confirm")
# async def upload2_confirm_endpoint(
#     payload: Upload2ConfirmRequest,
#     current_user: UserSchema = Depends(get_current_user_from_cookie),
#     db: Session = Depends(get_db)
# ):
#     """업로드2 - 검증 통과 후 저장. 오류가 있는 행은 건너뜀."""
#     try:
#         # if not current_user or not current_user.is_master_admin:
#         #     raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")
#         target_group_id = payload.group_id or current_user.group_id
#         result = upload2_confirm(payload.rows, current_user, db, target_group_id)
#         return result
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"저장 실패: {str(e)}")


@router.post("/upload2-confirm")
async def upload2_confirm_endpoint(
    payload: Upload2ConfirmRequest,
    group_id: str = Query(
        ..., description="대상 병동 group_id (필수)"
    ),  # ← 반드시 URL에 ?group_id=... 포함
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """업로드2 - 검증 통과 후 저장. 오류가 있는 행은 건너뜀."""
    try:
        print(f"[DEBUG] 라우터 - 받은 쿼리 group_id: {group_id}")
        print(
            f"[DEBUG] current_user: nurse_id={current_user.nurse_id}, "
            f"group_id={getattr(current_user, 'group_id', '없음')}, "
            f"is_master_admin={current_user.is_master_admin}"
        )

        # 1. 쿼리 파라미터가 있으면 무조건 그걸 우선 사용 (가장 안전)
        target_group_id = group_id

        # 2. 만약 쿼리가 비어있으면 (미래 안전장치) current_user fallback
        if not target_group_id:
            target_group_id = getattr(current_user, "group_id", None)

        # 3. 그래도 없으면 → 에러
        if not target_group_id:
            raise HTTPException(
                status_code=400,
                detail="group_id가 필요합니다. URL에 ?group_id=... 를 반드시 포함해주세요.",
            )

        # ADM인 경우: group_id가 쿼리로 왔는지 로그로 확인 (디버깅용)
        if current_user.is_master_admin:
            print(f"[ADM 모드] 관리자 업로드 - 선택된 병동 group_id: {target_group_id}")

        result = upload2_confirm(payload.rows, current_user, db, target_group_id)
        return result

    except Exception as e:
        print(f"[ERROR] upload2-confirm 엔드포인트 실패: {str(e)}")
        raise HTTPException(status_code=500, detail=f"저장 실패: {str(e)}")


@router.get("/available-members")
async def get_available_members(
    group_id: str = Query(..., description="현재 선택된 병동 그룹 ID"),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    현재 그룹에 속하지 않은 동일 오피스 멤버 목록 조회.
    - 근무자 등록 모달에서 사용
    - MSSQL 전체 멤버 중 현재 group_id에 이미 등록된 간호사를 제외하고 반환
    """
    try:
        office_id = current_user.office_id
        if not office_id:
            raise HTTPException(
                status_code=400, detail="office_id를 확인할 수 없습니다."
            )

        result = get_available_members_service(office_id, group_id, db)
        if isinstance(result, list) and result:
            unique_nurse_ids = set(
                item.get("nurse_id") for item in result if item.get("nurse_id")
            )
            unique_count = len(unique_nurse_ids)
            print(
                f"[DEBUG] available-members 조회 성공 - 총 {len(result)}건, 고유 nurse_id {unique_count}개, group_id = {group_id}"
            )
        else:
            print(
                f"[DEBUG] available-members 조회 성고 - 빈 리스트 반환, group_id = {group_id}"
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] available-members 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=f"멤버 목록 조회 실패: {str(e)}")


@router.post("/add-to-group")
async def add_nurses_to_group(
    payload: AddToGroupRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    선택된 멤버를 현재 그룹에 근무자로 추가.
    - nurses 테이블에 이미 존재 (다른 group_id): group_id만 변경
    - nurses 테이블에 미존재: MSSQL 멤버 정보로 신규 생성
    """
    try:
        if not caller_is_head_nurse(db, current_user) and not current_user.is_master_admin:
            raise HTTPException(
                status_code=403, detail="수간호사 또는 관리자만 접근 가능합니다."
            )

        office_id = current_user.office_id
        if not office_id:
            raise HTTPException(
                status_code=400, detail="office_id를 확인할 수 없습니다."
            )

        # 대상 그룹을 호출자가 관리하는지 검증(HN=groups.hn_id / ADM=office). 토큰 group_id 무관.
        target_gid = resolve_effective_group(db, current_user, payload.group_id)
        result = add_nurses_to_group_service(
            payload.nurse_ids, target_gid, office_id, db
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] add-to-group 실패: {e}")
        raise HTTPException(status_code=500, detail=f"근무자 추가 실패: {str(e)}")


@router.get("/export-members")
async def export_members(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
):
    """ADM 전용: 현재 오피스 전체 인원 정보를 엑셀로 내려받기."""
    try:
        if not current_user or not current_user.is_master_admin:
            raise HTTPException(
                status_code=403, detail="마스터 관리자만 접근 가능합니다."
            )

        print(f"[DEBUG] current_user={current_user}")
        print(
            f"[DEBUG] current_user.office_id={getattr(current_user, 'office_id', None)}"
        )

        content = export_members_excel_bytes(current_user.office_id)
        print(
            f"[DEBUG] export result type={type(content)} size={len(content) if content else 0}"
        )

        filename = f"구성원_목록_{current_user.office_id}.xlsx"
        import urllib.parse

        encoded_filename = urllib.parse.quote(filename)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
            },
        )

    except Exception as e:
        print("[ERROR] export_members_excel error:", e)
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.post("/validate-excel")
async def validate_excel_data_endpoint(
    request: ExcelValidationRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """업로드된 데이터 유효성 검증"""
    try:
        if not current_user or not caller_is_head_nurse(db, current_user):
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        result = validate_excel_data(request.data, current_user, db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 검증 실패: {str(e)}")


@router.post("/confirm-upload")
async def confirm_upload(
    request: ExcelConfirmRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """검증된 데이터 최종 저장"""
    try:
        if not current_user or not caller_is_head_nurse(db, current_user):
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        filtered_data = [
            data
            for i, data in enumerate(request.data)
            if i < len(request.include_rows) and request.include_rows[i]
        ]
        if request.new_groups_to_create:
            result = create_groups_and_save_data(
                filtered_data, request.new_groups_to_create, current_user, db
            )
        else:
            result = save_excel_data(filtered_data, current_user, db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 저장 실패: {str(e)}")


@router.post("/integrated-register")
async def integrated_register(
    payload: IntegratedRegisterRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    직접 입력으로 신규 직원 계정 생성 + 근무자 등록 (통합)
    """
    try:
        if not current_user.is_master_admin:
            raise HTTPException(status_code=403, detail="관리자 권한 필요")

        result = integrated_member_and_nurse_register(
            payload.members,
            current_user,
            db,
            payload.group_id,  # 프론트에서 반드시 보내야 함
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"통합 등록 실패: {str(e)}")


# 마이 페이지 테스트
# 인증번호 임시 저장소 (프로덕션에서는 Redis로 교체 권장)
verification_cache: Dict[
    str, Dict
] = {}  # {nurse_id: {'code': str, 'expires': datetime, 'new_phone': str or None, 'new_password': str or None}}


@router.get("/personnel-basic-info")
async def get_personnel_basic_info(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    try:
        # nurse_id 기준으로만 조회 (group switch 이후에도 마이페이지 접근 가능하도록)
        # team 관계를 joinedload로 한 번에 가져옴
        nurse = (
            db.query(NurseModel)
            .options(joinedload(NurseModel.team))
            .filter(NurseModel.nurse_id == current_user.nurse_id)
            .first()
        )

        if not nurse:
            # ADM(관리자)은 nurses 테이블에 레코드가 없으므로 기본 정보 반환
            if getattr(current_user, "is_master_admin", False):
                # 그룹웨어 DB에서 관리자 정보 조회
                _acct = getattr(current_user, "account_id", "")
                _gw_rows = msdb_manager.fetch_all(Member.member_view(), params=(_acct,)) if _acct else []
                _gw = _gw_rows[0] if _gw_rows else {}
                _birth = _gw.get("DateOfBirth")
                _age = None
                if _birth:
                    try:
                        _bd = datetime.strptime(str(_birth)[:10], "%Y-%m-%d").date() if isinstance(_birth, str) else _birth
                        _age = (date.today() - _bd).days // 365
                    except Exception:
                        pass
                _join = _gw.get("JoinDate")
                _tenure = ""
                if _join:
                    try:
                        _jd = _join.date() if hasattr(_join, "date") else _join
                        if isinstance(_jd, date) and _jd <= date.today():
                            _rd = relativedelta(date.today(), _jd)
                            _tenure = f"{_rd.years}년 {_rd.months}개월"
                    except Exception:
                        pass
                return {
                    "is_admin": True,
                    "account_id": _gw.get("account_id", _acct),
                    "nurse_id": str(_gw.get("nurse_id", getattr(current_user, "nurse_id", ""))),
                    "name": _gw.get("name", getattr(current_user, "name", "관리자")),
                    "office_name": _gw.get("office_name", getattr(current_user, "office_name", "")),
                    "group_name": "",
                    "team_name": _gw.get("mb_partName", ""),
                    "emp_num": str(_gw.get("nurse_id", "")),
                    "role": "ADM",
                    "level_": _gw.get("OfficialTitleName", ""),
                    "birth_date": str(_birth)[:10] if _birth else None,
                    "age": _age,
                    "gender": _gw.get("gender"),
                    "joining_date": str(_join)[:10] if _join else None,
                    "experience": int(_gw.get("career") or 0) if _gw.get("career") else None,
                    "tenure": _tenure,
                    "tenure_display": _tenure,
                    "work_place": _gw.get("office_name", ""),
                    "phone_number": _gw.get("PortableTel"),
                    "email": _gw.get("Email") or None,
                    "profile_image_url": None,
                }
            raise HTTPException(
                status_code=404, detail="간호사 정보를 찾을 수 없습니다."
            )

        # 재직기간 계산
        tenure_display = "미등록"
        if nurse.joining_date:
            today = date.today()
            join_date = (
                nurse.joining_date.date()
                if hasattr(nurse.joining_date, "date")
                else nurse.joining_date
            )
            if join_date <= today:
                delta = relativedelta(today, join_date)
                tenure_display = f"{delta.years}년 {delta.months}개월"

        age = None
        if nurse.birth_date:
            try:
                # birth_date가 "YYYY-MM-DD" 형식 문자열이라고 가정
                birth_date = datetime.strptime(nurse.birth_date, "%Y-%m-%d").date()
                today = date.today()
                age = today.year - birth_date.year
                if (today.month, today.day) < (birth_date.month, birth_date.day):
                    age -= 1
                # 음수 방지 (미래 생일 등)
                age = max(0, age)
            except ValueError:
                # 날짜 형식이 잘못된 경우 무시
                age = None

        work_place = "미등록"
        if nurse.team and nurse.team.team_name:
            work_place = nurse.team.team_name

        return {
            "nurse_id": nurse.nurse_id,
            "name": nurse.name,
            "account_id": nurse.account_id,
            "emp_num": nurse.emp_num,
            "gender": nurse.gender,
            "birth_date": nurse.birth_date,
            "age": age,
            "joining_date": nurse.joining_date.isoformat()
            if nurse.joining_date
            else None,
            "experience": nurse.experience,
            "phone_number": nurse.phone_number,
            "email": nurse.email or "",
            "role": nurse.role,
            "level_": nurse.level_,
            "is_head_nurse": nurse.is_head_nurse,
            "tenure": tenure_display,
            "work_place": work_place,
            "profile_image_url": get_profile_image_url(nurse.profile_image_key),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"간호사 기본 정보 조회 실패: {str(e)}"
        )


@router.patch("/personnel-basic-info")
async def partial_update_personnel_basic_info(
    update_data: PersonnelUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    nurse = (
        db.query(NurseModel)
        .filter(NurseModel.nurse_id == current_user.nurse_id)
        .first()
    )

    if not nurse:
        raise HTTPException(404, "간호사 정보 없음")

    if update_data.experience is not None:
        nurse.experience = update_data.experience

    if update_data.email is not None:
        nurse.email = update_data.email

    db.commit()
    db.refresh(nurse)

    # email 변경 시 MSSQL dual write (비밀번호 찾기 본인확인용)
    if update_data.email is not None:
        try:
            msdb_manager.execute(
                "UPDATE bizwiz20db.Member SET Email = %s WHERE EmpSeqNo = %s",
                (update_data.email, current_user.nurse_id),
            )
        except Exception as e:
            import logging
            logging.warning(f"[personnel-basic-info] MSSQL email 동기화 실패: {e}")

    return {"message": "정보가 성공적으로 수정되었습니다"}


@router.post("/profile-image")
async def upload_profile_image(
    file: UploadFile = File(...),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    try:
        result = upload_profile_image_service(
            current_user=current_user, db=db, image_file=file
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"프로필 이미지 업로드 실패: {str(e)}"
        )


@router.get("/profile-image")
async def get_profile_image(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    try:
        return get_profile_image_info_service(current_user=current_user, db=db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"프로필 이미지 조회 실패: {str(e)}"
        )


@router.delete("/profile-image")
async def delete_profile_image(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    try:
        return delete_profile_image_service(current_user=current_user, db=db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"프로필 이미지 삭제 실패: {str(e)}"
        )


@router.put("/change-password")
async def change_password(
    payload: PasswordChangeRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    nurse_id = current_user.nurse_id
    print(f"[DEBUG] change-password 요청 - nurse_id: {nurse_id}")
    print(f"[DEBUG] payload: {payload.dict()}")

    # 1. 새 비밀번호와 확인 비밀번호 일치 여부 체크
    if payload.new_password != payload.confirm_password:
        raise HTTPException(400, "새 비밀번호와 확인 비밀번호가 일치하지 않습니다")

    # 2. 기존 비밀번호와 새 비밀번호가 동일한지 체크 (보안상 금지)
    if payload.new_password == payload.current_password:
        raise HTTPException(400, "기존 비밀번호와 동일한 비밀번호는 사용할 수 없습니다")

    # 3. 기존 비밀번호가 맞는지 확인 (pwdcompare 사용)
    print("[DEBUG] 기존 비밀번호 검증 시작")
    result = msdb_manager.fetch_one(
        "SELECT pwdcompare(%s, MemberPassEncrypt) AS IsCorrect FROM bizwiz20db.Member_Login WHERE EmpSeqNo = %s",
        (payload.current_password, nurse_id),
    )
    print(f"[DEBUG] pwdcompare 결과: {result}")

    # result가 정수로 나오는 경우 (기존 문제 해결)
    is_correct = (
        result
        if isinstance(result, int)
        else result.get("IsCorrect")
        if isinstance(result, dict)
        else None
    )

    if is_correct != 1:
        raise HTTPException(401, "기존 비밀번호가 올바르지 않습니다")

    # 4. 비밀번호 업데이트
    print("[DEBUG] 비밀번호 업데이트 시작")
    update_result = msdb_manager.execute(
        Member.member_pwd_update(),
        (payload.new_password, payload.new_password, current_user.office_id, nurse_id),
    )
    print(f"[DEBUG] 업데이트 결과: {update_result}")

    return {"message": "비밀번호가 성공적으로 변경되었습니다"}


@router.post("/change-phone/send-code")
async def send_phone_verification_code(
    payload: PhoneChangeRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    nurse_id = current_user.nurse_id
    code = "".join(random.choices(string.digits, k=6))

    verification_cache[nurse_id] = {
        "code": code,
        "expires": datetime.now() + timedelta(minutes=3),
        "new_phone": payload.new_phone_number,
    }

    sendPhoneNumber = "0269593214"

    # nurses 테이블에서 phone_number 가져오기
    userPhoneNumber = payload.new_phone_number.replace("-", "")

    # 번호 형식 간단 검증 (선택사항)
    if (
        not userPhoneNumber.startswith("010")
        or len(userPhoneNumber) != 11
        or not userPhoneNumber.isdigit()
    ):
        raise HTTPException(
            400, "유효한 휴대폰 번호를 입력해주세요 (010으로 시작하는 11자리 숫자)"
        )

    smsMessage = f"[메디통] 휴대폰 번호 변경 인증번호: {code} (3분 이내 입력)"

    from utils.utils import set_sms

    sms_result = set_sms(userPhoneNumber, sendPhoneNumber, smsMessage)

    print(f"[DEBUG] SMS 발송 요청 - 새 번호: {userPhoneNumber}, 결과: {sms_result}")

    if sms_result.get("result") == "fail":
        raise HTTPException(500, "인증번호 발송에 실패했습니다")

    return {"message": "인증번호가 입력하신 새 번호로 발송되었습니다"}


@router.put("/change-phone/verify")
async def verify_and_update_phone(
    payload: PhoneChangeRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    nurse_id = current_user.nurse_id
    cached = verification_cache.get(nurse_id)

    if not cached:
        raise HTTPException(400, "인증 정보가 없거나 만료되었습니다")

    if datetime.now() > cached["expires"]:
        del verification_cache[nurse_id]
        raise HTTPException(410, "인증 시간이 초과되었습니다")

    if payload.verification_code != cached["code"]:
        raise HTTPException(400, "인증번호가 올바르지 않습니다")

    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
    if nurse:
        nurse.phone_number = cached["new_phone"]
        db.commit()

    msdb_manager.execute(
        "UPDATE bizwiz20db.Member SET PortableTel = %s WHERE EmpSeqNo = %s",
        (cached["new_phone"], nurse_id),
    )

    del verification_cache[nurse_id]

    return {"message": "휴대폰 번호가 성공적으로 변경되었습니다"}


# ── NurseAssignment 엔드포인트 (동적 경로보다 먼저 선언) ──


@router.post(
    "/assignments",
    response_model=NurseAssignmentResponse,
    deprecated=True,
)
async def create_nurse_assignment(
    req: NurseAssignmentCreate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """[DEPRECATED] 배정/상태 변경 등록.

    신규 호출은 `PATCH /nurses/{nurse_id}`에 `assignment: {operation: "create", ...}` payload로 대체해 주세요.
    (파견/휴직/퇴사/프리셉티/병동이동)
    """
    return create_assignment(req, db, current_user=current_user)


@router.get("/assignments", response_model=NurseAssignmentListResponse)
async def get_nurse_assignments(
    group_id: Optional[str] = None,
    nurse_id: Optional[str] = None,
    status: Optional[str] = "active",
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """배정 이력 조회.

    응답 구조:
    - items: 현재 status 필터가 적용된 레코드 목록
    - counts: 동일 범위 전체의 status 별 카운트 (필터 무관)
    - total: counts 합계
    - applied_status: 사용된 status 값

    status 기본값은 'active' — cancelled/completed 등 비활성 이력은 기본 미노출.
    전체 이력이 필요하면 `status=all` 로 호출.
    """
    office_id = getattr(current_user, "office_id", None)
    if not office_id:
        raise HTTPException(status_code=400, detail="office_id가 필요합니다.")
    _group = group_id or resolve_home_group_id(db, current_user)
    _status = None if status == "all" else status
    items = get_assignments(db, office_id, group_id=_group, nurse_id=nurse_id, status=_status)
    counts = get_assignment_status_counts(db, office_id, group_id=_group, nurse_id=nurse_id)
    total = counts.active + counts.completed + counts.cancelled + counts.on_hold
    return NurseAssignmentListResponse(
        items=items,
        counts=counts,
        total=total,
        applied_status=status,
    )


class AssignmentPreviewRequest(BaseModel):
    """배정 dry-run 영향 분석 요청.

    실제 DB 변경 없이 영향만 계산. 확정 직전 운영자가 보는 용도.
    """
    nurse_id: str
    reason: str  # 파견 / 병동이동 / 휴직 / 복직 / 프리셉티 등
    start_date: date
    target_group_id: Optional[str] = None
    expected_end_date: Optional[date] = None
    exclude_id: Optional[int] = None  # update 시 자기 자신 제외용


@router.post("/assignments/preview")
async def preview_nurse_assignment(
    req: AssignmentPreviewRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """배정 생성/수정 전 영향 분석 (dry-run).

    돌려보지 않고 영향만 계산해 반환:
    - conflicts: 기간 겹침 active 배정
    - nml_affected: 자동 group_id update 대상 NML
    - wanted_affected: 발효 월 이후 wanted 카운트 (안 건드림, 인지용)
    - schedules_to_check: 재생성 검토 대상 schedule
    - notifications: 알림 대상

    참조: docs/NURSE_ASSIGNMENT_CRON_DESIGN.md §5
    """
    return preview_assignment_impact(
        db,
        nurse_id=req.nurse_id,
        reason=req.reason,
        start_date=req.start_date,
        target_group_id=req.target_group_id,
        expected_end_date=req.expected_end_date,
        exclude_id=req.exclude_id,
    )


@router.put(
    "/assignments/{assignment_id}",
    response_model=NurseAssignmentResponse,
    deprecated=True,
)
async def update_nurse_assignment(
    assignment_id: int,
    req: NurseAssignmentUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """[DEPRECATED] 배정 수정 (기간/상태 변경).

    신규 호출은 `PATCH /nurses/{nurse_id}`에 `assignment: {operation: "update", assignment_id, ...}` payload로 대체해 주세요.
    """
    return update_assignment(assignment_id, req, db, current_user=current_user)


@router.delete(
    "/assignments/{assignment_id}",
    response_model=NurseAssignmentResponse,
    deprecated=True,
)
async def delete_nurse_assignment(
    assignment_id: int,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """[DEPRECATED] 배정 취소 (status → cancelled).

    신규 호출은 `PATCH /nurses/{nurse_id}`에 `assignment: {operation: "cancel", assignment_id}` payload로 대체해 주세요.
    """
    return cancel_assignment(assignment_id, db, current_user=current_user)


@router.get("/{nurse_id}", response_model=NurseProfile)
async def get_nurse_by_id(
    nurse_id: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """단일 간호사 프로필 조회.

    통합 권한 헬퍼(can_caller_access_nurse)로 ADM / self / home·managed source /
    inbound (파견·병동이동) 모두 통과시킨 뒤, 그룹 필터를 건너뛰고 직접 조회한다.

    group_id (Optional):
        사이드프로필 view 컨텍스트. 명시 시 해당 그룹의 inbound assignment 기준으로
        target_* overlay 가 적용됨. 본인 home group 외 값이면 managed groups 검증.
    """
    try:
        from services.group_access import can_caller_access_nurse, assert_caller_can_access_group
        if not can_caller_access_nurse(db, current_user, nurse_id):
            raise HTTPException(status_code=404, detail="간호사를 찾을 수 없습니다")

        if group_id and str(group_id) != str(getattr(current_user, "group_id", "")):
            assert_caller_can_access_group(db, current_user, group_id)

        # 권한은 can_caller_access_nurse 로 이미 통과 → 조회는 그룹필터 없이 직접 한다
        # (docstring 의도). 라우터가 home 그룹으로만 필터하면 HN 의 비-home 관리병동 간호사가
        # 404 로 숨던 버그가 있었다. group_id 는 overlay(파견·병동이동 target_*) view 컨텍스트로만
        # 넘기고, 미지정 시엔 home overlay 로 폴백한다.
        _view_gid = (
            None if group_id in (None, "", "null", "undefined", "None") else group_id
        )
        if current_user.is_master_admin:
            result = get_nurses_filtered_service(
                current_user, db, nurse_id=nurse_id,
            )
        else:
            try:
                result = get_nurses_in_group_service(
                    current_user, db, nurse_id=nurse_id,
                    view_group_id=_view_gid,
                    skip_group_filter=True,
                )
            except Exception:
                result = None
        if not result:
            raise HTTPException(status_code=404, detail="간호사를 찾을 수 없습니다")
        return result[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"간호사 조회 실패: {str(e)}")


@router.patch("/{nurse_id}")
async def update_nurse_profile(
    nurse_id: str,
    update_data: NurseProfileUpdate,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """간호사 프로필 수정.

    group_id: 호출 view 의 그룹(=caller_group_id). 미지정 시 current_user.group_id 사용.
    target view(인바운드 간호사 수정) 에서 호출 시 명시 전달해야 target_* overlay 가
    정상 적용된다 (미전달 시 항상 source 분기로 처리되어 nurse 자체 컬럼만 변경됨).
    """
    try:
        return update_nurse_profile_service(
            nurse_id, update_data, current_user, db, view_group_id=group_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"간호사 정보 수정 실패: {str(e)}")


@router.delete("/{nurse_id}")
async def delete_nurse(
    nurse_id: str,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    특정 간호사 삭제 API
    - 프론트에서 휴지통 아이콘 클릭 시 호출
    """
    try:
        result = delete_nurse_service(nurse_id, current_user, db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"간호사 삭제 실패: {str(e)}")
