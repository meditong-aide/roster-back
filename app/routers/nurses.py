from fastapi import APIRouter, Depends, HTTPException
from fastapi import UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
import tempfile
import os

from db.client2 import get_db
from db.models import Nurse as NurseModel
from schemas.roster_schema import NurseProfile, MoveNurseRequest
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import ExcelValidationRequest, NurseSequenceUpdate, ReorderPayload, ExcelConfirmRequest
from services.nurse_service import (
    get_nurses_in_group_service,
    bulk_update_nurses_service,
    move_nurse_service,
    move_nurse_with_active_service,
    reorder_nurses_service,
    get_nurses_filtered_service,
)
from services.excel_service import (
    create_nurse_template, 
    process_excel_upload, 
    validate_excel_data,
    save_excel_data,
    create_groups_and_save_data,
    create_nurse_template2,
    # process_excel_upload2,
    upload2_validate,
    upload2_confirm,
    export_members_excel_bytes,
)
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse, Response


router = APIRouter(
    prefix="/nurses",
    tags=["nurses"]
)

@router.get("", response_model=List[NurseProfile])
async def get_nurses_in_group(
    office_id: Optional[str] = None,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):

    print('group_id', group_id)
    print('current_user.group_id', current_user.group_id)
    try:
        # ADM는 필터링 옵션 허용, 일반/수간호사는 자신의 그룹만
        if office_id or group_id:
            if not current_user or not current_user.is_master_admin:
                raise HTTPException(status_code=403, detail="마스터 관리자만 필터 조회가 가능합니다.")
            return get_nurses_filtered_service(current_user, db, office_id=office_id, group_id=group_id)
        return get_nurses_in_group_service(current_user, db)
    except Exception as e:
        print('error', e)
        # raise HTTPException(status_code=500, detail=f"간호사 목록 조회 실패: {str(e)}")

