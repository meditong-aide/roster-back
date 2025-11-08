import pprint
import uuid
from datetime import date
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.client import get_db
from db.models import RosterConfig as RosterConfigModel
from db.models import Schedule, ShiftPreference, Nurse, ScheduleEntry, Shift, IssuedRoster
from db.nurse_config import Nurse as NurseEngine
from db.roster_config import NurseRosterConfig, DEFAULT_CONFIG
from routers.auth import get_current_user_from_cookie
from routers.utils import get_days_in_month
from schemas.auth_schema import User
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import RosterConfigCreate, PublishRequest
from services.roster_service import save_roster_config_service, get_latest_schedule_service, \
    get_issued_schedules_service, get_schedule_status_service
from services.roster_system import RosterSystem

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime
from db.roster_config import NurseRosterConfig, DEFAULT_CONFIG
from schemas.roster_schema import RosterConfigCreate, RosterConfig, PublishRequest, WantedInvokeRequest, WantedInvokeResponse, RosterRequest
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User
from db.client2 import get_db
from db.models import RosterConfig as RosterConfigModel
from schemas.auth_schema import User as UserSchema
from db.models import Schedule, ShiftPreference, Nurse, ScheduleEntry, Shift, Group, RosterConfig, Wanted, IssuedRoster, ShiftManage, DailyShift
from sqlalchemy import func, and_
from routers.utils import get_days_in_month
from db.nurse_config import Nurse as NurseEngine
from services.roster_system import RosterSystem
from datetime import date
from services.roster_service import save_roster_config_service, get_latest_schedule_service, get_issued_schedules_service, get_schedule_status_service
import uuid
import pprint
router = APIRouter(
    prefix="/roster",
    tags=["roster"]
)
templates = Jinja2Templates(directory="app/templates")

