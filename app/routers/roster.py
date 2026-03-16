import pprint
import uuid
from datetime import date
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, Body, Query
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

# from db.client import get_db
from db.models import RosterConfig as RosterConfigModel
from db.models import (
    Office,
    Schedule,
    ShiftPreference,
    Nurse,
    ScheduleEntry,
    Shift,
    IssuedRoster,
    Group,
    WantedRequest,
)
from db.nurse_config import Nurse as NurseEngine
from db.roster_config import NurseRosterConfig, DEFAULT_CONFIG
from routers.auth import get_current_user_from_cookie
from routers.utils import get_days_in_month
from schemas.auth_schema import User
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import RosterConfigCreate, PublishRequest
from services.roster_service import (
    save_roster_config_service,
    get_latest_schedule_service,
    get_issued_schedules_service,
    get_schedule_status_service,
)
from services.roster_system import RosterSystem

from fastapi import APIRouter, HTTPException, Request, Depends, UploadFile, File, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
    FileResponse,
)
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime
from db.roster_config import NurseRosterConfig, DEFAULT_CONFIG
from schemas.roster_schema import (
    RosterConfigCreate,
    RosterConfig,
    PublishRequest,
    WantedInvokeRequest,
    WantedInvokeResponse,
    RosterRequest,
    ScheduleShareCreateRequest,
    ScheduleShareAutoCreateRequest,
    ScheduleShareCaptureCreateRequest,
)
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User
from db.client2 import get_db
from db.models import RosterConfig as RosterConfigModel
from schemas.auth_schema import User as UserSchema
from db.models import (
    Schedule,
    ShiftPreference,
    Nurse,
    ScheduleEntry,
    Shift,
    Group,
    RosterConfig,
    Wanted,
    IssuedRoster,
    IssuedRosterSnapshot,
    ShiftManage,
    DailyShift,
)
from sqlalchemy import func, and_
from routers.utils import get_days_in_month
from db.nurse_config import Nurse as NurseEngine
from services.roster_system import RosterSystem
from datetime import date
from services.roster_service import (
    save_roster_config_service,
    get_latest_schedule_service,
    get_issued_schedules_service,
    get_schedule_status_service,
    create_issued_roster_snapshot,
    get_issued_roster_snapshot_service,
)
from services.weekly_off_service import get_nurses_weekly_off_service
from utils.utils import send_roster_publish_push, send_roster_republish_push
import uuid
import pprint

router = APIRouter(prefix="/roster", tags=["roster"])
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

    is_admin = bool(getattr(user, "is_master_admin", False))

    # 권한/대상 그룹 결정
    override_gid: Optional[str] = None
    if user.is_head_nurse and user.group_id:
        override_gid = None  # HN은 본인 그룹 저장
    else:
        if not is_admin:
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(
                status_code=400, detail="group_id is required for admin"
            )
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if getattr(user, "office_id", None) and user.office_id != g.office_id:
            raise HTTPException(
                status_code=403, detail="Group does not belong to your office"
            )
        override_gid = g.group_id

    try:
        return save_roster_config_service(
            config_data, user, db, override_group_id=override_gid
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Configuration save failed: {str(e)}"
        )


