"""
근무표 관련 서비스 로직 모듈.

- DB 쿼리, 데이터 가공, 엔진 호출 등 라우터에서 분리합니다.
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일을 지향합니다.
"""

from datetime import date, datetime
import calendar
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import (
    RosterConfig as RosterConfigModel,
    Schedule,
    ShiftPreference,
    Nurse,
    ScheduleEntry,
    Shift,
    Group,
    RosterConfig,
    Wanted,
    IssuedRoster,
    ShiftManage,
    IssuedRosterSnapshot,
    WeeklyOffSetting,
    RosterGradeConfig,
)
from db.roster_config import NurseRosterConfig
from db.nurse_config import Nurse as NurseEngine
from schemas.roster_schema import RosterConfigCreate, PublishRequest, RosterRequest
from services.roster_system import RosterSystem
from services.shift_service_mssql import _to_time_str


def save_roster_config_service(
    config_data: RosterConfigCreate,
    user,
    db: Session,
    override_group_id: str | None = None,
):
    """
    근무표 설정 저장 서비스 함수.

    관리자(ADM) 사용자의 경우 `override_group_id`를 통해 저장 대상 그룹을 지정합니다.
    """
    try:
        # 1) 저장 대상 그룹/오피스 결정
        target_group_id: str
        target_office_id: str

        if override_group_id:
            group_row = (
                db.query(Group).filter(Group.group_id == override_group_id).first()
            )
            if not group_row:
                raise Exception("지정한 그룹을 찾을 수 없습니다.")
            target_group_id = group_row.group_id
            target_office_id = group_row.office_id
        else:
            nurse = db.query(Nurse).filter(Nurse.nurse_id == user.nurse_id).first()
            target_group_id = user.group_id
            target_office_id = nurse.office_id

        # 2) ShiftManage 기준으로 기본 일/저/야 요구 인원 계산
        shift_manages = (
            db.query(ShiftManage)
            .filter(
                ShiftManage.office_id == target_office_id,
                ShiftManage.group_id == target_group_id,
                ShiftManage.nurse_class == "RN",
            )
            .all()
        )
        day_req = eve_req = nig_req = 0
        if shift_manages:
            for sm in shift_manages:
                if sm.shift_slot == 1:
                    day_req = sm.manpower or 0
                elif sm.shift_slot == 2:
                    eve_req = sm.manpower or 0
                elif sm.shift_slot == 3:
                    nig_req = sm.manpower or 0
        else:
            day_req = eve_req = nig_req = 3

        # 3) 설정 저장
        config_dict = config_data.model_dump()
        use_mid = bool(config_dict.get("use_mid", False))
        config_dict.update({"day_req": day_req, "eve_req": eve_req, "nig_req": nig_req})
        db_config = RosterConfigModel(
            **config_dict,
            office_id=target_office_id,
            group_id=target_group_id,
        )
        print("db_config", db_config.__dict__)
        weekly_off_group = config_dict.get("weekly_off_group")
        db.query(WeeklyOffSetting).filter(
            WeeklyOffSetting.office_id == target_office_id,
            WeeklyOffSetting.group_id == target_group_id,
        ).update({"activate": 1 if weekly_off_group else 0})
        if weekly_off_group is not None:
            new_enabled = 1 if weekly_off_group else 0
            db.query(Nurse).filter(Nurse.group_id == target_group_id).update(
                {Nurse.weekly_off_enabled: new_enabled}, synchronize_session=False
            )

        if not use_mid:
            db.query(ShiftManage).filter(
                ShiftManage.office_id == target_office_id,
                ShiftManage.group_id == target_group_id,
                ShiftManage.nurse_class == "RN",
                ShiftManage.shift_slot == 5,
            ).update({ShiftManage.manpower: 0}, synchronize_session=False)

            nurses = db.query(Nurse).filter(Nurse.group_id == target_group_id).all()
            for nurse_row in nurses:
                raw_types = getattr(nurse_row, "is_night_nurse", None)
                if isinstance(raw_types, list) and raw_types:
                    nurse_row.is_night_nurse = [
                        t for t in raw_types if str(t).strip().upper() != "M"
                    ]

            grade_cfg = (
                db.query(RosterGradeConfig)
                .filter(
                    RosterGradeConfig.office_id == target_office_id,
                    RosterGradeConfig.group_id == target_group_id,
                )
                .first()
            )
            if grade_cfg and isinstance(grade_cfg.constraints_json, dict):
                cleaned = dict(grade_cfg.constraints_json)
                if "M" in cleaned:
                    cleaned.pop("M", None)
                    grade_cfg.constraints_json = cleaned

        db.add(db_config)
        db.commit()
        db.refresh(db_config)
        return {"message": "Configuration saved successfully"}
    except Exception as e:
        print(f"설정 저장 오류: {str(e)}")
        db.rollback()
        raise


