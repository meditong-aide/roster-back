from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from db.client2 import get_db
from db.models import Shift, Nurse, ScheduleEntry, ShiftManage, RosterConfig, Group
from schemas.auth_schema import User as UserSchema
from routers.auth import get_current_user_from_cookie
from services.group_access import resolve_effective_group, caller_is_head_nurse
from schemas.roster_schema import ShiftAddRequest, RemoveShiftRequest, MoveShiftRequest, ShiftManageSaveRequest, ShiftUpdateRequest, ShiftUploadConfirmRequest, ShiftImportRequest
from services.shift_service import (
    get_shifts_service as get_shifts_service_mysql,
    add_shift_service,
    update_shift_service,
    remove_shift_service,
    move_shift_service,
    create_shift_template,
    shift_upload_validate,
    shift_upload_confirm,
    get_available_shifts_for_import,
    import_shifts_to_group,
)
from typing import Optional, Any, List
import os
import tempfile
from services.shift_service_mssql import get_shifts_service as get_shifts_service_mssql, get_shifts_paged_service
from services.group_access import resolve_managed_group_ids
from datetime import timedelta, datetime

# def convert_time(value):
#     if isinstance(value, str):  # '06:00' → timedelta
#         h, m = map(int, value.split(":"))
#         return timedelta(hours=h, minutes=m)
#     return value


def convert_time(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parts = value.split(":")
            if len(parts) >= 2:
                h, m = map(int, parts[:2])
                return timedelta(hours=h, minutes=m)
            raise ValueError("Invalid time format")
        except Exception as e:
            print(f"convert_time error: {str(e)}, value: {value}")
            return None
    return value


router = APIRouter(
    tags=["shifts"]
)
templates = Jinja2Templates(directory="app/templates")
from dotenv import load_dotenv
load_dotenv()

# [Shifts] - 모든 시프트 정보 조회
@router.get("/shifts")
async def get_shifts(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)   
):

    try:
        backend = os.getenv("DB_BACKEND", "mysql").lower()
        override_gid = None
        if group_id:
            if not current_user:
                raise HTTPException(status_code=401, detail="인증이 필요합니다.")
            allowed = resolve_managed_group_ids(db, current_user)
            if group_id not in allowed:
                raise HTTPException(status_code=403, detail="해당 병동에 접근할 수 없습니다.")
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g or g.office_id != current_user.office_id:
                raise HTTPException(status_code=403, detail="해당 병동에 접근할 수 없습니다.")
            override_gid = group_id
        backend = 'mssql'
        if backend == "mssql":
            
            shifts = get_shifts_service_mssql(current_user, db, override_gid)
        else:
            shifts = get_shifts_service_mysql(current_user, db, override_gid)
        for shift in shifts:
            shift["start_time"] = convert_time(shift["start_time"])
            shift["end_time"] = convert_time(shift["end_time"])
        return shifts
    except HTTPException:
        raise
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=f"시프트 정보 조회 실패: {str(e)}")