@router.get("/config/versions")
async def get_config_versions(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """현재 대상 그룹의 설정 버전 목록 조회 (HN/ADM)."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if current_user.is_master_admin:
        target_group_id = group_id
        target_office_id = current_user.office_id
    else:
        target_group_id = current_user.group_id
        target_office_id = current_user.office_id

    try:
        versions = (
            db.query(
                RosterConfigModel.config_id,
                func.max(RosterConfigModel.created_at).label("latest_created_at"),
            )
            .filter(
                RosterConfigModel.office_id == target_office_id,
                RosterConfigModel.group_id == target_group_id,
                RosterConfigModel.config_id.isnot(None),
            )
            .group_by(RosterConfigModel.config_id)
            .order_by(func.max(RosterConfigModel.created_at).desc())
            .all()
        )
        return [{"config_id": v.config_id} for v in versions]
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get config versions: {str(e)}"
        )


@router.get("/config/version/{config_version}")
async def get_config_by_version(
    config_version: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """Get the latest config for a specific version"""
    print("[/config/version/{config_version}] group_id", group_id)
    print("[/config/version/{config_version}] current_user", current_user.__dict__)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if current_user.is_master_admin:
        target_group_id = group_id
        target_office_id = current_user.office_id
    else:
        target_group_id = current_user.group_id
        target_office_id = current_user.office_id

    # load latest config for target
    config = (
        db.query(RosterConfigModel)
        .filter(
            RosterConfigModel.office_id == target_office_id,
            RosterConfigModel.group_id == target_group_id,
        )
        .order_by(RosterConfigModel.created_at.desc())
        .first()
    )
    try:
        if not config:
            # Use resolved target identifiers (HN: own group, ADM: provided group_id)

            cfg = DEFAULT_CONFIG
            # DEFAULT 설정으로 RosterConfig 레코드 생성 및 저장
            new_config = RosterConfigModel(
                # config_version="default",
                office_id=target_office_id,
                group_id=target_group_id,
                # day_req=cfg.daily_shift_requirements.get('D', 3),
                # eve_req=cfg.daily_shift_requirements.get('E', 3),
                # nig_req=cfg.daily_shift_requirements.get('N', 2),
                min_exp_per_shift=cfg.min_experience_per_shift,
                req_exp_nurses=cfg.required_experienced_nurses,
                two_offs_per_week=getattr(cfg, "enforce_two_offs_per_week", False),
                max_nig_per_month=cfg.max_night_shifts_per_month,
                three_seq_nig=getattr(cfg, "max_consecutive_nights", 3) >= 3,
                two_offs_after_three_nig=getattr(
                    cfg, "two_offs_after_three_nig", False
                ),
                two_offs_after_two_nig=getattr(cfg, "two_offs_after_two_nig", False),
                banned_day_after_eve=getattr(cfg, "banned_day_after_eve", True),
                max_conseq_work=getattr(cfg, "max_consecutive_work_days", 6),
                off_days=cfg.calculate_total_off_days(0),
                shift_priority=getattr(cfg, "shift_requirement_priority", 0.8),
                weekend_shift_ratio=getattr(cfg, "weekend_shift_ratio", 1.0),
                patient_amount=getattr(cfg, "patient_amount", 0),
                sequential_offs=getattr(cfg, "sequential_offs", True),
                even_nights=getattr(cfg, "even_nights", True),
                nod_noe=False,
                preceptor_gauge=getattr(cfg, "preceptor_gauge", 5),
                preceptee_on=getattr(cfg, "preceptee_on", False),
                preceptee_shift_count=getattr(cfg, "preceptee_shift_count", True),
                weekly_off_group=getattr(cfg, "weekly_off_group", False),
                off_placement_mode=getattr(cfg, "off_placement_mode", 0),
                not_one_night=getattr(cfg, "not_one_night", False),
                use_mid=False,
                fixed_wanted_use_yn=getattr(cfg, "fixed_wanted_use_yn", False),
                show_level=getattr(cfg, "show_level", True),
                show_preceptor=getattr(cfg, "show_preceptor", True),
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
                "created_at": new_config.created_at.isoformat()
                if new_config.created_at
                else None,
                "nod_noe": new_config.nod_noe,
                "preceptor_gauge": new_config.preceptor_gauge,
                "preceptee_on": new_config.preceptee_on,
                "preceptee_shift_count": new_config.preceptee_shift_count,
                "weekly_off_group": new_config.weekly_off_group,
                "off_placement_mode": new_config.off_placement_mode,
                "not_one_night": new_config.not_one_night,
                "use_mid": bool(getattr(new_config, "use_mid", False)),
                "fixed_wanted_use_yn": new_config.fixed_wanted_use_yn,
                "show_level": new_config.show_level,
                "show_preceptor": new_config.show_preceptor,
            }
        else:
            config = (
                db.query(RosterConfigModel)
                .filter(
                    RosterConfigModel.office_id == target_office_id,
                    RosterConfigModel.group_id == target_group_id,
                )
                .order_by(RosterConfigModel.created_at.desc())
                .first()
            )
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
                "created_at": config.created_at.isoformat()
                if config.created_at
                else None,
                "nod_noe": config.nod_noe,
                "preceptor_gauge": config.preceptor_gauge,
                "preceptee_on": config.preceptee_on,
                "preceptee_shift_count": config.preceptee_shift_count,
                "weekly_off_group": config.weekly_off_group,
                "off_placement_mode": config.off_placement_mode,
                "not_one_night": config.not_one_night,
                "use_mid": bool(getattr(config, "use_mid", False)),
                "fixed_wanted_use_yn": config.fixed_wanted_use_yn,
                "show_level": config.show_level,
                "show_preceptor": config.show_preceptor,
            }
    except Exception as e:
        print("error", e)
        raise HTTPException(status_code=500, detail=f"Failed to get config: {str(e)}")


# [Schedules] - 최신 월과 버전의 스케줄 정보 조회 (수간호사용)
@router.get("/latest")
async def get_latest_schedule(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if current_user.is_master_admin:
            target_group_id = group_id
        else:
            target_group_id = current_user.group_id
        return get_latest_schedule_service(
            current_user, db, override_group_id=target_group_id
        )
    except Exception as e:
        print(
            "[DEBUG] [roster.py - get_latest_schedule] current_user",
            current_user.__dict__,
        )
        print("[DEBUG] [roster.py - get_latest_schedule] group_id", group_id)
        print("[DEBUG] [roster.py - get_latest_schedule] error", e)
        raise HTTPException(
            status_code=500, detail=f"Failed to get latest schedule: {str(e)}"
        )


# [Schedules] - 발행된(issued) 모든 스케줄 조회
@router.get("/issued")
async def get_issued_schedules(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    print("[/issued] group_id", group_id)
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if current_user.is_master_admin:
            target_group_id = group_id
        else:
            target_group_id = current_user.group_id
        return get_issued_schedules_service(
            current_user, db, target_group_id=target_group_id
        )
    except Exception as e:
        print("[/issued] error", e)
        raise HTTPException(
            status_code=500, detail=f"Failed to get issued schedules: {str(e)}"
        )


@router.get("/issued_roster")
async def get_issued_roster_snapshot(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    특정 연월의 활성 발행본 근무표 스냅샷을 조회합니다.

    - 수간호사: 자신의 그룹 기준
    - ADM: 쿼리 파라미터 group_id 로 대상 그룹 지정
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        # 대상 그룹 결정
        if current_user.is_master_admin:
            target_group_id = group_id
        else:
            target_group_id = current_user.group_id

        snapshot = get_issued_roster_snapshot_service(
            year=year,
            month=month,
            current_user=current_user,
            db=db,
            target_group_id=target_group_id,
        )
        if not snapshot:
            raise HTTPException(status_code=404, detail="Issued snapshot not found")
        return snapshot
    except HTTPException:
        raise
    except Exception as e:
        print("[/issued_roster] error", e)
        print("[/issued_roster] target_group_id", target_group_id)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get issued roster snapshot: {str(e)}",
        )


# [Schedules] - 현재 그룹의 특정 월에 대한 스케줄 상태 확인
@router.get("/status")
async def get_schedule_status(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    try:
        override_gid: Optional[str] = None
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if current_user.is_head_nurse and current_user.group_id:
            override_gid = None
        elif getattr(current_user, "is_master_admin", False):
            if not group_id:
                raise HTTPException(
                    status_code=400, detail="group_id is required for admin"
                )
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if (
                getattr(current_user, "office_id", None)
                and current_user.office_id != g.office_id
            ):
                raise HTTPException(
                    status_code=403, detail="Group does not belong to your office"
                )
            override_gid = g.group_id
        return get_schedule_status_service(
            year, month, current_user, db, override_group_id=override_gid
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get schedule status: {str(e)}"
        )


# 삭제(드롭) 엔드포인트: schedule.dropped=1로 마킹
@router.delete("/{schedule_id}")
async def drop_schedule(
    schedule_id: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹 결정
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, "is_master_admin", False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(
                status_code=400, detail="group_id is required for admin"
            )
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if (
            getattr(current_user, "office_id", None)
            and current_user.office_id != g.office_id
        ):
            raise HTTPException(
                status_code=403, detail="Group does not belong to your office"
            )
        target_group_id = g.group_id

    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.schedule_id == schedule_id,
            Schedule.group_id == target_group_id,
            Schedule.dropped == False,
        )
        .first()
    )
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
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # 대상 그룹 결정
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, "is_master_admin", False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(
                status_code=400, detail="group_id is required for admin"
            )
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if (
            getattr(current_user, "office_id", None)
            and current_user.office_id != g.office_id
        ):
            raise HTTPException(
                status_code=403, detail="Group does not belong to your office"
            )
        target_group_id = g.group_id

    # Get schedule info
    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.schedule_id == schedule_id,
            Schedule.group_id == target_group_id,
            Schedule.dropped == False,
        )
        .first()
    )

    if not schedule:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")

    # Get all nurses in the group
    nurses_in_group = (
        db.query(Nurse.nurse_id, Nurse.name, Nurse.experience)
        .filter(Nurse.group_id == target_group_id)
        .order_by(Nurse.experience.desc(), Nurse.nurse_id.asc())
        .all()
    )

    # Get shift manage data
    # Get shift colors
    shifts_db = db.query(Shift).all()
    shift_colors = {s.shift_id: s.color for s in shifts_db}
    shift_id = {s.shift_id: s.shift_id for s in shifts_db}
    # Get schedule entries
    entries = (
        db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id).all()
    )
    # for e in entries:
    roster_data = {
        "year": schedule.year,
        "month": schedule.month,
        "schedule_id": schedule_id,
        "days_in_month": get_days_in_month(schedule.year, schedule.month),
        "shift_colors": shift_colors,
        "nurses": [],
        "memo": schedule.memo,
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
        # print('nurse', nurse.name)
        nurse_schedule = [
            entries_by_nurse.get(nurse.nurse_id, {}).get(d, "-")
            for d in range(1, roster_data["days_in_month"] + 1)
        ]

        counts = {shift: nurse_schedule.count(shift) for shift in shift_colors.keys()}

        roster_data["nurses"].append(
            {
                "id": nurse.nurse_id,
                "name": nurse.name,
                "experience": nurse.experience,
                "schedule": nurse_schedule,
                "counts": counts,
            }
        )
    # print('roster_data', roster_data['nurses'])
    roster_data["violations"] = violations
    return roster_data


# [Schedules] - 특정 월의 모든 버전 목록 조회 (수간호사용)
@router.get("/{year:int}/{month:int}/versions")
async def get_schedule_versions(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if current_user.is_master_admin:
        target_group_id = group_id
    else:
        target_group_id = current_user.group_id

    schedules = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == target_group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,
        )
        .order_by(Schedule.version.desc())
        .all()
    )
    print("[roster.py - get_schedule_versions] target_group_id", target_group_id)

    return [
        {
            "schedule_id": schedule.schedule_id,
            "version": schedule.version,
            "status": schedule.status,
            "created_at": schedule.created_at.isoformat()
            if schedule.created_at
            else None,
            "created_by": schedule.created_by,
            "updated_at": schedule.updated_at.isoformat()
            if schedule.updated_at
            else None,
            "name": schedule.name,
        }
        for schedule in schedules
    ]


# [Roster] - 특정 월의 근무표 조회
@router.get("/{year:int}/{month:int}")
async def get_roster_for_month(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
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
            raise HTTPException(
                status_code=400, detail="group_id is required for admin"
            )
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if (
            getattr(current_user, "office_id", None)
            and current_user.office_id != g.office_id
        ):
            raise HTTPException(
                status_code=403, detail="Group does not belong to your office"
            )
        target_group_id = g.group_id
    print("target_group_id1", target_group_id)
    # Get latest issued schedule for the month
    schedule_info = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == target_group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.status == "issued",
            Schedule.dropped == False,
        )
        .order_by(Schedule.version.desc())
        .first()
    )

    if not schedule_info:
        raise HTTPException(
            status_code=404, detail="No issued roster found for this month."
        )

    # Get all nurses in the group
    nurses_in_group = (
        db.query(Nurse.nurse_id, Nurse.name, Nurse.experience)
        .filter(Nurse.group_id == target_group_id)
        .order_by(Nurse.experience.desc(), Nurse.nurse_id.asc())
        .all()
    )

    # Get shift colors
    shifts_db = db.query(Shift).filter(Shift.group_id == target_group_id).all()
    shift_colors = {s.shift_id: s.color for s in shifts_db}

    # Get schedule entries
    entries = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == schedule_info.schedule_id)
        .all()
    )

    roster_data = {
        "year": year,
        "month": month,
        "days_in_month": get_days_in_month(year, month),
        "shift_colors": shift_colors,
        "nurses": [],
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
        nurse_schedule = [
            entries_by_nurse.get(nurse.nurse_id, {}).get(d, "-")
            for d in range(1, roster_data["days_in_month"] + 1)
        ]

        counts = {shift: nurse_schedule.count(shift) for shift in shift_colors.keys()}

        roster_data["nurses"].append(
            {
                "id": nurse.nurse_id,
                "name": nurse.name,
                "experience": nurse.experience,
                "schedule": nurse_schedule,
                "counts": counts,
            }
        )
    roster_data["violations"] = violations

    return roster_data


# [Roster] - 근무표 발행
@router.post("/publish")
async def publish_roster(
    req: PublishRequest,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        # 대상 간호사/그룹/오피스 결정 (현재는 수간호사 기준)
        nurse = db.query(Nurse).filter(Nurse.nurse_id == current_user.nurse_id).first()
        if not nurse:
            raise HTTPException(
                status_code=404, detail="간호사 정보를 찾을 수 없습니다."
            )

        office_id = nurse.group.office_id
        target_group_id = current_user.group_id
    except Exception as e:
        print(
            "[DEBUG] [roster.py - publish_roster] current_user", current_user.__dict__
        )
        print("[DEBUG] [roster.py - publish_roster] group_id", group_id)
        print("[DEBUG] [roster.py - publish_roster] error", e)
        raise HTTPException(status_code=500, detail=f"근무표 발행 실패: {e}")

    # Get schedule to publish
    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.schedule_id == req.schedule_id,
            Schedule.group_id == target_group_id,
            Schedule.dropped == False,
        )
        .first()
    )

    if not schedule:
        raise HTTPException(status_code=404, detail="해당 스케줄을 찾을 수 없습니다.")

    # Check if this is the first publication
    existing_issued = (
        db.query(IssuedRoster)
        .filter(
            IssuedRoster.group_id == target_group_id,
            IssuedRoster.office_id == office_id,
        )
        .first()
    )

    is_first_issue = not existing_issued

    # Get next sequence number

    max_seq = (
        db.query(func.max(IssuedRoster.seq_no))
        .filter(
            IssuedRoster.group_id == target_group_id,
            IssuedRoster.office_id == office_id,
        )
        .scalar()
        or 0
    )
    print("[DEBUG] [roster.py - publish_roster] max_seq", max_seq)
    # Set all other schedules in this month to draft
    db.query(Schedule).filter(
        Schedule.group_id == target_group_id,
        Schedule.year == schedule.year,
        Schedule.month == schedule.month,
        Schedule.status == "issued",
    ).update({"status": "draft"})

    # Update current schedule to issued
    schedule.status = "issued"

    # Create issued roster record
    issued_roster = IssuedRoster(
        seq_no=max_seq + 1,
        office_id=office_id,
        group_id=target_group_id,
        nurse_id=current_user.nurse_id,
        version=schedule.version,
        v_name=f"v{schedule.version}",  # 기본 버전명
        issue_cmmt=req.issue_comment if not is_first_issue else "첫 발행",
        schedule_id=req.schedule_id,
    )

    # 발행 시점 스냅샷 레코드 생성
    snapshot = create_issued_roster_snapshot(
        schedule=schedule,
        year=schedule.year,
        month=schedule.month,
        current_user=current_user,
        office_id=office_id,
        group_id=target_group_id,
        db=db,
    )
    # commit 전에 조회 → 신규 IssuedRoster가 아직 없으므로 이전 발행 기록만 정확히 감지
    prev_issued = (
        db.query(IssuedRoster)
        .join(Schedule, IssuedRoster.schedule_id == Schedule.schedule_id)
        .filter(
            IssuedRoster.group_id == target_group_id,
            IssuedRoster.office_id == office_id,
            Schedule.year == schedule.year,
            Schedule.month == schedule.month,
        )
        .first()
    )
    is_republish = prev_issued is not None

    db.add(issued_roster)
    db.add(snapshot)
    db.commit()
    nurses_in_group = (
        db.query(Nurse.nurse_id).filter(Nurse.group_id == target_group_id).all()
    )
    print("[DEBUG] [roster.py - publish_roster] nurses_in_group", nurses_in_group)
    receive_emp_seq_no = [nurse.nurse_id for nurse in nurses_in_group]

    if is_republish:
        send_roster_republish_push(
            year=schedule.year,
            month=schedule.month,
            recipients=receive_emp_seq_no,
            office_code=office_id,
            sender_emp_seq_no=current_user.nurse_id,
            sender_member_id=current_user.account_id,
        )
    else:
        send_roster_publish_push(
            year=schedule.year,
            month=schedule.month,
            recipients=receive_emp_seq_no,
            office_code=office_id,
            sender_emp_seq_no=current_user.nurse_id,
            sender_member_id=current_user.account_id,
        )

    # return message_result

    return {
        "message": "근무표가 성공적으로 발행되었습니다.",
        # "seq_no": issued_roster.seq_no,
        "is_first_issue": is_first_issue,
    }


# [Roster] - 근무표 발행 취소 (마감 철회)
@router.post("/unpublish")
async def unpublish_roster(
    schedule_id: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    발행된 근무표를 draft 상태로 되돌립니다.
    - Schedule.status: 'issued' → 'draft'
    - IssuedRoster.is_active: True → False
    - IssuedRosterSnapshot.is_active_issued: True → False
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (
        getattr(current_user, "is_head_nurse", False)
        or getattr(current_user, "is_master_admin", False)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")

    # 대상 그룹 결정
    if getattr(current_user, "is_head_nurse", False) and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not group_id:
            raise HTTPException(
                status_code=400, detail="group_id is required for admin"
            )
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if (
            getattr(current_user, "office_id", None)
            and current_user.office_id != g.office_id
        ):
            raise HTTPException(
                status_code=403, detail="Group does not belong to your office"
            )
        target_group_id = g.group_id

    # 스케줄 조회
    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.schedule_id == schedule_id,
            Schedule.group_id == target_group_id,
            Schedule.dropped == False,
        )
        .first()
    )

    if not schedule:
        raise HTTPException(status_code=404, detail="해당 스케줄을 찾을 수 없습니다.")

    if schedule.status != "issued":
        raise HTTPException(
            status_code=400, detail="발행되지 않은 스케줄은 발행 취소할 수 없습니다."
        )

    # 1) Schedule status를 draft로 변경
    schedule.status = "draft"

    # 2) IssuedRoster의 is_active를 False로 변경
    db.query(IssuedRoster).filter(
        IssuedRoster.schedule_id == schedule_id,
        IssuedRoster.group_id == target_group_id,
    ).update({"is_active": False})

    # 3) IssuedRosterSnapshot의 is_active_issued를 False로 변경
    db.query(IssuedRosterSnapshot).filter(
        IssuedRosterSnapshot.schedule_id == schedule_id,
        IssuedRosterSnapshot.group_id == target_group_id,
    ).update({"is_active_issued": False})

    db.commit()

    return {
        "message": "근무표 발행이 취소되었습니다.",
        "schedule_id": schedule_id,
        "new_status": "draft",
    }


# [Roster] - 특정 월의 근무표 조회
@router.get("/{year: int}/{month: int}")
async def get_roster_for_month(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹 결정
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not getattr(current_user, "is_master_admin", False):
            raise HTTPException(status_code=403, detail="Permission denied")
        if not group_id:
            raise HTTPException(
                status_code=400, detail="group_id is required for admin"
            )
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if (
            getattr(current_user, "office_id", None)
            and current_user.office_id != g.office_id
        ):
            raise HTTPException(
                status_code=403, detail="Group does not belong to your office"
            )
        target_group_id = g.group_id

    # Get latest issued schedule for the month
    schedule_info = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == target_group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.status == "issued",
        )
        .order_by(Schedule.version.desc())
        .first()
    )

    if not schedule_info:
        raise HTTPException(
            status_code=404, detail="No issued roster found for this month."
        )

    # Get all nurses in the group
    nurses_in_group = (
        db.query(Nurse.nurse_id, Nurse.name, Nurse.experience)
        .filter(Nurse.group_id == target_group_id)
        .order_by(Nurse.experience.desc(), Nurse.nurse_id.asc())
        .all()
    )

    # Get shift colors
    shifts_db = db.query(Shift).all()
    shift_colors = {s.shift_id: s.color for s in shifts_db}

    # Get schedule entries
    entries = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == schedule_info.schedule_id)
        .all()
    )

    roster_data = {
        "year": year,
        "month": month,
        "days_in_month": get_days_in_month(year, month),
        "shift_colors": shift_colors,
        "nurses": [],
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
        nurse_schedule = [
            entries_by_nurse.get(nurse.nurse_id, {}).get(d, "-")
            for d in range(1, roster_data["days_in_month"] + 1)
        ]

        counts = {shift: nurse_schedule.count(shift) for shift in shift_colors.keys()}

        roster_data["nurses"].append(
            {
                "id": nurse.nurse_id,
                "name": nurse.name,
                "experience": nurse.experience,
                "schedule": nurse_schedule,
                "counts": counts,
            }
        )
    roster_data["violations"] = violations

    return roster_data


# [Roster] - 근무표 저장
@router.post("/save")
async def save_roster(
    roster_data: dict,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (
        getattr(current_user, "is_head_nurse", False)
        or getattr(current_user, "is_master_admin", False)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")

    year = roster_data.get("year")
    month = roster_data.get("month")
    schedule_id = roster_data.get("schedule_id")
    roster = roster_data.get("roster")
    memo = roster_data.get("memo")
    if not all([year, month, schedule_id, roster]):
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: year, month, schedule_id, roster",
        )

    # Get the latest schedule for the month
    # 대상 그룹 결정
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not group_id:
            raise HTTPException(
                status_code=400, detail="group_id is required for admin"
            )
        g = db.query(Group).filter(Group.group_id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if (
            getattr(current_user, "office_id", None)
            and current_user.office_id != g.office_id
        ):
            raise HTTPException(
                status_code=403, detail="Group does not belong to your office"
            )
        target_group_id = g.group_id

    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == target_group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.schedule_id == schedule_id,
        )
        .order_by(Schedule.schedule_id.desc())
        .first()
    )

    if not schedule:
        raise HTTPException(status_code=404, detail="No schedule found for this month")
    schedule.memo = memo
    # Clear existing roster entries
    db.query(ScheduleEntry).filter(
        ScheduleEntry.schedule_id == schedule.schedule_id
    ).delete()

    # Save new roster entries (케이스 보존을 위해 유효 shift_id 기반 정규화)
    valid_shift_ids = {
        s.shift_id
        for s in db.query(Shift)
        .filter(
            Shift.group_id == target_group_id,
            Shift.office_id == getattr(current_user, "office_id", None),
        )
        .all()
    }

    def _normalize_shift_id_for_save_router(raw_shift: str) -> str:
        if raw_shift in valid_shift_ids:
            return raw_shift
        upper = raw_shift.upper()
        if upper in valid_shift_ids:
            return upper
        match = next(
            (sid for sid in valid_shift_ids if sid.upper() == upper), raw_shift
        )
        return match

    for nurse in roster:
        nurse_id = nurse.get("nurse_id") or nurse.get("id")  # 둘 다 체크
        if not nurse_id:
            continue  # nurse_id가 없으면 건너뛰기

        schedule_data = nurse.get("schedule", [])
        for day_index, shift_id in enumerate(schedule_data):
            if shift_id and shift_id.strip():  # 빈 값이 아닌 경우만
                work_date = date(year, month, day_index + 1)
                norm_shift = _normalize_shift_id_for_save_router(str(shift_id))
                entry = ScheduleEntry(
                    entry_id=str(uuid.uuid4().hex)[:16],
                    schedule_id=schedule.schedule_id,
                    nurse_id=nurse_id,
                    work_date=work_date,
                    shift_id=norm_shift,
                )
                db.add(entry)

    db.commit()
    return {"message": "Roster saved successfully"}


# [Schedules] - 특정 스케줄의 모든 간호사 원티드 제출 현황 확인
@router.get("/{year:int}/{month:int}/submissions")
async def get_submission_statuses(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 대상 그룹 결정 (HN: 본인 그룹, ADM: 쿼리로 지정)
    if group_id:
        target_group_id = group_id
    else:
        target_group_id = current_user.group_id

    # 대상 그룹의 간호사 수집
    nurses_in_group = (
        db.query(Nurse.nurse_id).filter(Nurse.group_id == target_group_id).all()
    )
    nurse_ids_in_group = {n[0] for n in nurses_in_group}
    # 각 간호사의 최신 제출 상태 확인
    submitted_nurse_ids = set()
    month_str = f"{year:04d}-{month:02d}"
    for nurse_id in nurse_ids_in_group:
        # 해당 간호사의 최신 제출된 선호도가 있는지 확인
        latest_submitted = (
            db.query(WantedRequest)
            .filter(
                WantedRequest.nurse_id == nurse_id,
                # ShiftPreference.year == year,
                WantedRequest.month == month_str,
                WantedRequest.is_submitted == True,
            )
            .order_by(WantedRequest.submitted_at.desc())
            .first()
        )
        if latest_submitted:
            submitted_nurse_ids.add(nurse_id)
    return {
        "submitted_nurses": list(submitted_nurse_ids),
    }


# [Schedules] - 현재 그룹의 특정 월에 대한 스케줄 상태 확인
@router.get("/status")
async def get_schedule_status(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 수간호사/관리자: 그룹 요약 조회
    if getattr(current_user, "is_head_nurse", False) or getattr(
        current_user, "is_master_admin", False
    ):
        if current_user.is_head_nurse and current_user.group_id:
            target_group_id = current_user.group_id
        else:
            if not group_id:
                raise HTTPException(
                    status_code=400, detail="group_id is required for admin"
                )
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if (
                getattr(current_user, "office_id", None)
                and current_user.office_id != g.office_id
            ):
                raise HTTPException(
                    status_code=403, detail="Group does not belong to your office"
                )
            target_group_id = g.group_id
        schedules = (
            db.query(Schedule)
            .filter(
                Schedule.group_id == target_group_id,
                Schedule.year == year,
                Schedule.month == month,
            )
            .all()
        )
        has_schedules = len(schedules) > 0
        latest_status = schedules[0].status if schedules else None
        return {
            "has_schedules": has_schedules,
            "latest_status": latest_status,
            "schedule_count": len(schedules),
        }

    # 일반 간호사인 경우 - 최신 선호도 데이터 조회
    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == current_user.group_id,
            Schedule.year == year,
            Schedule.month == month,
        )
        .order_by(Schedule.version.desc())
        .first()
    )

    # 최신 제출된 선호도 먼저 확인
    submitted_preference = (
        db.query(ShiftPreference)
        .filter(
            ShiftPreference.nurse_id == current_user.nurse_id,
            ShiftPreference.year == year,
            ShiftPreference.month == month,
            ShiftPreference.is_submitted == True,
        )
        .order_by(ShiftPreference.submitted_at.desc())
        .first()
    )

    if submitted_preference:
        return {
            "schedule_status": schedule.status if schedule else None,
            "preference_is_submitted": True,
            "preference_data": submitted_preference.data,
            "has_schedules": schedule is not None,
            "created_at": submitted_preference.created_at,
            "submitted_at": submitted_preference.submitted_at,
        }

    # 제출된 것이 없으면 최신 draft 확인
    draft_preference = (
        db.query(ShiftPreference)
        .filter(
            ShiftPreference.nurse_id == current_user.nurse_id,
            ShiftPreference.year == year,
            ShiftPreference.month == month,
            ShiftPreference.is_submitted == False,
        )
        .order_by(ShiftPreference.created_at.desc())
        .first()
    )

    if draft_preference:
        return {
            "schedule_status": schedule.status if schedule else None,
            "preference_is_submitted": False,
            "preference_data": draft_preference.data,
            "has_schedules": schedule is not None,
            "created_at": draft_preference.created_at,
            "submitted_at": None,
        }

    # 아무 선호도도 없는 경우
    return {
        "schedule_status": schedule.status if schedule else None,
        "preference_is_submitted": False,
        "preference_data": None,
        "has_schedules": schedule is not None,
        "created_at": None,
        "submitted_at": None,
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
    if not (
        getattr(current_user, "is_head_nurse", False)
        or getattr(current_user, "is_master_admin", False)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")

    year: int = roster_data.get("year")
    month: int = roster_data.get("month")
    roster = roster_data.get("roster")
    schedule_id = roster_data.get("schedule_id")
    config_id = (
        db.query(Schedule).filter(Schedule.schedule_id == schedule_id).first().config_id
    )

    # config_version = db.query(RosterConfigModel).filter(RosterConfigModel.config_id == config_id).first().config_version
    if not all([year, month, roster]):
        raise HTTPException(
            status_code=400, detail="Missing required fields: year, month, roster"
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
                raise HTTPException(
                    status_code=400, detail="group_id is required for admin"
                )
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if (
                getattr(current_user, "office_id", None)
                and current_user.office_id != g.office_id
            ):
                raise HTTPException(
                    status_code=403, detail="Group does not belong to your office"
                )
            office_id = g.office_id
            target_group_id = g.group_id

        shift_rows = (
            db.query(ShiftManage)
            .filter(
                ShiftManage.office_id == office_id,
                ShiftManage.group_id == target_group_id,
                ShiftManage.nurse_class == "RN",
            )
            .order_by(ShiftManage.shift_slot.asc())
            .all()
        )
        #    예) { 'D': 'D', 'D1': 'D', 'MD': 'D',  'E': 'E', … }
        alias_map: dict[str, str] = {}
        daily_shift_requirements = {}
        for row in shift_rows:
            if not row.main_code:
                continue
            base = row.main_code.upper()  # ex) 'D'
            alias_map[base] = base
            # daily_shift_requirements[row.main_code.strip()] = row.manpower
            daily_shift_requirements[row.main_code] = row.manpower
            if row.codes:
                # row.codes 가 JSON 컬럼 → 이미 list 로 deserialize 되어있음
                for code in row.codes:
                    alias_map[code.upper()] = base
        try:
            shift_defs = (
                db.query(Shift.shift_id, Shift.default_shift)
                .filter(Shift.office_id == office_id, Shift.group_id == target_group_id)
                .all()
            )
            for sid, default_shift in shift_defs:
                if sid:
                    sid_u = str(sid).upper()
                    base = str(default_shift or "").strip().upper()
                    if base in {"OFF", "주"}:
                        base = "O"
                    if base in {"D", "E", "N", "O", "M", "W"}:
                        alias_map.setdefault(sid_u, base)
        except Exception as e:
            print(f"[validate_roster] shift default 정규화 로딩 실패: {e}")
        alias_map.setdefault("OFF", "O")
        alias_map.setdefault("O", "O")
        alias_map.setdefault("주", "O")
        # ──────────────────────── 2. 근무표 설정(인원/제약) 불러오기 ────────────────────────
        latest_config_db = (
            db.query(RosterConfigModel)
            .filter(
                RosterConfigModel.office_id == office_id,
                RosterConfigModel.group_id == target_group_id,
            )
            .order_by(RosterConfigModel.created_at.desc())
            .first()
        )
        if not latest_config_db:
            return {"violations": ["근무표 설정을 찾을 수 없습니다."]}

        max_nig = latest_config_db.max_nig_per_month
        if max_nig != 15:
            max_nig = 17

        roster_config_for_engine = NurseRosterConfig(
            daily_shift_requirements=daily_shift_requirements,
            max_consecutive_work_days=latest_config_db.max_conseq_work,
            max_night_shifts_per_month=max_nig,
            max_consecutive_nights=3 if latest_config_db.three_seq_nig else 2,
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
        by_day = {
            r.day: {
                "D": int(r.d_count or 0),
                "E": int(r.e_count or 0),
                "N": int(r.n_count or 0),
            }
            for r in rows
        }
        daily_shift_requirements_by_day = [
            by_day.get(
                d,
                {
                    "D": daily_shift_requirements.get("D", 0),
                    "E": daily_shift_requirements.get("E", 0),
                    "N": daily_shift_requirements.get("N", 0),
                },
            )
            for d in range(1, days_in_month + 1)
        ]

        # ──────────────────────── 3. RosterSystem 초기화 ────────────────────────
        nurses_for_engine = [
            NurseEngine.from_db_model(n, i)
            for i, n in enumerate(
                db.query(Nurse).filter(Nurse.group_id == target_group_id).all()
            )
        ]
        # 일자별 요구 인원은 config 객체에 속성으로 주입하여 사용
        try:
            setattr(
                roster_config_for_engine,
                "daily_shift_requirements_by_day",
                daily_shift_requirements_by_day,
            )
        except Exception as e:
            print(f"error: {e}")
            pass
        system = RosterSystem(
            nurses=nurses_for_engine,
            target_month=date(year, month, 1),
            config=roster_config_for_engine,
        )
        # shift_types 는 ['D','E','N','O'] (엔진 기본).
        shift_map = {s: i for i, s in enumerate(system.config.shift_types)}
        system.roster.fill(0)  # 3-D 배열 0으로 초기화
        # ──────────────────────── 4. 프론트에서 넘어온 근무표 → 엔진 포맷 변환 ────────────────────────
        nurse_idx_map: dict[str, int] = {
            str(n.db_id): idx for idx, n in enumerate(system.nurses)
        }

        for nurse_data in roster:
            nurse_identifier = (
                nurse_data.get("nurse_id")
                or nurse_data.get("id")
                or nurse_data.get("db_id")
            )
            if nurse_identifier is None:
                continue

            target_idx = nurse_idx_map.get(str(nurse_identifier))
            if target_idx is None:
                # 매핑 실패 시 해당 간호사는 건너뜀
                continue

            schedule = nurse_data.get("schedule", [])
            for day_idx, raw_shift in enumerate(schedule):
                if day_idx >= system.num_days:
                    continue
                # ① 대소문자 무시
                raw_shift = (raw_shift or "").upper()

                # ② alias_map 으로 본교대 변환
                base_shift = alias_map.get(raw_shift, raw_shift)

                # ③ 엔진 shift index 찾기
                shift_idx = shift_map.get(base_shift)
                if shift_idx is not None:
                    system.roster[target_idx, day_idx, shift_idx] = 1
                # else: 알 수 없는 코드 → 무시
        # ──────────────────────── 5. 위반사항 탐색 & 포매팅 ────────────────────────
        violation_details = system._find_violations()
        # print('violation_details')
        # import pprint
        # pprint.pprint(violation_details)
        violation_messages: set[str] = set()
        detailed_violations: list[dict] = []
        for v in violation_details:
            if v["type"] == "shift_requirement":
                # print( f"{v['day'] + 1}일: {v['shift']} 근무 인원 미달 ")
                violation_messages.add(
                    f"{v['day'] + 1}일: {v['shift']} 근무 인원 미달 "
                    f"(필요: {v['required']}, 배정: {v['actual']})"
                )
                detailed_violations.append(
                    {
                        "type": "shift_requirement",
                        "day": v["day"],
                        "shift": v["shift"],
                        "required": v["required"],
                        "actual": v["actual"],
                    }
                )
            elif v["type"] == "consecutive_work":
                nurse_name = system.nurses[v["nurse_idx"]].name
                violation_messages.add(f"{nurse_name}: 최대 연속 근무일 초과")
                detailed_violations.append(
                    {
                        "type": "consecutive_work",
                        "nurse_idx": v["nurse_idx"],
                        "nurse_name": nurse_name,
                        "day": v["day"] + 1,
                    }
                )
            elif v["type"] == "night_consecutive":
                nurse_name = system.nurses[v["nurse_idx"]].name
                violation_messages.add(f"{nurse_name}: 야간 연속 근무 위반")
                detailed_violations.append(
                    {
                        "type": "night_consecutive",
                        "nurse_idx": v["nurse_idx"],
                        "nurse_name": nurse_name,
                        "day": v["day"] + 1,
                    }
                )
            # elif v['type'] == 'night_nd':
            #     nurse_name = system.nurses[v['nurse_idx']].name
            #     violation_messages.add(f"{nurse_name}: 야간 근무 후 주간 근무 위반")
            #     detailed_violations.append({
            #         'type': 'night_nd',
            #         'nurse_idx': v['nurse_idx'],
            #         'nurse_name': nurse_name,
            #         'day': v['day']
            #     })
            elif v["type"] == "night_month_limit":
                nurse_name = system.nurses[v["nurse_idx"]].name
                violation_messages.add(f"{nurse_name}: 월 야간 근무 초과 위반")
                detailed_violations.append(
                    {
                        "type": "night_month_limit",
                        "nurse_idx": v["nurse_idx"],
                        "nurse_name": nurse_name,
                        "day": v["day"],
                    }
                )
        # print('violation_messages')
        # import pprint
        # pprint.pprint(sorted(violation_messages))
        # pprint.pprint(detailed_violations)
        return {
            "violations": sorted(violation_messages),
            "detailed_violations": detailed_violations,
        }

    # ──────────────────────── 6. 예외 처리 ────────────────────────
    except Exception as e:
        print(f"[validate_roster] 오류: {e}")
        return {
            "violations": [f"위반사항 계산 중 오류가 발생했습니다: {str(e)}"],
            "detailed_violations": [],
        }


# [Roster] - 스케줄 이름 업데이트
@router.patch("/{schedule_id}/name")
async def update_schedule_name(
    schedule_id: str,
    name_data: dict,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not (
        getattr(current_user, "is_head_nurse", False)
        or getattr(current_user, "is_master_admin", False)
    ):
        raise HTTPException(status_code=403, detail="Permission denied")

    try:
        # 스케줄 조회
        if current_user.is_head_nurse and current_user.group_id:
            target_group_id = current_user.group_id
        else:
            if not group_id:
                raise HTTPException(
                    status_code=400, detail="group_id is required for admin"
                )
            g = db.query(Group).filter(Group.group_id == group_id).first()
            if not g:
                raise HTTPException(status_code=404, detail="Group not found")
            if (
                getattr(current_user, "office_id", None)
                and current_user.office_id != g.office_id
            ):
                raise HTTPException(
                    status_code=403, detail="Group does not belong to your office"
                )
            target_group_id = g.group_id

        schedule = (
            db.query(Schedule)
            .filter(
                Schedule.schedule_id == schedule_id,
                Schedule.group_id == target_group_id,
                Schedule.dropped == False,
            )
            .first()
        )

        if not schedule:
            raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")

        # 이름 업데이트
        new_name = name_data.get("name")
        if not new_name or not new_name.strip():
            raise HTTPException(status_code=400, detail="이름을 입력해주세요.")

        schedule.name = new_name.strip()
        schedule.updated_at = datetime.now()

        db.add(schedule)
        db.commit()

        return {
            "message": "스케줄 이름이 성공적으로 업데이트되었습니다.",
            "name": new_name,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"이름 업데이트 실패: {str(e)}")


@router.get("/schedule/{schedule_id}/export", response_class=FileResponse)
async def export_schedule_excel(
    schedule_id: str,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    - 근무표 엑셀 내보내기
    - 파일명: roster_{year}_{month}_v{version}.xlsx
    """

    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.schedule_id == schedule_id,
            # Schedule.group_id == target_group_id,
            Schedule.dropped == False,
        )
        .first()
    )
    if not schedule:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    try:
        from services.excel_service import export_schedule_excel_bytes

        if group_id in [None, "", "null", "undefined", "None"]:
            target_group_id = current_user.group_id
        else:
            target_group_id = group_id
        data = export_schedule_excel_bytes(
            schedule_id, current_user, db, target_group_id
        )
        filename = f"roster_{schedule.year}_{schedule.month}_v{schedule.version}.xlsx"
        from io import BytesIO

        output = BytesIO(data)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        print(f"[export_schedule_excel] 오류: {e}")
        raise HTTPException(status_code=500, detail=f"엑셀 생성 실패: {str(e)}")


