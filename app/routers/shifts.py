from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from db.client2 import get_db
from db.models import Shift, Nurse, ScheduleEntry, ShiftManage, RosterConfig, Group
from schemas.auth_schema import User as UserSchema
from routers.auth import get_current_user_from_cookie
from schemas.roster_schema import ShiftAddRequest, RemoveShiftRequest, MoveShiftRequest, ShiftManageSaveRequest, ShiftUpdateRequest
from services.shift_service import (
    get_shifts_service as get_shifts_service_mysql,
    add_shift_service,
    update_shift_service,
    remove_shift_service,
    move_shift_service
)
from typing import Optional
import os
from services.shift_service_mssql import get_shifts_service as get_shifts_service_mssql
from datetime import timedelta, datetime

def convert_time(value):
    if isinstance(value, str):  # '06:00' → timedelta
        h, m = map(int, value.split(":"))
        return timedelta(hours=h, minutes=m)
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
            if not current_user or not getattr(current_user, 'is_master_admin', False):
                raise HTTPException(status_code=403, detail="마스터 관리자만 다른 병동 조회가 가능합니다.")
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g or g.office_id != current_user.office_id:
                raise HTTPException(status_code=403, detail="해당 병동에 접근할 수 없습니다.")
            override_gid = group_id
        backend = 'mssql'
        if backend == "mssql":
            print('여기')
            shifts = get_shifts_service_mssql(current_user, db, override_gid)
        else:
            shifts = get_shifts_service_mysql(current_user, db, override_gid)
        for shift in shifts:
            shift["start_time"] = convert_time(shift["start_time"])
            shift["end_time"] = convert_time(shift["end_time"])
        return shifts
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=f"시프트 정보 조회 실패: {str(e)}")

def _format_time_display(shift):
    """근무 시간 정보를 표시용으로 포맷팅"""
    if 'allday' in shift:
        if shift.allday == 1:
            return '종일'
    elif shift.start_time and shift.end_time:
        return f'{shift.start_time} ~ {shift.end_time}'
    elif shift.type:
        return shift.type

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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"근무코드 순서 변경 실패: {str(e)}")


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
    print('진입')
    try:
        if not current_user:
            print('[/shift-manage/{class_name}]: 유저 없음')
            raise HTTPException(status_code=401, detail="Not authenticated")
    except Exception as e:
        print('[/shift-manage/{class_name}]:', e)
        raise HTTPException(status_code=401, detail="Not authenticated")
    # 대상 그룹/오피스 결정
    if group_id:
        if not current_user or not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="마스터 관리자만 다른 병동 조회가 가능합니다.")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g or g.office_id != current_user.office_id:
            raise HTTPException(status_code=403, detail="해당 병동에 접근할 수 없습니다.")
        office_id = g.office_id
        target_group_id = g.group_id
    else:
        # 토큰 정보 우선 활용(ADM과 같이 Nurse 레코드가 없는 경우 대비)
        if getattr(current_user, 'office_id', None) and getattr(current_user, 'group_id', None):
            office_id = current_user.office_id
            target_group_id = current_user.group_id
        else:
            nurse = db.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
            if not nurse or not nurse.group:
                raise HTTPException(status_code=404, detail="User group information not found")
            office_id = nurse.group.office_id
            target_group_id = current_user.group_id

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
            {"shift_slot": 3, "main_code": "N", "codes": [], "manpower": 2}
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

    return [
        {
            "shift_slot": sm.shift_slot,
            "main_code": sm.main_code,
            "codes": sm.codes if sm.codes else [],
            "manpower": sm.manpower
        }
        for sm in shift_manages
    ]


@router.post("/shift-manage/save")
async def save_shift_manage(
    req: ShiftManageSaveRequest,
    # config_version: Optional[str] = None,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    print('current_user', current_user)
    if not current_user and not current_user.is_head_nurse and not current_user.is_master_admin:
        raise HTTPException(status_code=403, detail="Permission denied")
    
    # Get current user's office_id
    if group_id:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="마스터 관리자만 다른 병동 저장이 가능합니다.")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g or g.office_id != current_user.office_id:
            raise HTTPException(status_code=403, detail="해당 병동에 접근할 수 없습니다.")
        office_id = g.office_id
        target_group_id = g.group_id
    else:
        nurse = db.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
        if not nurse or not nurse.group:
            raise HTTPException(status_code=404, detail="User group information not found")
        office_id = nurse.group.office_id
        target_group_id = current_user.group_id
    
    # 기존 데이터 삭제 (특정 클래스의 모든 슬롯)
    db.query(ShiftManage).filter(
        ShiftManage.office_id == office_id,
        ShiftManage.group_id == target_group_id,
        ShiftManage.nurse_class == req.class_name,
        # ShiftManage.config_version == config_version
    ).delete()
    
    # 새 데이터 저장
    for slot_data in req.slots:
        shift_manage = ShiftManage(
            office_id=office_id,
            group_id=target_group_id,
            nurse_class=req.class_name,
            shift_slot=slot_data["shift_slot"],
            main_code=slot_data.get("main_code"),
            codes=slot_data["codes"],
            manpower=slot_data["manpower"],
            # config_version=config_version
        )
        db.add(shift_manage)
    
    db.commit()
    return {"message": "시프트 관리 설정이 저장되었습니다."}