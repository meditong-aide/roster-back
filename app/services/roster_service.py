"""
근무표 관련 서비스 로직 모듈.

- DB 쿼리, 데이터 가공, 엔진 호출 등 라우터에서 분리합니다.
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일을 지향합니다.
"""

from datetime import date, datetime
import calendar
import uuid

from fastapi import HTTPException
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
from routers.utils import get_days_in_month
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
            group_row = db.query(Group).filter(Group.group_id == override_group_id).first()
            if not group_row:
                raise Exception("지정한 그룹을 찾을 수 없습니다.")
            target_group_id = group_row.group_id
            target_office_id = group_row.office_id
        else:
            
            nurse = db.query(Nurse).filter(Nurse.nurse_id == user.nurse_id).first()
            target_group_id = user.group_id
            target_office_id = nurse.office_id

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
        use_mid = bool(config_dict.get('use_mid', False))
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
        print('db_config', db_config.__dict__)
        weekly_off_group = config_dict.get('weekly_off_group')
        db.query(WeeklyOffSetting).filter(
            WeeklyOffSetting.office_id == target_office_id,
            WeeklyOffSetting.group_id == target_group_id,
        ).update(
            {
                'activate': 1 if weekly_off_group else 0
            }
        )
        if weekly_off_group is not None:
            new_enabled = 1 if weekly_off_group else 0
            db.query(Nurse).filter(
                Nurse.group_id == target_group_id
            ).update(
                {Nurse.weekly_off_enabled: new_enabled},
                synchronize_session=False
            )

        if use_mid:
            # use_mid=True: grade_config 에 M 키 없으면 자동 추가 (각 grade 0명)
            grade_cfg = db.query(RosterGradeConfig).filter(
                RosterGradeConfig.office_id == target_office_id,
                RosterGradeConfig.group_id == target_group_id,
            ).first()
            if grade_cfg and isinstance(grade_cfg.constraints_json, dict):
                cj = dict(grade_cfg.constraints_json)
                if 'M' not in cj and cj:
                    # 기존 D 키에서 grade 번호 추출해서 M 기본값 생성
                    sample = cj.get('D') or cj.get('E') or cj.get('N') or {}
                    cj['M'] = {g: 0 for g in sample}
                    grade_cfg.constraints_json = cj

        if not use_mid:
            db.query(ShiftManage).filter(
                ShiftManage.office_id == target_office_id,
                ShiftManage.group_id == target_group_id,
                ShiftManage.nurse_class == 'RN',
                ShiftManage.shift_slot == 5,
            ).update({ShiftManage.manpower: 0}, synchronize_session=False)

            nurses = db.query(Nurse).filter(Nurse.group_id == target_group_id).all()
            for nurse_row in nurses:
                raw_types = getattr(nurse_row, 'is_night_nurse', None)
                if isinstance(raw_types, list) and raw_types:
                    nurse_row.is_night_nurse = [
                        t for t in raw_types if str(t).strip().upper() != 'M'
                    ]

            grade_cfg = db.query(RosterGradeConfig).filter(
                RosterGradeConfig.office_id == target_office_id,
                RosterGradeConfig.group_id == target_group_id,
            ).first()
            if grade_cfg and isinstance(grade_cfg.constraints_json, dict):
                cleaned = dict(grade_cfg.constraints_json)
                if 'M' in cleaned:
                    cleaned.pop('M', None)
                    grade_cfg.constraints_json = cleaned

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

