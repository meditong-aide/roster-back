"""
근무표 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공, 엔진 호출 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
from sqlalchemy.orm import Session
from db.models import RosterConfig as RosterConfigModel, Schedule, ShiftPreference, Nurse, ScheduleEntry, Shift, Group, RosterConfig, Wanted, IssuedRoster, ShiftManage
from schemas.roster_schema import RosterConfigCreate, PublishRequest, RosterRequest
from db.roster_config import NurseRosterConfig
from db.nurse_config import Nurse as NurseEngine
from services.roster_system import RosterSystem
from datetime import date, datetime
from sqlalchemy import func
import uuid


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
            print(1)
            group_row = db.query(Group).filter(Group.group_id == override_group_id).first()
            if not group_row:
                raise Exception("지정한 그룹을 찾을 수 없습니다.")
            target_group_id = group_row.group_id
            target_office_id = group_row.office_id
        else:
            
            nurse = db.query(Nurse).filter(Nurse.nurse_id == user.nurse_id).first()

            # if not nurse or not nurse.group:
            #     raise Exception("User group information not found")
            print('user', user)
            print('nurse', nurse)
            target_group_id = user.group_id
            target_office_id = nurse.group.office_id

        # 2) ShiftManage 기준으로 기본 일/저/야 요구 인원 계산
        shift_manages = db.query(ShiftManage).filter(
            ShiftManage.office_id == target_office_id,
            ShiftManage.group_id == target_group_id,
            ShiftManage.nurse_class == 'RN',
        ).all()
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
        config_dict.update({
            'day_req': day_req,
            'eve_req': eve_req,
            'nig_req': nig_req
        })

        db_config = RosterConfigModel(
            **config_dict,
            office_id=target_office_id,
            group_id=target_group_id,
        )
        db.add(db_config)
        db.commit()
        db.refresh(db_config)
        return {"message": "Configuration saved successfully"}
    except Exception as e:
        print(f'설정 저장 오류: {str(e)}')
        db.rollback()
        raise

def get_latest_schedule_service(current_user, db: Session, override_group_id: str | None = None):
    """
    최신 스케줄 정보 조회 서비스 함수.

    관리자(ADM)는 `override_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
        raise Exception("Permission denied")

    target_group_id = override_group_id or current_user.group_id
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")

    latest_schedule = db.query(Schedule).filter(
        Schedule.group_id == target_group_id,
        Schedule.dropped == False
    ).order_by(
        Schedule.year.desc(),
        Schedule.month.desc(),
        Schedule.version.desc()
    ).first()
    if not latest_schedule:
        return None
    return {
        "year": latest_schedule.year,
        "month": latest_schedule.month,
        "version": latest_schedule.version,
        "status": latest_schedule.status,
        "schedule_id": latest_schedule.schedule_id
    }

def get_issued_schedules_service(current_user, db: Session, override_group_id: str | None = None):
    """
    발행된(issued) 모든 스케줄 정보 조회 서비스 함수.

    관리자(ADM)는 `override_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    # if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
    #     raise Exception("Permission denied")

    target_group_id =  current_user.group_id
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")

    schedules_query = db.query(Schedule.schedule_id, Schedule.year, Schedule.month).filter(
        Schedule.group_id == target_group_id,
        Schedule.status == 'issued',
        Schedule.dropped == False
    ).distinct().order_by(Schedule.year.desc(), Schedule.month.desc()).all()
    schedules = [{"year": r.year, "month": r.month, "schedule_id": r.schedule_id} for r in schedules_query]
    return schedules

def get_schedule_status_service(year: int, month: int, current_user, db: Session, override_group_id: str | None = None):
    """
    특정 월의 스케줄 상태 조회 서비스 함수.

    관리자(ADM)는 `override_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")

    # HN/ADM 그룹 요약
    if getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False):
        target_group_id = override_group_id or current_user.group_id
        if not target_group_id:
            raise Exception("대상 그룹이 없습니다.")
        schedules = db.query(Schedule).filter(
            Schedule.group_id == target_group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False
        ).all()
        has_schedules = len(schedules) > 0
        latest_status = schedules[0].status if schedules else None
        return {
            "has_schedules": has_schedules,
            "latest_status": latest_status,
            "schedule_count": len(schedules)
        }

    # 일반 간호사 개인 선호도/상태
    schedule = db.query(Schedule).filter(
        Schedule.group_id == current_user.group_id,
        Schedule.year == year,
        Schedule.month == month,
        Schedule.dropped == False
    ).order_by(Schedule.version.desc()).first()
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
    return {
        "schedule_status": schedule.status if schedule else None,
        "preference_is_submitted": False,
        "preference_data": None,
        "has_schedules": schedule is not None,
        "created_at": None,
        "submitted_at": None
    }

# ... (다른 서비스 함수도 동일하게 분리하여 추가 예정) ... 