def _get_target_group_id(
    current_user: UserSchema, group_id_param: Optional[str], db: Session
) -> str:
    """대상 그룹 결정 로직 (기존 코드 재사용)"""
    if current_user.is_head_nurse and current_user.group_id:
        return current_user.group_id

    if not getattr(current_user, "is_master_admin", False):
        raise HTTPException(status_code=403, detail="Permission denied")

    if not group_id_param:
        raise HTTPException(status_code=400, detail="group_id is required for admin")

    g = db.query(Group).filter(Group.group_id == group_id_param).first()
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")

    if (
        getattr(current_user, "office_id", None)
        and current_user.office_id != g.office_id
    ):
        raise HTTPException(
            status_code=403, detail="Group does not belong to your office"
        )

    return g.group_id


def _get_next_version(db: Session, group_id: str, year: int, month: int) -> int:
    """같은 연/월 내 최대 version + 1"""
    max_ver = (
        db.query(func.max(Schedule.version))
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,
        )
        .scalar()
        or 0
    )
    return max_ver + 1


@router.post("/copy/{source_schedule_id}")
async def copy_schedule_to_new_version(
    source_schedule_id: str,
    request: dict = Body({}),
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user or not (
        current_user.is_head_nurse or current_user.is_master_admin
    ):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    target_group_id = _get_target_group_id(current_user, group_id, db)

    # 원본 조회 + 디버깅 로그
    source = (
        db.query(Schedule)
        .filter(
            Schedule.schedule_id == source_schedule_id,
            Schedule.group_id == target_group_id,
            Schedule.dropped == False,
        )
        .first()
    )

    if not source:
        raise HTTPException(status_code=404, detail="복사할 근무표를 찾을 수 없습니다.")

    print(f"[COPY DEBUG] 원본 schedule_id: {source.schedule_id}")
    print(f"[COPY DEBUG] 원본 version: {source.version}, name: {source.name}")

    if request.get("year") is not None or request.get("month") is not None:
        raise HTTPException(
            status_code=400, detail="다른 연/월 복사는 지원되지 않습니다."
        )

    target_year = source.year
    target_month = source.month

    new_version = _get_next_version(db, target_group_id, target_year, target_month)

    original_name = source.name or f"{target_month}월 근무표 VER{source.version}"
    new_name = request.get("new_name", f"복사 - {original_name}").strip()

    new_schedule_id = str(uuid.uuid4().hex)[:12]

    new_schedule = Schedule(
        schedule_id=new_schedule_id,
        office_id=source.office_id,
        group_id=target_group_id,
        year=target_year,
        month=target_month,
        version=new_version,
        config_id=source.config_id,
        created_by=current_user.account_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        status="draft",
        dropped=False,
        name=new_name,
        memo=source.memo,
    )
    db.add(new_schedule)

    # ScheduleEntry 복사 - 여기서 디버깅 핵심
    entries = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == source.schedule_id)
        .all()
    )

    print(f"[COPY DEBUG] 복사 대상 entries 수: {len(entries)}")
    if len(entries) == 0:
        print(
            f"[COPY WARNING] 원본 schedule_id '{source.schedule_id}' 에 배정 데이터가 없습니다! 빈 복사 발생"
        )
        print(
            f"[COPY WARNING] DB 직접 확인 필요: SELECT COUNT(*) FROM schedule_entries WHERE schedule_id = '{source.schedule_id}'"
        )
    else:
        print(
            f"[COPY DEBUG] 첫 entry 예시: nurse_id={entries[0].nurse_id}, work_date={entries[0].work_date}, shift={entries[0].shift_id}"
        )

    for entry in entries:
        db.add(
            ScheduleEntry(
                entry_id=str(uuid.uuid4().hex)[:16],
                schedule_id=new_schedule_id,
                nurse_id=entry.nurse_id,
                work_date=entry.work_date,  # 그대로 복사 (DATETIME 유지)
                shift_id=entry.shift_id,
            )
        )

    try:
        db.commit()
        db.refresh(new_schedule)
        print(
            f"[COPY SUCCESS] 새 schedule_id: {new_schedule_id}, version: {new_version}"
        )
    except Exception as e:
        db.rollback()
        print(f"[COPY ERROR] 커밋 실패: {str(e)}")
        raise HTTPException(status_code=500, detail=f"복사 실패: {str(e)}")

    return {
        "message": "새 버전으로 복사 완료",
        "new_schedule_id": new_schedule.schedule_id,
        "new_version": new_version,
        "new_name": new_name,
        "year": target_year,
        "month": target_month,
        "status": "draft",
        "entries_copied": len(entries),  # 추가: 몇 개 복사됐는지 반환
    }