@router.post("/config/save")
async def save_roster_config(
    config_data: RosterConfigCreate,
    group_id: Optional[str] = None,
    user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """근무표 설정 저장 (HN/ADM).

    - HN: 본인 그룹에 저장
    - ADM: `group_id` 쿼리 파라미터로 대상 그룹 지정 필수
    """
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    is_admin = bool(getattr(user, 'is_master_admin', False))

    # 권한/대상 그룹 결정
    override_gid: Optional[str] = None
    if user.is_head_nurse and user.group_id:
        override_gid = None  # HN은 본인 그룹 저장
    else:
        if not is_admin:
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(user, 'office_id', None) and user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        override_gid = g.group_id

    try:
        return save_roster_config_service(config_data, user, db, override_group_id=override_gid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Configuration save failed: {str(e)}")

@router.get("/config/versions")
async def get_config_versions(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """현재 대상 그룹의 설정 버전 목록 조회 (HN/ADM)."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹/오피스 결정
    if current_user.is_head_nurse and current_user.group_id:
        office_id = current_user.office_id
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        office_id = g.office_id
        target_group_id = g.group_id
    
    try:
        versions = db.query(
            RosterConfigModel.config_id,
            func.max(RosterConfigModel.created_at).label('latest_created_at')
        ).filter(
            RosterConfigModel.office_id == office_id,
            RosterConfigModel.group_id == target_group_id,
            RosterConfigModel.config_id.isnot(None)
        ).group_by(RosterConfigModel.config_id).order_by(
            func.max(RosterConfigModel.created_at).desc()
        ).all()
        return [{"config_id": v.config_id} for v in versions]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get config versions: {str(e)}")

@router.get("/config/version/{config_version}")
async def get_config_by_version(
    config_version: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """Get the latest config for a specific version"""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    is_admin = bool(getattr(current_user, 'is_master_admin', False))

    # Resolve target office/group based on role and optional query param
    target_office_id: Optional[str] = None
    target_group_id: Optional[str] = None

    if current_user.is_head_nurse and current_user.group_id:
        target_office_id = current_user.office_id
        target_group_id = current_user.group_id
    else:
        if not is_admin:
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        # Optional safety: ensure admin belongs to same office if present
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id
        target_office_id = g.office_id
    # load latest config for target
    config = db.query(RosterConfigModel).filter(
        RosterConfigModel.office_id == target_office_id,
        RosterConfigModel.group_id == target_group_id
    ).order_by(RosterConfigModel.created_at.desc()).first()
    try:
        if not config:
            # Use resolved target identifiers (HN: own group, ADM: provided group_id)
            office_id = target_office_id
            gid = target_group_id
            if office_id is None or gid is None:
                raise HTTPException(status_code=400, detail="group_id (and office_id) required")
            cfg = DEFAULT_CONFIG
            # DEFAULT 설정으로 RosterConfig 레코드 생성 및 저장
            new_config = RosterConfigModel(
                # config_version="default",
                office_id=office_id,
                group_id=gid,
                # day_req=cfg.daily_shift_requirements.get('D', 3),
                # eve_req=cfg.daily_shift_requirements.get('E', 3),
                # nig_req=cfg.daily_shift_requirements.get('N', 2),
                min_exp_per_shift=cfg.min_experience_per_shift,
                req_exp_nurses=cfg.required_experienced_nurses,
                two_offs_per_week=getattr(cfg, 'enforce_two_offs_per_week', False),
                max_nig_per_month=cfg.max_night_shifts_per_month,
                three_seq_nig=getattr(cfg, 'max_consecutive_nights', 3) >= 3,
                two_offs_after_three_nig=getattr(cfg, 'two_offs_after_three_nig', False),
                two_offs_after_two_nig=getattr(cfg, 'two_offs_after_two_nig', False),
                banned_day_after_eve=getattr(cfg, 'banned_day_after_eve', True),
                max_conseq_work=getattr(cfg, 'max_consecutive_work_days', 6),
                off_days=cfg.calculate_total_off_days(0),
                shift_priority=getattr(cfg, 'shift_requirement_priority', 0.8),
                weekend_shift_ratio=getattr(cfg, 'weekend_shift_ratio', 1.0),
                patient_amount=getattr(cfg, 'patient_amount', 0),
                sequential_offs=getattr(cfg, 'sequential_offs', True),
                even_nights=getattr(cfg, 'even_nights', True),
                nod_noe=False,
                preceptor_gauge=getattr(cfg, 'preceptor_gauge', 5),
            )
            db.add(new_config)

            db.commit()

            db.refresh(new_config)

            return {
                "config_id": new_config.config_id,
                # "config_version": new_config.config_version,
                # "day_req": new_config.day_req,
                # "eve_req": new_config.eve_req,
                # "nig_req": new_config.nig_req,
                "min_exp_per_shift": new_config.min_exp_per_shift,
                "req_exp_nurses": new_config.req_exp_nurses,
                "two_offs_per_week": new_config.two_offs_per_week,
                "max_nig_per_month": new_config.max_nig_per_month,
                "three_seq_nig": new_config.three_seq_nig,
                "two_offs_after_three_nig": new_config.two_offs_after_three_nig,
                "two_offs_after_two_nig": new_config.two_offs_after_two_nig,
                "banned_day_after_eve": new_config.banned_day_after_eve,
                "max_conseq_work": new_config.max_conseq_work,
                "off_days": new_config.off_days,
                "shift_priority": new_config.shift_priority,
                "weekend_shift_ratio": new_config.weekend_shift_ratio,
                "patient_amount": new_config.patient_amount,
                "sequential_offs": new_config.sequential_offs,
                "even_nights": new_config.even_nights,
                "created_at": new_config.created_at.isoformat() if new_config.created_at else None,
                "nod_noe": new_config.nod_noe,
                "preceptor_gauge" : new_config.preceptor_gauge,
            }
        else:
            config = db.query(RosterConfigModel).filter(
                RosterConfigModel.office_id == target_office_id,
                RosterConfigModel.group_id == target_group_id,
            ).order_by(RosterConfigModel.created_at.desc()).first()
            if not config:
                raise HTTPException(status_code=404, detail="Config not found")
            pprint.pprint(config)
            return {
                "config_id": config.config_id,
                # "config_version": config.config_version,
                # "day_req": config.day_req,
                # "eve_req": config.eve_req,
                # "nig_req": config.nig_req,
                "min_exp_per_shift": config.min_exp_per_shift,
                "req_exp_nurses": config.req_exp_nurses,
                "two_offs_per_week": config.two_offs_per_week,
                "max_nig_per_month": config.max_nig_per_month,
                "three_seq_nig": config.three_seq_nig,
                "two_offs_after_three_nig": config.two_offs_after_three_nig,
                "two_offs_after_two_nig": config.two_offs_after_two_nig,
                "banned_day_after_eve": config.banned_day_after_eve,
                "max_conseq_work": config.max_conseq_work,
                "off_days": config.off_days,
                "shift_priority": config.shift_priority,
                "weekend_shift_ratio": config.weekend_shift_ratio,
                "patient_amount": config.patient_amount,
                "sequential_offs": config.sequential_offs,
                "even_nights": config.even_nights,
                "created_at": config.created_at.isoformat() if config.created_at else None,
                "nod_noe": config.nod_noe,
                "preceptor_gauge" : config.preceptor_gauge,
            }
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"Failed to get config: {str(e)}")
    

# [Schedules] - 최신 월과 버전의 스케줄 정보 조회 (수간호사용)
@router.get("/latest")
async def get_latest_schedule(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        override_gid: Optional[str] = None
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if current_user.is_head_nurse and current_user.group_id:
            override_gid = None
        else:
            print('group_id', group_id)
            if not getattr(current_user, 'is_master_admin', False):
                raise HTTPException(status_code=403, detail="Permission denied")
            if not group_id:
                print('!!!!!!!!!!!!!!!!!!!!group_id is required for admin')
                raise HTTPException(status_code=400, detail="group_id is required for admin")
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
                raise HTTPException(status_code=403, detail="Group does not belong to your office")
            override_gid = g.group_id
        return get_latest_schedule_service(current_user, db, override_group_id=override_gid)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get latest schedule: {str(e)}")


# [Schedules] - 발행된(issued) 모든 스케줄 조회
@router.get("/issued")
async def get_issued_schedules(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        override_gid: Optional[str] = None
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if current_user.is_head_nurse and current_user.group_id:
            override_gid = None
        else:
            if not getattr(current_user, 'is_master_admin', False):
                raise HTTPException(status_code=403, detail="Permission denied")
            if not group_id:
                raise HTTPException(status_code=400, detail="group_id is required for admin")
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
                raise HTTPException(status_code=403, detail="Group does not belong to your office")
            override_gid = g.group_id
        return get_issued_schedules_service(current_user, db, override_group_id=override_gid)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get issued schedules: {str(e)}")


# [Schedules] - 현재 그룹의 특정 월에 대한 스케줄 상태 확인
@router.get("/status")
async def get_schedule_status(
    year: int, month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        override_gid: Optional[str] = None
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if current_user.is_head_nurse and current_user.group_id:
            override_gid = None
        elif getattr(current_user, 'is_master_admin', False):
            if not group_id:
                raise HTTPException(status_code=400, detail="group_id is required for admin")
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
                raise HTTPException(status_code=403, detail="Group does not belong to your office")
            override_gid = g.group_id
        return get_schedule_status_service(year, month, current_user, db, override_group_id=override_gid)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get schedule status: {str(e)}")

# 삭제(드롭) 엔드포인트: schedule.dropped=1로 마킹
@router.delete("/{schedule_id}")
async def drop_schedule(
    schedule_id: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹 결정
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id

    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.dropped == False
    ).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="삭제할 스케줄을 찾을 수 없습니다.")
    schedule.dropped = True
    schedule.updated_at = datetime.now()
    db.add(schedule)
    db.commit()
    return {"message": "스케줄이 삭제(숨김)되었습니다.", "schedule_id": schedule_id}


 # [Roster] - 특정 schedule_id의 근무표 조회
@router.get("/schedule/{schedule_id}")
async def get_roster_by_schedule_id(
    schedule_id: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # 대상 그룹 결정
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id
    
    # Get schedule info
    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.dropped == False
    ).first()
    
    if not schedule:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    
    # Get all nurses in the group
    nurses_in_group = db.query(Nurse.nurse_id, Nurse.name, Nurse.experience).filter(
        Nurse.group_id == target_group_id
    ).order_by(Nurse.experience.desc(), Nurse.nurse_id.asc()).all()

# Get shift manage data
    # Get shift colors
    shifts_db = db.query(Shift).all()
    shift_colors = {s.shift_id: s.color for s in shifts_db}
    shift_id = {s.shift_id: s.shift_id for s in shifts_db}
    # Get schedule entries
    entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id).all()
    # for e in entries:
    roster_data = {
        "year": schedule.year, 
        "month": schedule.month,
        "schedule_id": schedule_id,
        "days_in_month": get_days_in_month(schedule.year, schedule.month),
        "shift_colors": shift_colors,
        "nurses": []
    }
    # Structure data by nurse
    entries_by_nurse = {}
    for entry in entries:
        if entry.nurse_id not in entries_by_nurse:
            entries_by_nurse[entry.nurse_id] = {}
        # entries_by_nurse[entry.nurse_id][entry.work_date.day] = entry.shift_id.shift()
        entries_by_nurse[entry.nurse_id][entry.work_date.day] = entry.shift_id

    violations = []  # 임시로 빈 리스트

    for nurse in nurses_in_group:
        nurse_schedule = [entries_by_nurse.get(nurse.nurse_id, {}).get(d, '-') for d in range(1, roster_data["days_in_month"] + 1)]
        
        counts = {shift: nurse_schedule.count(shift) for shift in shift_colors.keys()}
        
        roster_data["nurses"].append({
            "id": nurse.nurse_id,
            "name": nurse.name,
            "experience": nurse.experience,
            "schedule": nurse_schedule,
            "counts": counts
        })
    roster_data["violations"] = violations
    return roster_data
# [Schedules] - 특정 월의 모든 버전 목록 조회 (수간호사용)
@router.get("/{year:int}/{month:int}/versions")
async def get_schedule_versions(
    year: int, month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        print()
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id

    schedules = db.query(Schedule).filter(
        Schedule.group_id == target_group_id,
        Schedule.year == year,
        Schedule.month == month,
        Schedule.dropped == False
    ).order_by(Schedule.version.desc()).all()
    
    return [{
        "schedule_id": schedule.schedule_id,
        "version": schedule.version,
        "status": schedule.status,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
        "created_by": schedule.created_by,
        "name": schedule.name
    } for schedule in schedules]

# [Roster] - 특정 월의 근무표 조회
@router.get("/{year:int}/{month:int}")
async def get_roster_for_month(
    year: int, month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # 대상 그룹 결정
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        # if not getattr(current_user, 'is_master_admin', False):
        #     raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id
    print('target_group_id1', target_group_id)
    # Get latest issued schedule for the month
    schedule_info = db.query(Schedule).filter(
        Schedule.group_id == target_group_id,
        Schedule.year == year,
        Schedule.month == month,
        Schedule.status == 'issued',
        Schedule.dropped == False
    ).order_by(Schedule.version.desc()).first()

    if not schedule_info:
        raise HTTPException(status_code=404, detail="No issued roster found for this month.")

    # Get all nurses in the group
    nurses_in_group = db.query(Nurse.nurse_id, Nurse.name, Nurse.experience).filter(
        Nurse.group_id == target_group_id
    ).order_by(Nurse.experience.desc(), Nurse.nurse_id.asc()).all()

    # Get shift colors
    shifts_db = db.query(Shift).filter(Shift.group_id == target_group_id).all()
    shift_colors = {s.shift_id: s.color for s in shifts_db}
    
    # Get schedule entries
    entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_info.schedule_id).all()
    
    roster_data = {
        "year": year, "month": month,
        "days_in_month": get_days_in_month(year, month),
        "shift_colors": shift_colors,
        "nurses": []
    }
    
    # Structure data by nurse
    entries_by_nurse = {}
    for entry in entries:
        if entry.nurse_id not in entries_by_nurse:
            entries_by_nurse[entry.nurse_id] = {}
        entries_by_nurse[entry.nurse_id][entry.work_date.day] = entry.shift_id

    # 저장된 위반사항 사용 (RosterSystem 생성하지 않음)
    violations = []  # 임시로 빈 리스트 반환 - DB 스키마 업데이트 후 위반사항 기능 복구 예정

    for nurse in nurses_in_group:
        nurse_schedule = [entries_by_nurse.get(nurse.nurse_id, {}).get(d, '-') for d in range(1, roster_data["days_in_month"] + 1)]
        
        counts = {shift: nurse_schedule.count(shift) for shift in shift_colors.keys()}
        
        roster_data["nurses"].append({
            "id": nurse.nurse_id,
            "name": nurse.name,
            "experience": nurse.experience,
            "schedule": nurse_schedule,
            "counts": counts
        })
    roster_data["violations"] = violations
        
    return roster_data

# [Roster] - 근무표 발행
@router.post("/publish")
async def publish_roster(
    req: PublishRequest,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹/오피스 결정
    if current_user.is_head_nurse and current_user.group_id:
        nurse = db.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
        if not nurse:
            raise HTTPException(status_code=404, detail="간호사 정보를 찾을 수 없습니다.")
        office_id = nurse.group.office_id
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        office_id = g.office_id
        target_group_id = g.group_id

    # Get schedule to publish
    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == req.schedule_id,
        Schedule.group_id == target_group_id
    ).first()
    
    if not schedule:
        raise HTTPException(status_code=404, detail="해당 스케줄을 찾을 수 없습니다.")

    # Check if this is the first publication
    existing_issued = db.query(IssuedRoster).filter(
        IssuedRoster.group_id == target_group_id,
        IssuedRoster.office_id == office_id
    ).first()
    
    is_first_issue = not existing_issued
    
    # Get next sequence number
    max_seq = db.query(func.max(IssuedRoster.seq_no)).filter(
        IssuedRoster.group_id == target_group_id,
        IssuedRoster.office_id == office_id
    ).scalar() or 0
    
    # Set all other schedules in this month to draft
    db.query(Schedule).filter(
        Schedule.group_id == target_group_id,
        Schedule.year == schedule.year,
        Schedule.month == schedule.month,
        Schedule.status == 'issued'
    ).update({"status": "draft"})
    
    # Update current schedule to issued
    schedule.status = 'issued'
    
    # Create issued roster record
    issued_roster = IssuedRoster(
        seq_no=max_seq + 1,
        office_id=office_id,
        group_id=target_group_id,
        nurse_id=current_user.nurse_id,
        version=schedule.version,
        v_name=f"v{schedule.version}",  # 기본 버전명
        issue_cmmt=req.issue_comment if not is_first_issue else "첫 발행",
        schedule_id=req.schedule_id
    )
    
    db.add(issued_roster)
    db.commit()
    
    return {
        "message": "근무표가 성공적으로 발행되었습니다.",
        "seq_no": issued_roster.seq_no,
        "is_first_issue": is_first_issue
    } 


# [Roster] - 특정 월의 근무표 조회
@router.get("/{year: int}/{month: int}")
async def get_roster_for_month(
    year: int, month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹 결정
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id
    
    # Get latest issued schedule for the month
    schedule_info = db.query(Schedule).filter(
        Schedule.group_id == target_group_id,
        Schedule.year == year,
        Schedule.month == month,
        Schedule.status == 'issued'
    ).order_by(Schedule.version.desc()).first()

    if not schedule_info:
        raise HTTPException(status_code=404, detail="No issued roster found for this month.")

    # Get all nurses in the group
    nurses_in_group = db.query(Nurse.nurse_id, Nurse.name, Nurse.experience).filter(
        Nurse.group_id == target_group_id
    ).order_by(Nurse.experience.desc(), Nurse.nurse_id.asc()).all()

    # Get shift colors
    shifts_db = db.query(Shift).all()
    shift_colors = {s.shift_id: s.color for s in shifts_db}
    
    # Get schedule entries
    entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_info.schedule_id).all()
    
    roster_data = {
        "year": year, "month": month,
        "days_in_month": get_days_in_month(year, month),
        "shift_colors": shift_colors,
        "nurses": []
    }
    
    # Structure data by nurse
    entries_by_nurse = {}
    for entry in entries:
        if entry.nurse_id not in entries_by_nurse:
            entries_by_nurse[entry.nurse_id] = {}
        entries_by_nurse[entry.nurse_id][entry.work_date.day] = entry.shift_id

    # 저장된 위반사항 사용 (RosterSystem 생성하지 않음)
    violations = []  # 임시로 빈 리스트 반환 - DB 스키마 업데이트 후 위반사항 기능 복구 예정

    for nurse in nurses_in_group:
        nurse_schedule = [entries_by_nurse.get(nurse.nurse_id, {}).get(d, '-') for d in range(1, roster_data["days_in_month"] + 1)]
        
        counts = {shift: nurse_schedule.count(shift) for shift in shift_colors.keys()}
        
        roster_data["nurses"].append({
            "id": nurse.nurse_id,
            "name": nurse.name,
            "experience": nurse.experience,
            "schedule": nurse_schedule,
            "counts": counts
        })
    roster_data["violations"] = violations
        
    return roster_data

# [Roster] - 근무표 저장
@router.post("/save")
async def save_roster(
    roster_data: dict,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    year = roster_data.get('year')
    month = roster_data.get('month')
    schedule_id = roster_data.get('schedule_id')
    roster = roster_data.get('roster')
    
    if not all([year, month, schedule_id, roster]):
        raise HTTPException(status_code=400, detail="Missing required fields: year, month, schedule_id, roster")

    # Get the latest schedule for the month
    # 대상 그룹 결정
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id

    schedule = db.query(Schedule).filter(
        Schedule.group_id == target_group_id,
        Schedule.year == year,
        Schedule.month == month,
        Schedule.schedule_id == schedule_id
    ).order_by(Schedule.schedule_id.desc()).first()
    
    if not schedule:
        raise HTTPException(status_code=404, detail="No schedule found for this month")

    # Clear existing roster entries
    db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule.schedule_id).delete()
    
    # Save new roster entries
    
    for nurse in roster:
        nurse_id = nurse.get('nurse_id') or nurse.get('id')  # 둘 다 체크
        if not nurse_id:
            continue  # nurse_id가 없으면 건너뛰기
            
        schedule_data = nurse.get('schedule', [])
        for day_index, shift_id in enumerate(schedule_data):
            if shift_id and shift_id.strip():  # 빈 값이 아닌 경우만
                work_date = date(year, month, day_index + 1)
                entry = ScheduleEntry(
                    entry_id=str(uuid.uuid4().hex)[:16],
                    schedule_id=schedule.schedule_id,
                    nurse_id=nurse_id,
                    work_date=work_date,
                    shift_id=shift_id.upper()
                )
                db.add(entry)

    db.commit()
    return {"message": "Roster saved successfully"}



# [Schedules] - 특정 스케줄의 모든 간호사 원티드 제출 현황 확인
@router.get("/{year:int}/{month:int}/submissions")
async def get_submission_statuses(
    year: int, month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹 결정 (HN: 본인 그룹, ADM: 쿼리로 지정)
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, 'is_master_admin', False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
            raise HTTPException(status_code=403, detail="Group does not belong to your office")
        target_group_id = g.group_id
    # 대상 그룹의 간호사 수집
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

# [Schedules] - 현재 그룹의 특정 월에 대한 스케줄 상태 확인
@router.get("/status")
async def get_schedule_status(
    year: int, month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # 수간호사/관리자: 그룹 요약 조회
    if getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False):
        if current_user.is_head_nurse and current_user.group_id:
            target_group_id = current_user.group_id
        else:
            if not group_id:
                raise HTTPException(status_code=400, detail="group_id is required for admin")
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
                raise HTTPException(status_code=403, detail="Group does not belong to your office")
            target_group_id = g.group_id
        schedules = db.query(Schedule).filter(
            Schedule.group_id == target_group_id,
            Schedule.year == year,
            Schedule.month == month
        ).all()
        has_schedules = len(schedules) > 0
        latest_status = schedules[0].status if schedules else None
        return {
            "has_schedules": has_schedules,
            "latest_status": latest_status,
            "schedule_count": len(schedules)
        }
    
    # 일반 간호사인 경우 - 최신 선호도 데이터 조회
    schedule = db.query(Schedule).filter(
        Schedule.group_id == current_user.group_id,
        Schedule.year == year,
        Schedule.month == month
    ).order_by(Schedule.version.desc()).first()

    # 최신 제출된 선호도 먼저 확인
    submitted_preference = db.query(ShiftPreference).filter(
        ShiftPreference.nurse_id == current_user.nurse_id,
        ShiftPreference.year == year,
        ShiftPreference.month == month,
        ShiftPreference.is_submitted == True
    ).order_by(ShiftPreference.submitted_at.desc()).first()
    
    if submitted_preference:
        return {
            "schedule_status": schedule.status if schedule else None,
            "preference_is_submitted": True,
            "preference_data": submitted_preference.data,
            "has_schedules": schedule is not None,
            "created_at": submitted_preference.created_at,
            "submitted_at": submitted_preference.submitted_at
        }
    
    # 제출된 것이 없으면 최신 draft 확인
    draft_preference = db.query(ShiftPreference).filter(
        ShiftPreference.nurse_id == current_user.nurse_id,
        ShiftPreference.year == year,
        ShiftPreference.month == month,
        ShiftPreference.is_submitted == False
    ).order_by(ShiftPreference.created_at.desc()).first()
    
    if draft_preference:
        return {
            "schedule_status": schedule.status if schedule else None,
            "preference_is_submitted": False,
            "preference_data": draft_preference.data,
            "has_schedules": schedule is not None,
            "created_at": draft_preference.created_at,
            "submitted_at": None
        }
    
    # 아무 선호도도 없는 경우
    return {
        "schedule_status": schedule.status if schedule else None,
        "preference_is_submitted": False,
        "preference_data": None,
        "has_schedules": schedule is not None,
        "created_at": None,
        "submitted_at": None
    }

@router.post("/validate")
async def validate_roster(
    roster_data: dict,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    # ──────────────────────── 0. 인증/파라미터 체크 ────────────────────────
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")

    year: int   = roster_data.get('year')
    month: int  = roster_data.get('month')
    roster      = roster_data.get('roster')
    schedule_id = roster_data.get('schedule_id')
    config_id = db.query(Schedule).filter(Schedule.schedule_id == schedule_id).first().config_id

    # config_version = db.query(RosterConfigModel).filter(RosterConfigModel.config_id == config_id).first().config_version
    if not all([year, month, roster]):
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: year, month, roster"
        )

    try:
        # ──────────────────────── 1. “base-code ↔️ 파생코드” 매핑 만들기 ────────────────────────
        #
        #  * 같은 nurse_class라도, 사전에 등록된 교대(slot) 기준으로만 조회
        #  * codes 열(JSON) 에 들어있는 파생 코드를 본교대(main_code) 로 매핑
        from db.models import ShiftManage, Nurse, RosterConfig  # local import
        # 대상 그룹/오피스 결정
        if current_user.is_head_nurse and current_user.group_id:
            office_id = current_user.office_id
            target_group_id = current_user.group_id
        else:
            if not group_id:
                raise HTTPException(status_code=400, detail="group_id is required for admin")
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
                raise HTTPException(status_code=403, detail="Group does not belong to your office")
            office_id = g.office_id
            target_group_id = g.group_id

        shift_rows = db.query(ShiftManage).filter(
            ShiftManage.office_id == office_id,
            ShiftManage.group_id  == target_group_id,
            ShiftManage.nurse_class == 'RN',
        ).order_by(ShiftManage.shift_slot.asc()).all()
        #    예) { 'D': 'D', 'D1': 'D', 'MD': 'D',  'E': 'E', … }
        alias_map: dict[str, str] = {}
        daily_shift_requirements = {}
        for row in shift_rows:
            if not row.main_code:
                continue
            base = row.main_code.upper()          # ex) 'D'
            alias_map[base] = base
            # daily_shift_requirements[row.main_code.strip()] = row.manpower
            daily_shift_requirements[row.main_code] = row.manpower
            if row.codes:
                # row.codes 가 JSON 컬럼 → 이미 list 로 deserialize 되어있음
                for code in row.codes:
                    alias_map[code.upper()] = base
        # OFF(휴무) 도 항상 포함시킴
        alias_map.setdefault('OFF', 'O')
        alias_map.setdefault('O',   'O')
        # ──────────────────────── 2. 근무표 설정(인원/제약) 불러오기 ────────────────────────
        latest_config_db = db.query(RosterConfigModel).filter(
            RosterConfigModel.office_id == office_id,
            RosterConfigModel.group_id == target_group_id,
        ).order_by(RosterConfigModel.created_at.desc()).first()
        if not latest_config_db:
            return {"violations": ["근무표 설정을 찾을 수 없습니다."]}

        roster_config_for_engine = NurseRosterConfig(
            daily_shift_requirements=daily_shift_requirements,
            max_consecutive_work_days   = latest_config_db.max_conseq_work,
            max_night_shifts_per_month  = latest_config_db.max_nig_per_month,
            max_consecutive_nights      = 3 if latest_config_db.three_seq_nig else 2
        )

        days_in_month = get_days_in_month(year, month)
        try:
            rows = (
                db.query(DailyShift)
                .filter(
                    DailyShift.office_id == office_id,
                    DailyShift.group_id == target_group_id,
                    DailyShift.year == year,
                    DailyShift.month == month,
                )
                .order_by(DailyShift.day.asc())
                .all()
            )
        except Exception as e:
            print(f"error: {e}")
        # day→counts 맵 구성 후 리스트로 변환(0-index)
        by_day = {r.day: {'D': int(r.d_count or 0), 'E': int(r.e_count or 0), 'N': int(r.n_count or 0)} for r in rows}
        daily_shift_requirements_by_day = [by_day.get(d, {'D': daily_shift_requirements.get('D', 0), 'E': daily_shift_requirements.get('E', 0), 'N': daily_shift_requirements.get('N', 0)}) for d in range(1, days_in_month + 1)]

        # ──────────────────────── 3. RosterSystem 초기화 ────────────────────────
        nurses_for_engine = [
            NurseEngine.from_db_model(n, i)
            for i, n in enumerate(
                db.query(Nurse).filter(Nurse.group_id == target_group_id).all()
            )
        ]
        # 일자별 요구 인원은 config 객체에 속성으로 주입하여 사용
        try:
            setattr(roster_config_for_engine, 'daily_shift_requirements_by_day', daily_shift_requirements_by_day)
        except Exception as e:
            print(f"error: {e}")
            pass
        system = RosterSystem(
            nurses        = nurses_for_engine,
            target_month  = date(year, month, 1),
            config        = roster_config_for_engine
        )
        # shift_types 는 ['D','E','N','O'] (엔진 기본).  
        shift_map = {s: i for i, s in enumerate(system.config.shift_types)}
        system.roster.fill(0)                                # 3-D 배열 0으로 초기화
        # ──────────────────────── 4. 프론트에서 넘어온 근무표 → 엔진 포맷 변환 ────────────────────────
        for nurse_idx, nurse_data in enumerate(roster):
            if nurse_idx >= len(system.nurses):
                continue
            schedule = nurse_data.get('schedule', [])
            for day_idx, raw_shift in enumerate(schedule):
                if day_idx >= system.num_days:
                    continue
                # ① 대소문자 무시
                raw_shift = (raw_shift or '').upper()

                # ② alias_map 으로 본교대 변환
                base_shift = alias_map.get(raw_shift, raw_shift)

                # ③ 엔진 shift index 찾기
                shift_idx = shift_map.get(base_shift)
                if shift_idx is not None:
                    system.roster[nurse_idx, day_idx, shift_idx] = 1
                # else: 알 수 없는 코드 → 무시
        # ──────────────────────── 5. 위반사항 탐색 & 포매팅 ────────────────────────
        violation_details = system._find_violations()
        # print('violation_details')
        # import pprint
        # pprint.pprint(violation_details)
        violation_messages: set[str] = set()
        detailed_violations: list[dict] = []
        for v in violation_details:
            if v['type'] == 'shift_requirement':
                # print( f"{v['day'] + 1}일: {v['shift']} 근무 인원 미달 ")
                violation_messages.add(
                    f"{v['day'] + 1}일: {v['shift']} 근무 인원 미달 "
                    f"(필요: {v['required']}, 배정: {v['actual']})"
                )
                detailed_violations.append({
                    'type': 'shift_requirement',
                    'day': v['day'],
                    'shift': v['shift'],
                    'required': v['required'],
                    'actual': v['actual']
                })
            elif v['type'] == 'consecutive_work':
                nurse_name = system.nurses[v['nurse_idx']].name
                violation_messages.add(f"{nurse_name}: 최대 연속 근무일 초과")
                detailed_violations.append({
                    'type': 'consecutive_work',
                    'nurse_idx': v['nurse_idx'],
                    'nurse_name': nurse_name,
                    'day': v['day']+1
                })
            elif v['type'] == 'night_consecutive':
                nurse_name = system.nurses[v['nurse_idx']].name
                violation_messages.add(f"{nurse_name}: 야간 연속 근무 위반")
                detailed_violations.append({
                    'type': 'night_consecutive',
                    'nurse_idx': v['nurse_idx'],
                    'nurse_name': nurse_name,
                    'day': v['day']+1
                })
            elif v['type'] == 'night_nd':
                nurse_name = system.nurses[v['nurse_idx']].name
                violation_messages.add(f"{nurse_name}: 야간 근무 후 주간 근무 위반")
                detailed_violations.append({
                    'type': 'night_nd',
                    'nurse_idx': v['nurse_idx'],
                    'nurse_name': nurse_name,
                    'day': v['day']
                })
            elif v['type'] == 'night_month_limit':
                nurse_name = system.nurses[v['nurse_idx']].name
                violation_messages.add(f"{nurse_name}: 월 야간 근무 초과 위반")
                detailed_violations.append({
                    'type': 'night_month_limit',
                    'nurse_idx': v['nurse_idx'],
                    'nurse_name': nurse_name,
                    'day': v['day']
                })
        # print('violation_messages')
        # import pprint
        # pprint.pprint(sorted(violation_messages))
        # pprint.pprint(detailed_violations)
        return {
            "violations": sorted(violation_messages),
            "detailed_violations": detailed_violations
        }

    # ──────────────────────── 6. 예외 처리 ────────────────────────
    except Exception as e:
        print(f"[validate_roster] 오류: {e}")
        return {
            "violations": [f"위반사항 계산 중 오류가 발생했습니다: {str(e)}"],
            "detailed_violations": []
        }

# [Roster] - 스케줄 이름 업데이트
@router.patch("/{schedule_id}/name")
async def update_schedule_name(
    schedule_id: str,
    name_data: dict,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
        raise HTTPException(status_code=403, detail="Permission denied")
    
    try:
        # 스케줄 조회
        if current_user.is_head_nurse and current_user.group_id:
            target_group_id = current_user.group_id
        else:
            if not group_id:
                raise HTTPException(status_code=400, detail="group_id is required for admin")
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
                raise HTTPException(status_code=403, detail="Group does not belong to your office")
            target_group_id = g.group_id

        schedule = db.query(Schedule).filter(
            Schedule.schedule_id == schedule_id,
            Schedule.group_id == target_group_id,
            Schedule.dropped == False
        ).first()
        
        if not schedule:
            raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
        
        # 이름 업데이트
        new_name = name_data.get('name')
        if not new_name or not new_name.strip():
            raise HTTPException(status_code=400, detail="이름을 입력해주세요.")
        
        schedule.name = new_name.strip()
        schedule.updated_at = datetime.now()
        
        db.add(schedule)
        db.commit()
        
        return {"message": "스케줄 이름이 성공적으로 업데이트되었습니다.", "name": new_name}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"이름 업데이트 실패: {str(e)}")

@router.get("/schedule/{schedule_id}/export", response_class=StreamingResponse)
async def export_schedule_excel(
    schedule_id: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
        - 근무표 엑셀 내보내기
        - 파일명: roster_{year}_{month}_v{version}.xlsx
    """
    if not current_user or not current_user.is_head_nurse:
        raise HTTPException(status_code=403, detail="Permission denied")
    print('group_id', group_id)
    # 스케줄 정보 확인(파일명에 사용)
    # # 대상 그룹 확인
    # if current_user.is_head_nurse and current_user.group_id:
    #     target_group_id = current_user.group_id
    # else:
    #     if not getattr(current_user, 'is_master_admin', False):
    #         raise HTTPException(status_code=403, detail="Permission denied")
    #     if not group_id:
    #         raise HTTPException(status_code=400, detail="group_id is required for admin")
    #     g = db.query(Group).filter(Group.group_id == group_id).first()
    #     if not g:
    #         raise HTTPException(status_code=404, detail="Group not found")
    #     if getattr(current_user, 'office_id', None) and current_user.office_id != g.office_id:
    #         raise HTTPException(status_code=403, detail="Group does not belong to your office")
    #     target_group_id = g.group_id

    
    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        # Schedule.group_id == target_group_id,
        Schedule.dropped == False
    ).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    try:
        from services.excel_service import export_schedule_excel_bytes
        if group_id:
            target_group_id = group_id
        else:
            target_group_id = current_user.group_id
        data = export_schedule_excel_bytes(schedule_id, current_user, db, target_group_id)
        filename = f"roster_{schedule.year}_{schedule.month}_v{schedule.version}.xlsx"
        return StreamingResponse(
            iter([data]),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename=\"{filename}\""
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"엑셀 생성 실패: {str(e)}")