@router.get("/shifts/paged")
async def get_shifts_paged(
    group_id: str,
    cursor: Optional[str] = None,
    limit: int = 20,
    q: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """근무코드 목록 cursor 페이징(읽기 전용) — 기존 /shifts 와 별개의 추가 엔드포인트.

    응답: {"result": {"items": [...], "nextCursor": str|None, "total": int}}.
    nextCursor 가 None 이면 마지막 페이지. cursor 는 직전 응답값을 그대로 전달한다.
    정렬은 서버 고정(sequence ASC, id ASC) — 프론트 재정렬 금지.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    allowed = resolve_managed_group_ids(db, current_user)
    if group_id not in allowed:
        raise HTTPException(status_code=403, detail="해당 병동에 접근할 수 없습니다.")
    g = db.query(Group).filter(Group.group_id == group_id).first()
    if not g or g.office_id != current_user.office_id:
        raise HTTPException(status_code=403, detail="해당 병동에 접근할 수 없습니다.")
    try:
        page = get_shifts_paged_service(
            current_user, db, group_id, cursor=cursor, limit=limit, q=q
        )
        for item in page["items"]:
            item["start_time"] = convert_time(item["start_time"])
            item["end_time"] = convert_time(item["end_time"])
        return {"result": page}
    except HTTPException:
        raise
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=f"시프트 페이징 조회 실패: {str(e)}")


def _format_time_display(shift):
    """근무 시간 정보를 표시용으로 포맷팅"""
    if 'allday' in shift:
        if shift.allday == 1:
            return '종일'
    elif shift.start_time and shift.end_time:
        return f'{shift.start_time} ~ {shift.end_time}'
    elif shift.type:
        return shift.type


def _is_weekly_off_slot(slot_data: dict[str, Any]) -> bool:
    """
    프론트 표시용 주휴 슬롯(main_code='O', shift_slot=4, codes에 '주') 여부를 판별합니다.
    """
    main_code = slot_data.get("main_code")
    shift_slot = slot_data.get("shift_slot")
    codes = slot_data.get("codes") or []
    return main_code == "O" and shift_slot == 4 and any(code == "주" for code in codes)

@router.post("/shifts/add")
async def add_shift(
    req: ShiftAddRequest,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        print('----------------------------------[shifts/add] group_id', group_id)
        result = add_shift_service(req, current_user, db, group_id)
    except HTTPException:
        raise
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"근무코드 추가 실패: {str(e)}")

@router.post("/shifts/update")
async def update_shift(
    req: ShiftUpdateRequest,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        print('[shifts/update] group_id', group_id)
        result = update_shift_service(req, current_user, db, group_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"근무코드 수정 실패: {str(e)}")
@router.post("/shifts/remove")
async def remove_shift(
    req: RemoveShiftRequest,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        return remove_shift_service(req, current_user, db, group_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"근무코드 삭제 실패: {str(e)}")

@router.post("/shifts/move")
async def move_shift(
    req: MoveShiftRequest,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        print('[shifts/move] group_id', group_id)
        return move_shift_service(req, current_user, db, group_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"근무코드 순서 변경 실패: {str(e)}")


# [Shifts] - 엑셀 일괄 업로드
@router.get("/shifts/template-download")
async def download_shift_template(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
):
    """근무코드 엑셀 업로드 템플릿 다운로드"""
    try:
        template_path = create_shift_template()
        return FileResponse(
            path=template_path,
            filename="근무코드_업로드_템플릿.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"템플릿 생성 실패: {str(e)}")


@router.post("/shifts/upload-validate")
async def shift_upload_validate_endpoint(
    file: UploadFile = File(...),
    group_id: str = Query(..., description="병동 그룹 ID (필수)"),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """근무코드 엑셀 업로드 - 검증"""
    try:
        if not current_user or not (caller_is_head_nurse(db, current_user) or getattr(current_user, "is_master_admin", False)):
            raise HTTPException(status_code=403, detail="수간호사 또는 마스터 관리자만 접근 가능합니다.")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name
        try:
            result = shift_upload_validate(tmp_file_path, current_user, db, group_id=group_id)
            return result
        finally:
            os.unlink(tmp_file_path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"검증 실패: {str(e)}")


@router.post("/shifts/upload-confirm")
async def shift_upload_confirm_endpoint(
    payload: ShiftUploadConfirmRequest,
    group_id: str = Query(..., description="대상 병동 group_id (필수)"),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """근무코드 엑셀 업로드 - 확정 저장"""
    try:
        if not current_user or not (caller_is_head_nurse(db, current_user) or getattr(current_user, "is_master_admin", False)):
            raise HTTPException(status_code=403, detail="수간호사 또는 마스터 관리자만 접근 가능합니다.")

        target_group_id = group_id
        if not target_group_id:
            target_group_id = getattr(current_user, "group_id", None)
        if not target_group_id:
            raise HTTPException(status_code=400, detail="group_id가 필요합니다. URL에 ?group_id=... 를 포함해주세요.")

        result = shift_upload_confirm(payload.rows, current_user, db, target_group_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"저장 실패: {str(e)}")


# [Shifts] - 타 병동 근무코드 가져오기
@router.get("/shifts/available-imports")
async def get_available_shift_imports(
    group_id: str = Query(..., description="현재 선택된 병동 그룹 ID"),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """현재 그룹에 없는 동일 오피스 내 다른 병동 근무코드 목록 조회"""
    try:
        if not current_user or not (caller_is_head_nurse(db, current_user) or getattr(current_user, "is_master_admin", False)):
            raise HTTPException(status_code=403, detail="수간호사 또는 마스터 관리자만 접근 가능합니다.")

        office_id = getattr(current_user, "office_id", None)
        if not office_id:
            raise HTTPException(status_code=400, detail="office_id를 확인할 수 없습니다.")

        result = get_available_shifts_for_import(office_id, group_id, db)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"조회 실패: {str(e)}")


@router.post("/shifts/import-to-group")
async def import_shifts_to_group_endpoint(
    payload: ShiftImportRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """선택된 근무코드를 동일 오피스 내 다른 병동에서 현재 그룹으로 가져오기"""
    try:
        if not current_user or not (caller_is_head_nurse(db, current_user) or getattr(current_user, "is_master_admin", False)):
            raise HTTPException(status_code=403, detail="수간호사 또는 마스터 관리자만 접근 가능합니다.")

        office_id = getattr(current_user, "office_id", None)
        if not office_id:
            raise HTTPException(status_code=400, detail="office_id를 확인할 수 없습니다.")

        result = import_shifts_to_group(payload.shift_ids, payload.group_id, office_id, db)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"가져오기 실패: {str(e)}")


# [Shift Management] - 시프트 관리 데이터 조회
@router.get("/shift-manage/{class_name}")
async def get_shift_manage(
    class_name: Optional[str] = None,
    # config_version: Optional[str] = None,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    시프트 관리 데이터 조회 엔드포인트.

    - class_name이 없거나 'null'/'undefined'인 경우에도 동작하도록 처리합니다.
    - ADM 등 간호사 레코드가 없는 사용자에 대해서는 토큰의 office_id/group_id를 우선 사용합니다.

    매개변수
    - class_name: 조회할 간호사 클래스명. 예: 'RN'. None/'null'인 경우 전체 조회.
    - group_id: 특정 병동으로 오버라이드할 때 사용.

    반환
    - 슬롯 목록: [{ shift_slot, main_code, codes, manpower }]
    """
    try:
        if not current_user:
            print('[/shift-manage/{class_name}]: 유저 없음')
            raise HTTPException(status_code=401, detail="Not authenticated")
    except Exception as e:
        print('[/shift-manage/{class_name}]:', e)
        raise HTTPException(status_code=401, detail="Not authenticated")
    # 대상 그룹/오피스: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석.
    # (ADM=office 내 임의 / HN=관리 그룹 / 일반=home, 미지정 시 home)
    target_group_id = resolve_effective_group(
        db, current_user, group_id, require_group=False
    )
    office_id = current_user.office_id

    # class_name 정규화: '', 'null', 'undefined'를 미지정으로 간주
    raw_class = class_name.strip().lower() if isinstance(class_name, str) else None
    has_class_filter = bool(raw_class) and raw_class not in ("null", "undefined")

    # 조회 쿼리 구성
    query = db.query(ShiftManage).filter(
        ShiftManage.office_id == office_id,
        ShiftManage.group_id == target_group_id,
    )
    if has_class_filter:
        query = query.filter(ShiftManage.nurse_class == class_name)

    shift_manages = query.order_by(ShiftManage.shift_slot.asc()).all()

    # 데이터가 없을 때 기본 슬롯 생성: 클래스가 지정된 경우에만 생성
    if not shift_manages and has_class_filter:
        default_slots = [
            {"shift_slot": 1, "main_code": "D", "codes": [], "manpower": 3},
            {"shift_slot": 2, "main_code": "E", "codes": [], "manpower": 3},
            {"shift_slot": 3, "main_code": "N", "codes": [], "manpower": 2},
            {"shift_slot": 5, "main_code": "M", "codes": [], "manpower": 0},
        ]

        for slot_data in default_slots:
            shift_manage = ShiftManage(
                office_id=office_id,
                group_id=target_group_id,
                nurse_class=class_name,
                shift_slot=slot_data["shift_slot"],
                main_code=slot_data["main_code"],
                codes=slot_data["codes"],
                manpower=slot_data["manpower"],
            )
            db.add(shift_manage)
        db.commit()

        shift_manages = query.order_by(ShiftManage.shift_slot.asc()).all()
    # for shift in shifts:
    #     shift_manages.append({'shift_slot': 4, 'main_code': 'O', 'codes': [shift.default_shift], 'manpower': 0})
    response = [
        {
            "shift_slot": sm.shift_slot,
            "main_code": sm.main_code,
            "codes": sm.codes if sm.codes else [],
            "manpower": sm.manpower
        }
        for sm in shift_manages
    ]
    # default_shift에 '주'가 포함된 shift를 shifts 테이블에서 찾아서 shift_manages에 추가
    shifts = db.query(Shift).filter(Shift.office_id == current_user.office_id, Shift.default_shift.like('O'), Shift.group_id == target_group_id).all()
    shift_codes = [shift.default_shift for shift in shifts]
    response.append({'shift_slot': 4, 'main_code': 'O', 'codes': shift_codes, 'manpower': ''})
    return response


@router.post("/shift-manage/save")
async def save_shift_manage(
    req: ShiftManageSaveRequest,
    # config_version: Optional[str] = None,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    시프트 관리 슬롯을 저장합니다.

    - 프론트에서 합산해주는 주휴 슬롯(shift_slot=4, main_code='O')은 DB에 저장하지 않고 무시합니다.
    """
    print('current_user', current_user)
    try:
        if not current_user or (not caller_is_head_nurse(db, current_user) and not getattr(current_user, "is_master_admin", False)):
            raise HTTPException(status_code=403, detail="Permission denied")
        
        # 대상 그룹: 토큰 group_id 대신 nurse_id→DB + groups.hn_id 로 해석(ADM 무지정 시 400).
        # HN 도 관리(hn_id) 그룹이면 저장 가능.
        target_group_id = resolve_effective_group(db, current_user, group_id)
        office_id = current_user.office_id
        print(1)
        # 기존 데이터 삭제 (특정 클래스의 모든 슬롯)
        db.query(ShiftManage).filter(
            ShiftManage.office_id == office_id,
            ShiftManage.group_id == target_group_id,
            ShiftManage.nurse_class == req.class_name,
            # ShiftManage.config_version == config_version
        ).delete()
        print(2)
        slots_to_save = [
            slot_data for slot_data in req.slots
            if not _is_weekly_off_slot(slot_data)
        ]
        # 새 데이터 저장
        for slot_data in slots_to_save:
            shift_manage = ShiftManage(
                office_id=office_id,
                group_id=target_group_id,
                nurse_class=req.class_name,
                shift_slot=slot_data["shift_slot"],
                main_code=slot_data.get("main_code"),
                codes=slot_data.get("codes") or [],
                manpower=slot_data.get("manpower") or 0,
                # config_version=config_version
            )
            db.add(shift_manage)
        print(3)
        db.commit()
    except HTTPException:
        raise  # 403/400(권한·그룹) 은 그대로 전파
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"시프트 관리 설정 저장 실패: {str(e)}")
    return {"message": "시프트 관리 설정이 저장되었습니다."}