@router.post("/create-empty")
async def create_empty_roster(
    year: int = Query(..., description="생성할 연도 (현재 탭 연도)"),
    month: int = Query(..., description="생성할 월 (현재 탭 월)"),
    name: Optional[str] = Query(None, description="새 버전 이름 (생략 시 자동 생성)"),
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    현재 보고 있는 연/월에 빈 신규 버전 생성
    - year/month: 쿼리 파라미터로 현재 탭 값 전달 (필수)
    - name: 옵션 (없으면 자동)
    - ScheduleEntry 생성 X → 완전 빈 근무표
    """
    if not current_user or not (
        current_user.is_head_nurse or current_user.is_master_admin
    ):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    target_group_id = _get_target_group_id(current_user, group_id, db)

    # year/month는 쿼리로 받음 (FastAPI가 자동 검증)
    new_version = _get_next_version(db, target_group_id, year, month)

    latest_config = (
        db.query(RosterConfigModel)
        .filter(
            RosterConfigModel.group_id == target_group_id,
            RosterConfigModel.office_id == current_user.office_id,
        )
        .order_by(RosterConfigModel.created_at.desc())
        .first()
    )

    # 이름 결정
    created_name = name or f"{month}월 근무표 VER{new_version}"

    new_schedule_id = str(uuid.uuid4().hex)[:12]

    new_schedule = Schedule(
        schedule_id=new_schedule_id,
        office_id=current_user.office_id,
        group_id=target_group_id,
        year=year,
        month=month,
        version=new_version,
        config_id=latest_config.config_id if latest_config else None,
        created_by=current_user.account_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        status="draft",
        dropped=False,
        name=created_name,
        memo=None,
    )
    db.add(new_schedule)

    # 빈 근무표 → ScheduleEntry 생성 안 함

    try:
        db.commit()
        db.refresh(new_schedule)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"빈 근무표 생성 실패: {str(e)}")

    return {
        "message": "빈 신규 버전 생성 완료",
        "new_schedule_id": new_schedule.schedule_id,
        "new_version": new_version,
        "name": created_name,
        "year": year,
        "month": month,
        "status": "draft",
        "entries_copied": 0,
    }


@router.post("/create-with-weekly-off")
async def create_roster_with_weekly_off(
    year: int = Query(..., description="생성할 연도 (현재 탭 연도)"),
    month: int = Query(..., description="생성할 월 (현재 탭 월)"),
    name: Optional[str] = Query(None, description="새 버전 이름 (생략 시 자동 생성)"),
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    현재 보고 있는 연/월에 주휴일만 포함된 신규 빈 근무표 생성
    """
    if not current_user or not (
        current_user.is_head_nurse or current_user.is_master_admin
    ):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    target_group_id = _get_target_group_id(current_user, group_id, db)

    new_version = _get_next_version(db, target_group_id, year, month)

    latest_config = (
        db.query(RosterConfigModel)
        .filter(
            RosterConfigModel.group_id == target_group_id,
            RosterConfigModel.office_id == current_user.office_id,
        )
        .order_by(RosterConfigModel.created_at.desc())
        .first()
    )

    weekly_off_on = (
        bool(getattr(latest_config, "weekly_off_group", False))
        if latest_config
        else False
    )
    created_name = name or (
        f"{month}월 근무표 VER{new_version} (주휴 포함)"
        if weekly_off_on
        else f"{month}월 근무표 VER{new_version}"
    )

    new_schedule_id = str(uuid.uuid4().hex)[:12]

    new_schedule = Schedule(
        schedule_id=new_schedule_id,
        office_id=current_user.office_id,
        group_id=target_group_id,
        year=year,
        month=month,
        version=new_version,
        config_id=latest_config.config_id if latest_config else None,
        created_by=current_user.account_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        status="draft",
        dropped=False,
        name=created_name,
        memo="주휴일 자동 포함 버전",
    )
    db.add(new_schedule)

    weekly_off_data = get_nurses_weekly_off_service(
        year=year, month=month, user=current_user, db=db, group_id=target_group_id
    )

    created_entries = 0

    nurses = []
    if isinstance(weekly_off_data, dict):
        nurses = weekly_off_data.get("items", [])
    elif hasattr(weekly_off_data, "items"):
        nurses = weekly_off_data.items

    print(f"[DEBUG] 추출된 nurses 수: {len(nurses)}")

    from datetime import date, timedelta

    for idx, nurse in enumerate(nurses):
        # Pydantic 모델이므로 속성 접근 사용
        nurse_id = getattr(nurse, "nurse_id", None)
        name = getattr(nurse, "name", "이름없음")
        enabled = getattr(nurse, "weekly_off_enabled", False)
        preview_wd = getattr(nurse, "preview_weekday", None)
        base_wd = getattr(nurse, "base_weekday", None)
        label = getattr(nurse, "preview_weekday_label", "")

        print(
            f"[DEBUG] {idx + 1}번째 간호사: {name} (id:{nurse_id}) enabled={enabled}, preview={preview_wd}, base={base_wd}, label={label}"
        )

        if not nurse_id or not enabled:
            print(f"[DEBUG] 스킵 - enabled=False 또는 id 없음: {name}")
            continue

        wd = preview_wd if preview_wd is not None else base_wd
        if wd is None:
            print(
                f"[DEBUG] 스킵 - 요일 없음: {name} (preview={preview_wd}, base={base_wd})"
            )
            continue

        print(f"[DEBUG] {name} 주휴 적용 시작 - 요일 {wd} ({label})")

        first_day = date(year, month, 1)
        current = first_day
        # 월 마지막 날 계산
        month_end = (first_day.replace(day=28) + timedelta(days=10)).replace(
            day=1
        ) - timedelta(days=1)

        count_for_this_nurse = 0
        while current <= month_end:
            if current.weekday() == wd:
                entry = ScheduleEntry(
                    entry_id=str(uuid.uuid4().hex)[:12],
                    schedule_id=new_schedule.schedule_id,
                    nurse_id=nurse_id,
                    work_date=current,
                    shift_id="주",
                )
                db.add(entry)
                created_entries += 1
                count_for_this_nurse += 1
                print(f"[DEBUG] 생성됨: {name} → {current} (weekday {wd})")

            current += timedelta(days=1)

        print(f"[DEBUG] {name} 주휴 총 생성: {count_for_this_nurse}개")

    try:
        db.commit()
        db.refresh(new_schedule)
        print(f"[DEBUG] 전체 주휴 엔트리 생성 완료: {created_entries}개")
    except Exception as e:
        db.rollback()
        print(f"[ERROR] commit 실패: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"주휴 포함 근무표 생성 실패: {str(e)}"
        )

    return {
        "message": "주휴일이 포함된 신규 빈 버전 생성 완료",
        "new_schedule_id": new_schedule.schedule_id,
        "new_version": new_version,
        "name": created_name,
        "year": year,
        "month": month,
        "status": "draft",
        "entries_created": created_entries,
    }


@router.post("/shares/schedules/{schedule_id}")
async def create_schedule_share_link(
    schedule_id: str,
    payload: ScheduleShareCreateRequest = Body(...),
    request: Request = None,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    from services.roster_service import create_schedule_share_link_service

    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    image_url = payload.image_url
    title = payload.title
    description = payload.description
    expires_in_days = payload.expires_in_days
    image_url = str(image_url).strip() if image_url is not None else ""
    title = str(title) if title is not None else None
    description = str(description) if description is not None else None
    if not image_url:
        raise HTTPException(status_code=400, detail="image_url is required")
    try:
        expires_in_days = int(expires_in_days)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expires_in_days must be integer")

    try:
        base_url = str(request.base_url).rstrip("/") if request else ""
        return create_schedule_share_link_service(
            db=db,
            current_user=current_user,
            schedule_id=schedule_id,
            fallback_base_url=base_url,
            image_url=image_url,
            title=title,
            description=description,
            expires_in_days=expires_in_days,
            override_group_id=group_id,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"공유 링크 생성 실패: {str(e)}")


@router.post("/shares/schedules/{schedule_id}/upload")
async def create_schedule_share_link_with_upload(
    schedule_id: str,
    image_file: UploadFile = File(...),
    title: Optional[str] = Form("근무표 공유"),
    description: Optional[str] = Form("공유된 근무표입니다."),
    expires_in_days: int = Form(3),
    group_id: Optional[str] = None,
    request: Request = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    from services.roster_service import (
        upload_schedule_share_image_and_create_link_service,
    )

    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        base_url = str(request.base_url).rstrip("/") if request else ""
        return upload_schedule_share_image_and_create_link_service(
            db=db,
            current_user=current_user,
            schedule_id=schedule_id,
            fallback_base_url=base_url,
            image_file=image_file,
            title=title,
            description=description,
            expires_in_days=expires_in_days,
            override_group_id=group_id,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"이미지 업로드/공유 링크 생성 실패: {str(e)}"
        )


@router.post("/shares/schedules/{schedule_id}/auto")
async def create_schedule_share_link_auto(
    schedule_id: str,
    payload: ScheduleShareAutoCreateRequest = Body(...),
    request: Request = None,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    from services.roster_service import (
        auto_generate_schedule_share_image_and_create_link_service,
    )

    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    title = payload.title
    description = payload.description
    expires_in_days = payload.expires_in_days

    try:
        base_url = str(request.base_url).rstrip("/") if request else ""
        return auto_generate_schedule_share_image_and_create_link_service(
            db=db,
            current_user=current_user,
            schedule_id=schedule_id,
            fallback_base_url=base_url,
            title=title,
            description=description,
            expires_in_days=expires_in_days,
            override_group_id=group_id,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"자동 이미지 생성/공유 링크 생성 실패: {str(e)}"
        )


@router.post("/shares/schedules/{schedule_id}/capture")
async def create_schedule_share_link_capture(
    schedule_id: str,
    payload: ScheduleShareCaptureCreateRequest = Body(...),
    request: Request = None,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    from services.roster_service import (
        capture_schedule_share_image_and_create_link_service,
    )

    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    image_data_url = payload.image_data_url
    title = payload.title
    description = payload.description
    expires_in_days = payload.expires_in_days

    try:
        base_url = str(request.base_url).rstrip("/") if request else ""
        return capture_schedule_share_image_and_create_link_service(
            db=db,
            current_user=current_user,
            schedule_id=schedule_id,
            fallback_base_url=base_url,
            image_data_url=image_data_url,
            title=title,
            description=description,
            expires_in_days=expires_in_days,
            override_group_id=group_id,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"캡처 이미지 공유 링크 생성 실패: {str(e)}"
        )


@router.delete("/shares/{share_token}")
async def revoke_schedule_share_link(
    share_token: str,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    from services.roster_service import revoke_schedule_share_link_service

    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        return revoke_schedule_share_link_service(
            db=db, current_user=current_user, token=share_token
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"공유 링크 해제 실패: {str(e)}")


@router.get("/s/{token}", response_class=HTMLResponse)
async def render_schedule_share_page(
    token: str, request: Request, db: Session = Depends(get_db)
):
    import html
    import os
    from services.roster_service import (
        get_public_share_link_service,
        _share_public_base_url,
    )

    share_row = get_public_share_link_service(db=db, token=token)
    if not share_row:
        raise HTTPException(status_code=404, detail="Share link not found or expired")

    schedule_row = (
        db.query(Schedule.year, Schedule.month)
        .filter(Schedule.schedule_id == share_row.get("schedule_id"))
        .first()
    )
    nurse_row = (
        db.query(Nurse.name)
        .filter(Nurse.nurse_id == share_row.get("created_by_nurse_id"))
        .first()
    )
    office_row = (
        db.query(Office.office_name)
        .filter(Office.office_id == share_row.get("office_id"))
        .first()
    )
    group_row = (
        db.query(Group.group_name)
        .filter(Group.group_id == share_row.get("group_id"))
        .first()
    )

    year = (
        int(schedule_row.year)
        if schedule_row and schedule_row.year is not None
        else None
    )
    month = (
        int(schedule_row.month)
        if schedule_row and schedule_row.month is not None
        else None
    )
    nurse_name = str(nurse_row.name) if nurse_row and nurse_row.name else "간호사"
    office_name = (
        str(office_row.office_name)
        if office_row and office_row.office_name
        else "오피스"
    )
    group_name = (
        str(group_row.group_name) if group_row and group_row.group_name else "그룹"
    )

    if year is not None and month is not None:
        og_title_text = f"{year}년 {month}월 {nurse_name}근무표"
        og_description_text = (
            f"{office_name} {group_name} {year} {month} 근무표를 확인하세요."
        )
    else:
        og_title_text = f"{nurse_name}근무표"
        og_description_text = f"{office_name} {group_name} 근무표를 확인하세요."

    og_title = html.escape(og_title_text)
    og_description = html.escape(og_description_text)
    fixed_meta_image_url = os.getenv(
        "SHARE_META_IMAGE_URL",
        "https://s3-aide-backend-share-dev.s3.ap-northeast-2.amazonaws.com/og-images/meta/fixed-share-preview.png",
    )
    og_image = html.escape(str(fixed_meta_image_url).strip())
    display_image = html.escape(
        f"{_share_public_base_url(str(request.base_url).rstrip('/') if request else '')}/roster/s/{token}/image"
    )

    html_body = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{og_title}</title>
  <meta property="og:type" content="website" />
  <meta property="og:title" content="{og_title}" />
  <meta property="og:description" content="{og_description}" />
  <meta property="og:image" content="{og_image}" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{og_title}" />
  <meta name="twitter:description" content="{og_description}" />
  <meta name="twitter:image" content="{og_image}" />
</head>
<body style="margin:0;background:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:8px;">
  <img src="{display_image}" alt="{og_title}" style="max-width:100vw;max-height:100vh;width:auto;height:auto;display:block;object-fit:contain;" />
</body>
</html>
"""
    return HTMLResponse(content=html_body, status_code=200)


@router.get("/s/{token}/image", name="render_schedule_share_image")
async def render_schedule_share_image(token: str, db: Session = Depends(get_db)):
    import io
    from services.roster_service import get_public_share_image_service

    try:
        image_bytes, content_type = get_public_share_image_service(db=db, token=token)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Share image load failed: {str(e)}"
        )

    return StreamingResponse(io.BytesIO(image_bytes), media_type=content_type)