@router.get("/personnel-basic-info")
async def get_personnel_basic_info(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        return get_personnel_basic_info_service(current_user, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"간호사 기본 정보 조회 실패: {str(e)}")



@router.post("/sequence/save")
async def save_nurse_sequence(
    req: NurseSequenceUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    단일 간호사 이동/상태변경 (드래그앤드롭 중간 저장 용도)
    """
    try:
        return move_nurse_with_active_service(req.nurse_id, req.new_sequence, req.active, current_user, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"간호사 순서 변경 실패: {str(e)}")

@router.post("/sequence/reorder")
async def reorder_nurses(
    payload: ReorderPayload,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    드래그앤드롭 종료 시점에 한 번 호출하여 서버 기준으로 순서를 확정.
    프론트에서는 active 리스트와 inactive 리스트의 nurse_id 배열을 넘겨주세요.
    """
    try:
        return reorder_nurses_service(payload.active_order, payload.inactive_order, current_user, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"일괄 재정렬 실패: {str(e)}")


@router.post("/bulk-update")
async def bulk_update_nurses(
    nurses_data: List[NurseProfile],
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:

        # ADM이 group_id를 지정하면 해당 병동을 대상으로 업데이트 허용
        return bulk_update_nurses_service(nurses_data, current_user, db, override_group_id=group_id)
    except Exception as e:
        print('error1', e)
        raise HTTPException(status_code=500, detail=f"간호사 일괄 업데이트 실패: {str(e)}") 

@router.get("/template-download")
async def download_template(
    current_user: UserSchema = Depends(get_current_user_from_cookie)
):
    """엑셀 템플릿 파일 다운로드"""
    try:
        if not current_user or not current_user.is_head_nurse:
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        template_path = create_nurse_template()
        return FileResponse(
            path=template_path,
            filename="간호사_정보_템플릿.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"템플릿 생성 실패: {str(e)}")

@router.get("/template2-download")
async def download_template2(
    current_user: UserSchema = Depends(get_current_user_from_cookie)
):
    """신규 엑셀 템플릿2 (계정ID/이름만) 다운로드 - ADM 전용"""
    try:
        # if not current_user or not current_user.is_master_admin:
        #     raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")
        template_path = create_nurse_template2()
        return FileResponse(
            path=template_path,
            filename="간호사_업로드2_템플릿.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"템플릿2 생성 실패: {str(e)}")

@router.post("/upload-excel")
async def upload_excel(
    file: UploadFile = File(...),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """엑셀 파일 업로드 및 검증"""
    try:
        if not current_user or not current_user.is_head_nurse:
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        if file.size and file.size > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="파일 크기는 10MB를 초과할 수 없습니다.")
        if not file.filename.lower().endswith(('.xlsx', '.xls')):
            raise HTTPException(status_code=400, detail="지원되지 않는 파일 형식입니다. (.xlsx, .xls만 지원)")
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name
        try:
            result = process_excel_upload(tmp_file_path, current_user, db)
            return result
        finally:
            os.unlink(tmp_file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"엑셀 업로드 실패: {str(e)}")

# @router.post("/upload2-excel")
# async def upload_excel2(
#     file: UploadFile = File(...),
#     current_user: UserSchema = Depends(get_current_user_from_cookie),
#     db: Session = Depends(get_db),
#     group_id: Optional[str] = None,
# ):
#     print('[/upload2-excel] group_id', group_id)
#     """엑셀 업로드2: mdt_temp에서 account_id 존재 여부를 office_id 기준으로 검증 - ADM 전용"""
#     try:
#         if not current_user or not current_user.is_master_admin:
#             raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")
#         if file.size and file.size > 10 * 1024 * 1024:
#             raise HTTPException(status_code=400, detail="파일 크기는 10MB를 초과할 수 없습니다.")
#         if not file.filename.lower().endswith(('.xlsx', '.xls')):
#             raise HTTPException(status_code=400, detail="지원되지 않는 파일 형식입니다. (.xlsx, .xls만 지원)")
#         with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
#             content = await file.read()
#             tmp_file.write(content)
#             tmp_file_path = tmp_file.name
#         print('group_id11', group_id)
#         try:
#             result = process_excel_upload2(tmp_file_path, current_user, db, target_group_id=group_id)
#             return result
#         finally:
#             os.unlink(tmp_file_path)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"엑셀 업로드2 실패: {str(e)}")


class Upload2ConfirmRequest(BaseModel):
    rows: List[dict]
    group_id: Optional[str] = None


@router.post("/upload2-validate")
async def upload2_validate_endpoint(
    file: UploadFile = File(...),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """업로드2 - 검증 전용. 오류 목록과 정규화된 행을 반환한다."""
    try:
        # if not current_user or not current_user.is_master_admin:
        #     raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name
        try:
            result = upload2_validate(tmp_file_path, current_user, db)
            return result
        finally:
            os.unlink(tmp_file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"검증 실패: {str(e)}")


@router.post("/upload2-confirm")
async def upload2_confirm_endpoint(
    payload: Upload2ConfirmRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """업로드2 - 검증 통과 후 저장. 오류가 있는 행은 건너뜀."""
    try:
        # if not current_user or not current_user.is_master_admin:
        #     raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")
        target_group_id = payload.group_id or current_user.group_id
        result = upload2_confirm(payload.rows, current_user, db, target_group_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"저장 실패: {str(e)}")


@router.get("/export-members")
async def export_members(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
):
    """ADM 전용: 현재 오피스 전체 인원 정보를 엑셀로 내려받기."""
    try:
        if not current_user or not current_user.is_master_admin:
            raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")

        print(f"[DEBUG] current_user={current_user}")
        print(f"[DEBUG] current_user.office_id={getattr(current_user, 'office_id', None)}")

        content = export_members_excel_bytes(current_user.office_id)
        print(f"[DEBUG] export result type={type(content)} size={len(content) if content else 0}")

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
    db: Session = Depends(get_db)
):
    """업로드된 데이터 유효성 검증"""
    try:
        if not current_user or not current_user.is_head_nurse:
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        result = validate_excel_data(request.data, current_user, db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 검증 실패: {str(e)}")

@router.post("/confirm-upload")
async def confirm_upload(
    request: ExcelConfirmRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """검증된 데이터 최종 저장"""
    try:
        if not current_user or not current_user.is_head_nurse:
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        filtered_data = [
            data for i, data in enumerate(request.data) 
            if i < len(request.include_rows) and request.include_rows[i]
        ]
        if request.new_groups_to_create:
            result = create_groups_and_save_data(filtered_data, request.new_groups_to_create, current_user, db)
        else:
            result = save_excel_data(filtered_data, current_user, db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 저장 실패: {str(e)}") 