def get_issued_schedules_service(current_user, db: Session, target_group_id: str | None = None):
    """
    발행된(issued) 모든 스케줄 정보 조회 서비스 함수.

    관리자(ADM)는 `target_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    # if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
    #     raise Exception("Permission denied")

 
    try:
        schedules_query = db.query(Schedule.schedule_id, Schedule.year, Schedule.month).filter(
            Schedule.group_id == target_group_id,
            Schedule.status == 'issued',
            Schedule.dropped == False
        ).distinct().order_by(Schedule.year.desc(), Schedule.month.desc()).all()
        schedules = [{"year": r.year, "month": r.month, "schedule_id": r.schedule_id} for r in schedules_query]
    except Exception as e:
        print('[get_issued_schedules_service] error', e)
        print('[get_issued_schedules_service] target_group_id', target_group_id)
        raise HTTPException(status_code=500, detail=f"Failed to get issued schedules: {str(e)}")
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


def get_prev_month_tail_service(
    year: int,
    month: int,
    schedule_id: str | None,
    tail_days: int,
    group_id: str | None,
    current_user,
    db: Session,
):
    if current_user.is_head_nurse and current_user.group_id:
        target_group_id = current_user.group_id
    else:
        if not group_id:
            raise HTTPException(status_code=400, detail="group_id is required for admin")
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

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    ref_schedule_id = schedule_id
    if not ref_schedule_id:
        cur_issued = (
            db.query(Schedule.schedule_id)
            .filter(
                Schedule.group_id == target_group_id,
                Schedule.year == year,
                Schedule.month == month,
                Schedule.status == "issued",
                Schedule.dropped == False,
            )
            .scalar()
        )
        ref_schedule_id = cur_issued

    nurses = (
        db.query(Nurse.nurse_id, Nurse.name, Nurse.sequence)
        .filter(
            Nurse.group_id == target_group_id,
            Nurse.active == 1,
        )
        .order_by(Nurse.sequence.asc(), Nurse.nurse_id.asc())
        .all()
    )

    prev_schedule = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == target_group_id,
            Schedule.year == prev_year,
            Schedule.month == prev_month,
            Schedule.status == "issued",
            Schedule.dropped == False,
        )
        .first()
    )

    if not prev_schedule:
        prev_schedule = (
            db.query(Schedule)
            .filter(
                Schedule.group_id == target_group_id,
                Schedule.year == prev_year,
                Schedule.month == prev_month,
                Schedule.dropped == False,
            )
            .order_by(Schedule.created_at.desc())
            .first()
        )

    if not prev_schedule:
        return {"prev_year": prev_year, "prev_month": prev_month, "data": None}

    days_in_prev_month = get_days_in_month(prev_year, prev_month)
    tail_day_list = list(
        range(max(1, days_in_prev_month - tail_days + 1), days_in_prev_month + 1)
    )

    start_date = date(prev_year, prev_month, tail_day_list[0])
    end_date = date(prev_year, prev_month, tail_day_list[-1])

    entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == prev_schedule.schedule_id,
            ScheduleEntry.work_date >= start_date,
            ScheduleEntry.work_date <= end_date,
        )
        .all()
    )

    entries_by_nurse = {}
    for entry in entries:
        nid = entry.nurse_id
        if nid not in entries_by_nurse:
            entries_by_nurse[nid] = {}
        entries_by_nurse[nid][entry.work_date.day] = entry.shift_id

    # 전월 assignment 치환 (파견/병동이동/휴직)
    from services.assignment_service import get_roster_assignments
    _prev_assignments = get_roster_assignments(
        db, group_id=target_group_id, year=prev_year, month=prev_month,
    )

    nurse_list = []
    for nurse in nurses:
        shifts = {
            str(d): entries_by_nurse.get(nurse.nurse_id, {}).get(d)
            for d in tail_day_list
        }
        # assignment 기간의 shift에 reason 필드 추가
        _a_list = _prev_assignments.get(nurse.nurse_id, [])
        for _a in _a_list:
            _a_start = date.fromisoformat(_a["start_date"]) if isinstance(_a["start_date"], str) else _a["start_date"]
            _a_end = date.fromisoformat(_a["end_date"]) if _a.get("end_date") else None
            for d in tail_day_list:
                _cell_date = date(prev_year, prev_month, d)
                if _cell_date >= _a_start and (not _a_end or _cell_date <= _a_end):
                    shifts[str(d)] = None
        nurse_list.append(
            {
                "nurse_id": nurse.nurse_id,
                "name": nurse.name,
                "shifts": shifts,
            }
        )

    return {
        "prev_year": prev_year,
        "prev_month": prev_month,
        "data": {
            "schedule_id": prev_schedule.schedule_id,
            "schedule_name": prev_schedule.name,
            "schedule_status": prev_schedule.status,
            "tail_days": tail_day_list,
            "nurses": nurse_list,
        },
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


def get_my_issued_roster_service(
    year: int,
    month: int,
    current_user,
    db: Session,
) -> dict | None:
    """
    로그인 사용자 본인의 발행된 근무표만 조회합니다.
    snapshot의 roster_json에서 nurse_id 기준으로 추출.
    """
    snapshot_data = get_issued_roster_snapshot_service(
        year=year, month=month, current_user=current_user, db=db
    )
    if not snapshot_data:
        return None

    roster = snapshot_data.get("roster") or {}
    nurse_id = getattr(current_user, "nurse_id", None)
    if not nurse_id:
        return None

    roster_nurses = roster.get("nurses") or []
    my_roster = next(
        (n for n in roster_nurses if str(n.get("nurse_id")) == str(nurse_id)),
        None,
    )
    if not my_roster:
        return None

    result = {
        "year": roster.get("year"),
        "month": roster.get("month"),
        "nurse_id": my_roster.get("nurse_id"),
        "name": my_roster.get("name"),
        "schedule": my_roster.get("schedule"),
        "schedule_ids": my_roster.get("schedule_ids"),
        "counts": my_roster.get("counts"),
        "shift_colors": roster.get("shift_colors") or {},
        "issued_at": snapshot_data.get("created_at"),
    }

    # 파견/병동이동 간호사: target group 근무표 병합
    from services.assignment_service import get_active_assignments_for_month
    from calendar import monthrange
    assignments = get_active_assignments_for_month(
        db, current_user.group_id, year, month,
    )
    my_transfer = next(
        (a for a in assignments
         if a.nurse_id == nurse_id
         and a.reason in ("파견", "병동이동")
         and a.source_group_id == current_user.group_id
         and a.target_group_id),
        None,
    )
    if my_transfer:
        from calendar import monthrange
        from datetime import date
        days_in_month = monthrange(year, month)[1]
        a_start = my_transfer.start_date
        a_end = my_transfer.end_date or my_transfer.expected_end_date
        m_start = date(year, month, 1)
        m_end = date(year, month, days_in_month)
        period_start = max(a_start, m_start).day
        period_end = min(a_end, m_end).day if a_end else days_in_month

        from db.models import Group
        tgt_group = db.query(Group).filter(
            Group.group_id == my_transfer.target_group_id
        ).first()

        target_snapshot = get_issued_roster_snapshot_service(
            year=year, month=month, current_user=current_user, db=db,
            target_group_id=my_transfer.target_group_id,
        )
        target_issued = False
        src_schedule = result["schedule"] or []
        src_ids = result["schedule_ids"] or []

        if target_snapshot:
            t_roster = target_snapshot.get("roster") or {}
            t_nurses = t_roster.get("nurses") or []
            t_my = next(
                (n for n in t_nurses if str(n.get("nurse_id")) == str(nurse_id)),
                None,
            )
            if t_my:
                target_issued = True
                tgt_schedule = t_my.get("schedule") or []
                tgt_ids = t_my.get("schedule_ids") or []

                for d in range(period_start, period_end + 1):
                    idx = d - 1
                    if idx < len(tgt_schedule):
                        if idx < len(src_schedule):
                            src_schedule[idx] = tgt_schedule[idx]
                        if idx < len(src_ids) and idx < len(tgt_ids):
                            src_ids[idx] = tgt_ids[idx]

                # shift_colors 병합
                t_colors = t_roster.get("shift_colors") or {}
                merged_colors = {**result["shift_colors"], **t_colors}
                result["shift_colors"] = merged_colors

        if not target_issued:
            # target 미마감: 기존 셀에 reason 필드 추가
            _reason = my_transfer.reason  # "파견" / "병동이동"
            for d in range(period_start, period_end + 1):
                idx = d - 1
                if idx < len(src_schedule):
                    _cell = src_schedule[idx]
                    if isinstance(_cell, dict):
                        _cell["reason"] = _reason
                    else:
                        src_schedule[idx] = {"code": _cell, "reason": _reason}
                if idx < len(src_ids):
                    src_ids[idx] = None

        result["schedule"] = src_schedule
        result["schedule_ids"] = src_ids
        # counts 재계산
        result["counts"] = {}
        for code in (result["shift_colors"] or {}):
            result["counts"][code] = src_schedule.count(code)

        # transfer 정보 항상 첨부 (미마감 상태 포함)
        result["transfer"] = {
            "reason": my_transfer.reason,
            "target_group_id": my_transfer.target_group_id,
            "target_group_name": tgt_group.group_name if tgt_group else "",
            "start_date": str(a_start),
            "end_date": str(a_end) if a_end else None,
            "period_start_day": period_start,
            "period_end_day": period_end,
            "target_issued": target_issued,
        }

    return result


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

    # 간호사 리스트 및 정보 스냅샷 (인바운드 포함)
    nurses = list(
        db.query(Nurse)
        .filter(Nurse.group_id == group_id)
        .order_by(Nurse.experience.desc(), Nurse.nurse_id.asc())
        .all()
    )
    # 인바운드 간호사: schedule_entries에 존재하지만 group에 없는 간호사
    _group_nids = {n.nurse_id for n in nurses}
    _entry_nids = {
        row.nurse_id for row in
        db.query(ScheduleEntry.nurse_id)
        .filter(ScheduleEntry.schedule_id == schedule.schedule_id)
        .distinct()
        .all()
    }
    _inbound_nids = _entry_nids - _group_nids
    if _inbound_nids:
        _inbound_nurses = (
            db.query(Nurse)
            .filter(Nurse.nurse_id.in_(_inbound_nids))
            .all()
        )
        nurses.extend(_inbound_nurses)
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
    # shifts.id → 현재 shift_id 매핑 (schedule_entries의 shift_id가 구 코드일 수 있으므로)
    _int_id_to_shift_id: dict[int, str] = {
        s.id: s.shift_id for s in shift_rows
    }

    entries_by_nurse: dict = {}
    entry_ids_by_nurse: dict = {}
    for entry in entries:
        nurse_id = entry.nurse_id
        day = entry.work_date.day
        if nurse_id not in entries_by_nurse:
            entries_by_nurse[nurse_id] = {}
            entry_ids_by_nurse[nurse_id] = {}
        # entry.id(shifts.id)가 있으면 현재 shift_id로 복원, 없으면 기존 값 사용
        if entry.id and entry.id in _int_id_to_shift_id:
            entries_by_nurse[nurse_id][day] = _int_id_to_shift_id[entry.id]
        else:
            entries_by_nurse[nurse_id][day] = entry.shift_id
        entry_ids_by_nurse[nurse_id][day] = entry.id

    roster_nurses = []
    for n in nurses:
        schedule_list = []
        for day in range(1, days_in_month + 1):
            code = entries_by_nurse.get(n.nurse_id, {}).get(day, "-")
            schedule_list.append({
                "code": code,
                "color": shift_colors.get(code, ""),
            })
        schedule_ids = [
            entry_ids_by_nurse.get(n.nurse_id, {}).get(day)
            for day in range(1, days_in_month + 1)
        ]
        counts = {
            shift_id: sum(1 for item in schedule_list if item["code"] == shift_id)
            for shift_id in shift_colors.keys()
        }
        roster_nurses.append(
            {
                "nurse_id": n.nurse_id,
                "name": n.name,
                "experience": n.experience,
                "schedule": schedule_list,
                "schedule_ids": schedule_ids,
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



def _share_now() -> datetime:
    return datetime.now()



def _share_build_s3_client(region: str):
    import os
    import boto3

    access_key = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_KEY")
    session_token = os.getenv("AWS_SESSION_TOKEN")
    profile_name = os.getenv("AWS_PROFILE")

    if access_key and secret_key:
        return boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )

    if profile_name:
        session = boto3.Session(profile_name=profile_name, region_name=region)
        creds = session.get_credentials()
        if not creds or not creds.access_key or not creds.secret_key:
            raise ValueError("AWS credentials not found for AWS_PROFILE")
        return session.client("s3")

    session = boto3.Session(region_name=region)
    creds = session.get_credentials()
    if not creds or not creds.access_key or not creds.secret_key:
        raise ValueError("AWS credentials are missing. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or AWS_PROFILE")
    return session.client("s3")


def _share_public_base_url(fallback_base_url: str) -> str:
    import os

    configured = os.getenv("SHARE_PUBLIC_BASE_URL")
    if configured and str(configured).strip():
        return str(configured).strip().rstrip("/")
    return (fallback_base_url or "").rstrip("/")


def _share_fetch_s3_image_bytes(image_url: str) -> tuple[bytes, str]:
    import os
    from urllib.parse import urlparse

    parsed = urlparse(str(image_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("invalid image_url")

    object_key = parsed.path.lstrip("/")
    if not object_key:
        raise ValueError("invalid image_url path")

    region = os.getenv("AWS_REGION", "ap-northeast-2")
    bucket_name = parsed.netloc.split(".s3.")[0] if ".s3." in parsed.netloc else os.getenv("AWS_SHARE_S3_BUCKET") or os.getenv("SHARE_S3_BUCKET") or os.getenv("S3_SHARE_BUCKET")
    if not bucket_name:
        raise ValueError("share S3 bucket env is not configured")

    s3_client = _share_build_s3_client(region)
    obj = s3_client.get_object(Bucket=bucket_name, Key=object_key)
    content_type = str(obj.get("ContentType") or "image/png")
    image_bytes = obj["Body"].read()
    return image_bytes, content_type


def _share_resolve_target_scope(current_user, db: Session, override_group_id: str | None = None):
    is_master_admin = bool(getattr(current_user, "is_master_admin", False))
    if override_group_id:
        if not is_master_admin:
            raise PermissionError("Only admin can specify group_id")
        group_row = db.query(Group).filter(Group.group_id == override_group_id).first()
        if not group_row:
            raise LookupError("Group not found")
        if getattr(current_user, "office_id", None) and group_row.office_id != current_user.office_id:
            raise PermissionError("Group does not belong to your office")
        return group_row.group_id, group_row.office_id
    group_id = getattr(current_user, "group_id", None)
    office_id = getattr(current_user, "office_id", None)
    if not group_id or not office_id:
        raise ValueError("group_id or office_id is missing on current user")
    return group_id, office_id




def _share_build_object_prefix(office_id: str, group_id: str, nurse_id: str | None, year: int, month: int) -> str:
    safe_office_id = str(office_id or "unknown")
    safe_group_id = str(group_id or "unknown")
    safe_nurse_id = str(nurse_id or "unknown")
    return f"og-images/{safe_office_id}/{safe_group_id}/{safe_nurse_id}/{int(year):04d}/{int(month):02d}"


def _share_find_by_token(db: Session, token: str) -> dict | None:
    from db.models import ShareLink

    row = db.query(ShareLink).filter(ShareLink.token == token).first()
    if not row:
        return None
    return {
        "token": row.token,
        "schedule_id": row.schedule_id,
        "office_id": row.office_id,
        "group_id": row.group_id,
        "image_url": row.image_url,
        "title": row.title,
        "description": row.description,
        "created_by_nurse_id": row.created_by_nurse_id,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "created_at": row.created_at,
    }


def create_schedule_share_link_service(
    db: Session,
    current_user,
    schedule_id: str,
    fallback_base_url: str,
    image_url: str,
    title: str | None,
    description: str | None,
    expires_in_days: int,
    override_group_id: str | None = None,
) -> dict:
    import secrets
    from datetime import timedelta

    target_group_id, target_office_id = _share_resolve_target_scope(
        current_user=current_user,
        db=db,
        override_group_id=override_group_id,
    )

    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.office_id == target_office_id,
    ).first()
    if not schedule:
        raise LookupError("Schedule not found for your scope")

    if not image_url or not str(image_url).strip():
        raise ValueError("image_url is required")
    image_url = str(image_url).strip()

    token = secrets.token_hex(24)
    try:
        expires_days = max(1, min(int(expires_in_days), 365))
    except (TypeError, ValueError):
        raise ValueError("expires_in_days must be integer")
    expires_at = _share_now() + timedelta(days=expires_days)
    now = _share_now()

    from db.models import ShareLink

    share_row = ShareLink(
        token=token,
        schedule_id=schedule_id,
        office_id=target_office_id,
        group_id=target_group_id,
        image_url=image_url,
        title=title,
        description=description,
        created_by_nurse_id=getattr(current_user, "nurse_id", None),
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    db.add(share_row)
    db.commit()

    base_url = _share_public_base_url(fallback_base_url)
    return {
        "token": token,
        "share_url": f"{base_url}/roster/s/{token}",
        "image_url": f"{base_url}/roster/s/{token}/image",
        "expires_at": expires_at,
        "schedule_id": schedule_id,
        "group_id": target_group_id,
        "office_id": target_office_id,
    }



def upload_schedule_share_image_and_create_link_service(
    db: Session,
    current_user,
    schedule_id: str,
    fallback_base_url: str,
    image_file,
    title: str | None,
    description: str | None,
    expires_in_days: int,
    override_group_id: str | None = None,
) -> dict:
    import os
    import secrets
    import boto3

    allowed_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
    }
    content_type = str(getattr(image_file, "content_type", "") or "").lower()
    if content_type not in allowed_types:
        raise ValueError("지원하지 않는 이미지 형식입니다. (png, jpg, jpeg, webp)")

    image_bytes = image_file.file.read()
    if not image_bytes:
        raise ValueError("image file is empty")
    if len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("image file size must be <= 5MB")

    bucket_name = os.getenv("AWS_SHARE_S3_BUCKET") or os.getenv("SHARE_S3_BUCKET") or os.getenv("S3_SHARE_BUCKET")
    if not bucket_name:
        raise ValueError("share S3 bucket env is not configured")
    region = os.getenv("AWS_REGION", "ap-northeast-2")

    target_group_id, target_office_id = _share_resolve_target_scope(
        current_user=current_user,
        db=db,
        override_group_id=override_group_id,
    )
    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.office_id == target_office_id,
    ).first()
    if not schedule:
        raise LookupError("Schedule not found for your scope")

    ext = allowed_types[content_type]
    object_prefix = _share_build_object_prefix(
        office_id=target_office_id,
        group_id=target_group_id,
        nurse_id=getattr(current_user, "nurse_id", None),
        year=int(schedule.year),
        month=int(schedule.month),
    )
    object_key = f"{object_prefix}/{secrets.token_hex(16)}{ext}"

    try:
        s3_client = _share_build_s3_client(region)
        s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=image_bytes,
            ContentType=content_type,
            CacheControl="max-age=31536000",
        )
    except Exception as e:
        raise RuntimeError(f"S3 upload failed: {str(e)}")

    image_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/{object_key}"

    return create_schedule_share_link_service(
        db=db,
        current_user=current_user,
        schedule_id=schedule_id,
        fallback_base_url=fallback_base_url,
        image_url=image_url,
        title=title,
        description=description,
        expires_in_days=expires_in_days,
        override_group_id=override_group_id,
    )



def auto_generate_schedule_share_image_and_create_link_service(
    db: Session,
    current_user,
    schedule_id: str,
    fallback_base_url: str,
    title: str | None,
    description: str | None,
    expires_in_days: int,
    override_group_id: str | None = None,
) -> dict:
    import os
    import io
    import secrets
    import calendar
    import boto3
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from db.models import Nurse, ScheduleEntry

    target_group_id, target_office_id = _share_resolve_target_scope(
        current_user=current_user,
        db=db,
        override_group_id=override_group_id,
    )

    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.office_id == target_office_id,
    ).first()
    if not schedule:
        raise LookupError("Schedule not found for your scope")

    year = int(schedule.year)
    month = int(schedule.month)
    days_in_month = calendar.monthrange(year, month)[1]

    nurses = db.query(Nurse.nurse_id, Nurse.name, Nurse.sequence).filter(
        Nurse.group_id == target_group_id,
        Nurse.active == 1,
    ).order_by(Nurse.sequence.asc(), Nurse.nurse_id.asc()).all()

    entries = db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date, ScheduleEntry.shift_id).filter(
        ScheduleEntry.schedule_id == schedule_id,
    ).all()

    by_nurse = {}
    for e in entries:
        by_nurse.setdefault(str(e.nurse_id), {})[int(e.work_date.day)] = str(e.shift_id) if e.shift_id else "-"

    col_labels = ["이름"] + [str(d) for d in range(1, days_in_month + 1)]
    table_rows = []
    for n in nurses:
        row = [str(n.name)]
        day_map = by_nurse.get(str(n.nurse_id), {})
        for d in range(1, days_in_month + 1):
            row.append(day_map.get(d, "-"))
        table_rows.append(row)

    if not table_rows:
        table_rows = [["데이터 없음"] + ["-" for _ in range(days_in_month)]]

    fig_w = max(14, 1 + days_in_month * 0.42)
    fig_h = max(4, 1.5 + len(table_rows) * 0.35)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")
    ax.set_title(f"{year}년 {month}월 근무표", fontsize=16, pad=18)

    table = ax.table(
        cellText=table_rows,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
        colLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1.0, 1.35)

    image_buffer = io.BytesIO()
    fig.savefig(image_buffer, format="png", dpi=180, bbox_inches="tight")
    plt.close(fig)
    image_buffer.seek(0)
    image_bytes = image_buffer.getvalue()
    if not image_bytes:
        raise RuntimeError("failed to generate roster image")

    bucket_name = os.getenv("AWS_SHARE_S3_BUCKET") or os.getenv("SHARE_S3_BUCKET") or os.getenv("S3_SHARE_BUCKET")
    if not bucket_name:
        raise ValueError("share S3 bucket env is not configured")
    region = os.getenv("AWS_REGION", "ap-northeast-2")
    object_prefix = _share_build_object_prefix(
        office_id=target_office_id,
        group_id=target_group_id,
        nurse_id=getattr(current_user, "nurse_id", None),
        year=year,
        month=month,
    )
    object_key = f"{object_prefix}/auto-{secrets.token_hex(16)}.png"

    try:
        s3_client = _share_build_s3_client(region)
        s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=image_bytes,
            ContentType="image/png",
            CacheControl="max-age=31536000",
        )
    except Exception as e:
        raise RuntimeError(f"S3 upload failed: {str(e)}")

    image_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/{object_key}"

    return create_schedule_share_link_service(
        db=db,
        current_user=current_user,
        schedule_id=schedule_id,
        fallback_base_url=fallback_base_url,
        image_url=image_url,
        title=title,
        description=description,
        expires_in_days=expires_in_days,
        override_group_id=override_group_id,
    )



def capture_schedule_share_image_and_create_link_service(
    db: Session,
    current_user,
    schedule_id: str,
    fallback_base_url: str,
    image_data_url: str,
    title: str | None,
    description: str | None,
    expires_in_days: int,
    override_group_id: str | None = None,
) -> dict:
    import os
    import base64
    import secrets
    import binascii
    import boto3

    target_group_id, target_office_id = _share_resolve_target_scope(
        current_user=current_user,
        db=db,
        override_group_id=override_group_id,
    )

    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.office_id == target_office_id,
    ).first()
    if not schedule:
        raise LookupError("Schedule not found for your scope")

    if not image_data_url or not str(image_data_url).strip():
        raise ValueError("image_data_url is required")

    raw_data = str(image_data_url).strip()
    if not raw_data.startswith("data:"):
        raise ValueError("image_data_url must be data URL")

    try:
        header, b64_data = raw_data.split(",", 1)
    except ValueError:
        raise ValueError("invalid image_data_url format")

    if ";base64" not in header:
        raise ValueError("image_data_url must be base64 data URL")

    mime_type = header[5:].split(";", 1)[0].lower()
    allowed_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
    }
    if mime_type not in allowed_types:
        raise ValueError("지원하지 않는 이미지 형식입니다. (png, jpg, jpeg, webp)")

    try:
        image_bytes = base64.b64decode(b64_data, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid base64 image data")

    if not image_bytes:
        raise ValueError("image data is empty")
    if len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("image data size must be <= 5MB")

    bucket_name = os.getenv("AWS_SHARE_S3_BUCKET") or os.getenv("SHARE_S3_BUCKET") or os.getenv("S3_SHARE_BUCKET")
    if not bucket_name:
        raise ValueError("share S3 bucket env is not configured")
    region = os.getenv("AWS_REGION", "ap-northeast-2")

    ext = allowed_types[mime_type]
    object_prefix = _share_build_object_prefix(
        office_id=target_office_id,
        group_id=target_group_id,
        nurse_id=getattr(current_user, "nurse_id", None),
        year=int(schedule.year),
        month=int(schedule.month),
    )
    object_key = f"{object_prefix}/capture-{secrets.token_hex(16)}{ext}"

    try:
        s3_client = _share_build_s3_client(region)
        s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=image_bytes,
            ContentType=mime_type,
            CacheControl="max-age=31536000",
        )
    except Exception as e:
        raise RuntimeError(f"S3 upload failed: {str(e)}")

    image_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/{object_key}"

    return create_schedule_share_link_service(
        db=db,
        current_user=current_user,
        schedule_id=schedule_id,
        fallback_base_url=fallback_base_url,
        image_url=image_url,
        title=title,
        description=description,
        expires_in_days=expires_in_days,
        override_group_id=override_group_id,
    )


def get_public_share_link_service(db: Session, token: str) -> dict | None:
    share_row = _share_find_by_token(db, token)
    if not share_row:
        return None
    if share_row.get("revoked_at") is not None:
        return None
    expires_at = share_row.get("expires_at")
    if expires_at is not None and expires_at < _share_now():
        return None
    return share_row



def get_public_share_image_service(db: Session, token: str) -> tuple[bytes, str]:
    share_row = get_public_share_link_service(db, token)
    if not share_row:
        raise LookupError("Share link not found or expired")
    image_url = share_row.get("image_url")
    if not image_url:
        raise LookupError("Share image not found")
    return _share_fetch_s3_image_bytes(str(image_url))


def revoke_schedule_share_link_service(db: Session, current_user, token: str) -> dict:

    share_row = _share_find_by_token(db, token)
    if not share_row:
        raise LookupError("Share link not found")

    is_master_admin = bool(getattr(current_user, "is_master_admin", False))
    is_head_nurse = bool(getattr(current_user, "is_head_nurse", False))
    if not (is_master_admin or is_head_nurse):
        raise PermissionError("Permission denied")
    if is_master_admin and getattr(current_user, "office_id", None) and share_row.get("office_id") != getattr(current_user, "office_id", None):
        raise PermissionError("Share link does not belong to your office")
    if not is_master_admin and share_row.get("group_id") != getattr(current_user, "group_id", None):
        raise PermissionError("You can only revoke links in your group")

    from db.models import ShareLink

    revoked_at = _share_now()
    db.query(ShareLink).filter(ShareLink.token == token).update(
        {
            ShareLink.revoked_at: revoked_at,
            ShareLink.updated_at: revoked_at,
        },
        synchronize_session=False,
    )
    db.commit()

    return {"success": True, "token": token, "revoked_at": revoked_at}