def get_latest_schedule_service(
    current_user, db: Session, override_group_id: str | None = None
):
    """
    최신 스케줄 정보 조회 서비스 함수.

    관리자(ADM)는 `override_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    if not (
        getattr(current_user, "is_head_nurse", False)
        or getattr(current_user, "is_master_admin", False)
    ):
        raise Exception("Permission denied")

    target_group_id = override_group_id or current_user.group_id
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")

    latest_schedule = (
        db.query(Schedule)
        .filter(Schedule.group_id == target_group_id, Schedule.dropped == False)
        .order_by(Schedule.year.desc(), Schedule.month.desc(), Schedule.version.desc())
        .first()
    )
    if not latest_schedule:
        return None
    return {
        "year": latest_schedule.year,
        "month": latest_schedule.month,
        "version": latest_schedule.version,
        "status": latest_schedule.status,
        "schedule_id": latest_schedule.schedule_id,
    }


def get_issued_schedules_service(
    current_user, db: Session, target_group_id: str | None = None
):
    """
    발행된(issued) 모든 스케줄 정보 조회 서비스 함수.

    관리자(ADM)는 `target_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    # if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
    #     raise Exception("Permission denied")

    try:
        schedules_query = (
            db.query(Schedule.schedule_id, Schedule.year, Schedule.month)
            .filter(
                Schedule.group_id == target_group_id,
                Schedule.status == "issued",
                Schedule.dropped == False,
            )
            .distinct()
            .order_by(Schedule.year.desc(), Schedule.month.desc())
            .all()
        )
        schedules = [
            {"year": r.year, "month": r.month, "schedule_id": r.schedule_id}
            for r in schedules_query
        ]
    except Exception as e:
        print("[get_issued_schedules_service] error", e)
        print("[get_issued_schedules_service] target_group_id", target_group_id)
        raise HTTPException(
            status_code=500, detail=f"Failed to get issued schedules: {str(e)}"
        )
    return schedules


def get_schedule_status_service(
    year: int,
    month: int,
    current_user,
    db: Session,
    override_group_id: str | None = None,
):
    """
    특정 월의 스케줄 상태 조회 서비스 함수.

    관리자(ADM)는 `override_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")

    # HN/ADM 그룹 요약
    if getattr(current_user, "is_head_nurse", False) or getattr(
        current_user, "is_master_admin", False
    ):
        target_group_id = override_group_id or current_user.group_id
        if not target_group_id:
            raise Exception("대상 그룹이 없습니다.")
        schedules = (
            db.query(Schedule)
            .filter(
                Schedule.group_id == target_group_id,
                Schedule.year == year,
                Schedule.month == month,
                Schedule.dropped == False,
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

    # 일반 간호사 개인 선호도/상태
    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == current_user.group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,
        )
        .order_by(Schedule.version.desc())
        .first()
    )
    submitted_preference = (
        db.query(ShiftPreference)
        .filter(
            ShiftPreference.office_id == current_user.office_id,
            ShiftPreference.group_id == current_user.group_id,
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
    draft_preference = (
        db.query(ShiftPreference)
        .filter(
            ShiftPreference.office_id == current_user.office_id,
            ShiftPreference.group_id == current_user.group_id,
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
    return {
        "schedule_status": schedule.status if schedule else None,
        "preference_is_submitted": False,
        "preference_data": None,
        "has_schedules": schedule is not None,
        "created_at": None,
        "submitted_at": None,
    }


def get_issued_roster_snapshot_service(
    year: int,
    month: int,
    current_user,
    db: Session,
    target_group_id: str | None = None,
) -> dict | None:
    """
    특정 연월에 대해 활성 발행본(is_active_issued=True)의 근무표 스냅샷을 조회합니다.

    관리자(ADM)는 `target_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")

    if not target_group_id:
        target_group_id = getattr(current_user, "group_id", None)
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")

    # office_id 결정: 토큰의 office_id 우선, 없으면 그룹 조회
    office_id = getattr(current_user, "office_id", None)
    if not office_id:
        group_row = db.query(Group).filter(Group.group_id == target_group_id).first()
        if not group_row:
            raise Exception("그룹 정보를 찾을 수 없습니다.")
        office_id = group_row.office_id

    # 오피스/그룹 기준 활성 스냅샷 조회 후, year/month는 meta_json으로 필터링
    snapshots = (
        db.query(IssuedRosterSnapshot)
        .filter(
            IssuedRosterSnapshot.office_id == office_id,
            IssuedRosterSnapshot.group_id == target_group_id,
            IssuedRosterSnapshot.is_active_issued == True,
        )
        .order_by(IssuedRosterSnapshot.created_at.desc())
        .all()
    )

    matched_snapshot: IssuedRosterSnapshot | None = None
    for snap in snapshots:
        meta = snap.meta_json or {}
        if meta.get("year") == year and meta.get("month") == month:
            matched_snapshot = snap
            break

    if not matched_snapshot:
        return None

    return {
        "snapshot_id": matched_snapshot.snapshot_id,
        "office_id": matched_snapshot.office_id,
        "group_id": matched_snapshot.group_id,
        "schedule_id": matched_snapshot.schedule_id,
        "version": matched_snapshot.version,
        "created_at": matched_snapshot.created_at,
        "is_active_issued": matched_snapshot.is_active_issued,
        "meta": matched_snapshot.meta_json or {},
        "config": matched_snapshot.config_json or {},
        "nurses": matched_snapshot.nurses_json or [],
        "shifts": matched_snapshot.shifts_json or [],
        "shift_manage": matched_snapshot.shift_manage_json or [],
        "roster": matched_snapshot.roster_json or {},
        "violations": matched_snapshot.violations_json
        or {"messages": [], "details": []},
    }


def create_issued_roster_snapshot(
    schedule: Schedule,
    current_user,
    year: int,
    month: int,
    office_id: str,
    group_id: str,
    db: Session,
) -> IssuedRosterSnapshot:
    """
    근무표 발행 시점의 스냅샷 레코드를 생성합니다.

    DB 세션에는 추가만 수행하고, 커밋은 호출자가 직접 처리하도록 합니다.
    """
    # 동일 그룹/연월의 기존 발행 스냅샷 is_active_issued 플래그 비활성화
    (
        db.query(IssuedRosterSnapshot)
        .filter(
            IssuedRosterSnapshot.office_id == office_id,
            IssuedRosterSnapshot.group_id == group_id,
            IssuedRosterSnapshot.year == schedule.year,
            IssuedRosterSnapshot.month == schedule.month,
            IssuedRosterSnapshot.is_active_issued == True,
        )
        .update(
            {"is_active_issued": False},
            synchronize_session=False,
        )
    )
    # 메타 정보 구성
    meta_json: dict = {
        "office_id": office_id,
        "group_id": group_id,
        "schedule_id": schedule.schedule_id,
        "year": schedule.year,
        "month": schedule.month,
        "version": schedule.version,
        "schedule_name": schedule.name,
        "memo": schedule.memo,
        "issued_by_nurse_id": getattr(current_user, "nurse_id", None),
        "issued_by_account_id": getattr(current_user, "account_id", None),
    }

    # 설정 스냅샷 구성 (RosterConfig)
    config_json = None
    if schedule.config_id:
        cfg = (
            db.query(RosterConfigModel)
            .filter(RosterConfigModel.config_id == schedule.config_id)
            .first()
        )
        if cfg:
            config_json = {
                "config_id": cfg.config_id,
                "config_version": cfg.config_version,
                "office_id": cfg.office_id,
                "group_id": cfg.group_id,
                "day_req": cfg.day_req,
                "eve_req": cfg.eve_req,
                "nig_req": cfg.nig_req,
                "min_exp_per_shift": cfg.min_exp_per_shift,
                "req_exp_nurses": cfg.req_exp_nurses,
                "two_offs_per_week": cfg.two_offs_per_week,
                "max_nig_per_month": cfg.max_nig_per_month,
                "three_seq_nig": cfg.three_seq_nig,
                "two_offs_after_three_nig": cfg.two_offs_after_three_nig,
                "two_offs_after_two_nig": cfg.two_offs_after_two_nig,
                "banned_day_after_eve": cfg.banned_day_after_eve,
                "max_conseq_work": cfg.max_conseq_work,
                "off_days": cfg.off_days,
                "shift_priority": cfg.shift_priority,
                "weekend_shift_ratio": cfg.weekend_shift_ratio,
                "patient_amount": cfg.patient_amount,
                "sequential_offs": cfg.sequential_offs,
                "even_nights": cfg.even_nights,
                "nod_noe": cfg.nod_noe,
                "preceptor_gauge": cfg.preceptor_gauge,
                "preceptee_on": cfg.preceptee_on,
                "preceptee_shift_count": cfg.preceptee_shift_count,
                "created_at": cfg.created_at.isoformat()
                if getattr(cfg, "created_at", None)
                else None,
            }

    # 간호사 리스트 및 정보 스냅샷
    nurses = (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id)
        .order_by(Nurse.experience.desc(), Nurse.nurse_id.asc())
        .all()
    )
    nurses_json = []
    for n in nurses:
        nurses_json.append(
            {
                "nurse_id": n.nurse_id,
                "group_id": n.group_id,
                "office_id": n.office_id,
                "account_id": n.account_id,
                "emp_num": n.emp_num,
                "name": n.name,
                "experience": n.experience,
                "role": n.role,
                "level_": n.level_,
                "is_head_nurse": n.is_head_nurse,
                "emp_auth_gbn": n.emp_auth_gbn,
                "is_night_nurse": n.is_night_nurse,
                "personal_off_adjustment": n.personal_off_adjustment,
                "preceptor_id": n.preceptor_id,
                "joining_date": n.joining_date.isoformat()
                if getattr(n, "joining_date", None)
                else None,
                "resignation_date": n.resignation_date.isoformat()
                if getattr(n, "resignation_date", None)
                else None,
                "sequence": n.sequence,
                "active": n.active,
                "team_id": n.team_id,
            }
        )

    # 근무표(로스터) 스냅샷
    days_in_month = calendar.monthrange(schedule.year, schedule.month)[1]

    # 시프트 메타데이터 전체 스냅샷
    shift_rows = db.query(Shift).filter(Shift.group_id == group_id).all()
    shifts_json = [
        {
            "shift_id": s.shift_id,
            "office_id": s.office_id,
            "group_id": s.group_id,
            "name": s.name,
            "color": s.color,
            "start_time": _to_time_str(s.start_time),
            "end_time": _to_time_str(s.end_time),
            "type": s.type,
            "allday": s.allday,
            "auto_schedule": s.auto_schedule,
            "duration": s.duration,
            "sequence": s.sequence,
            "default_shift": s.default_shift,
            "id": s.id,
        }
        for s in shift_rows
    ]
    shift_colors = {s.shift_id: s.color for s in shift_rows}

    entries = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == schedule.schedule_id)
        .all()
    )
    entries_by_nurse: dict = {}
    for entry in entries:
        nurse_id = entry.nurse_id
        day = entry.work_date.day
        if nurse_id not in entries_by_nurse:
            entries_by_nurse[nurse_id] = {}
        entries_by_nurse[nurse_id][day] = entry.shift_id

    roster_nurses = []
    for n in nurses:
        schedule_list = [
            entries_by_nurse.get(n.nurse_id, {}).get(day, "-")
            for day in range(1, days_in_month + 1)
        ]
        counts = {
            shift_id: schedule_list.count(shift_id) for shift_id in shift_colors.keys()
        }
        roster_nurses.append(
            {
                "nurse_id": n.nurse_id,
                "name": n.name,
                "experience": n.experience,
                "schedule": schedule_list,
                "counts": counts,
            }
        )

    roster_json = {
        "year": schedule.year,
        "month": schedule.month,
        "days_in_month": days_in_month,
        "shift_colors": shift_colors,
        "nurses": roster_nurses,
    }

    # 시프트 관리(ShiftManage) 스냅샷 - RN 포함 전체 클래스 저장
    shift_manage_rows = (
        db.query(ShiftManage)
        .filter(
            ShiftManage.office_id == office_id,
            ShiftManage.group_id == group_id,
        )
        .order_by(ShiftManage.nurse_class.asc(), ShiftManage.shift_slot.asc())
        .all()
    )
    shift_manage_json = [
        {
            "nurse_class": sm.nurse_class,
            "shift_slot": sm.shift_slot,
            "main_code": sm.main_code,
            "codes": sm.codes if sm.codes else [],
            "manpower": sm.manpower,
        }
        for sm in shift_manage_rows
    ]

    # 위반사항은 우선 빈 구조로 저장하고, 이후 검증 로직 연동 시 확장합니다.
    violations_json: dict = {
        "messages": [],
        "details": [],
    }

    snapshot = IssuedRosterSnapshot(
        office_id=office_id,
        group_id=group_id,
        schedule_id=schedule.schedule_id,
        version=schedule.version,
        is_active_issued=True,
        meta_json=meta_json,
        config_json=config_json,
        nurses_json=nurses_json,
        shifts_json=shifts_json,
        shift_manage_json=shift_manage_json,
        roster_json=roster_json,
        violations_json=violations_json,
        year=schedule.year,
        month=schedule.month,
    )
    return snapshot
