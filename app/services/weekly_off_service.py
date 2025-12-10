from typing import List, Optional
from datetime import date, datetime
from sqlalchemy.orm import Session
from sqlalchemy import func
from fastapi import HTTPException

from db.models import WeeklyOffSetting, Nurse, Team, Group
from schemas.weekly_off_schema import (
    WeeklyOffSettingUpdate,
    WeeklyOffSettingResponse,
    WeeklyOffNurseUpdatePayload,
    WeeklyOffNurseListResponse,
    NurseWeeklyOffItem
)
from schemas.auth_schema import User as UserSchema

# ------------------------------------------------------------------
# 1. 공통 계산 함수 (Core Logic)
# ------------------------------------------------------------------

def calc_weekly_off_weekday_by_month(
    base_weekday: int,
    shift_variation: int,
    base_year: int,
    base_month: int,
    target_year: int,
    target_month: int,
) -> int:
    """
    월 단위 shift rotation(weekday rolling)을 적용해 타깃 월의 주휴 요일을 계산합니다.
    
    수식:
        weekday = (base_weekday + months_diff * shift_variation) % 7
        
    Args:
        base_weekday (int): 기준 월의 요일 (0=월, ... 6=일)
        shift_variation (int): 변동 요일 수 (예: -1)
        base_year (int): 기준 연도
        base_month (int): 기준 월
        target_year (int): 대상 연도
        target_month (int): 대상 월
        
    Returns:
        int: 계산된 타깃 월의 요일 (0~6)
    """
    months_diff = (target_year - base_year) * 12 + (target_month - base_month)
    # months_diff 가 음수여도 mod 7 연산은 파이썬에서 올바르게 순환됨 (-1 % 7 = 6)
    return (base_weekday + months_diff * shift_variation) % 7


def calc_weekly_off_weekday_by_week(
    base_weekday: int,
    shift_variation: int,
    cycle_start_date: date,
    target_date: date,
    cycle_interval_weeks: int = 1,
) -> int:
    """
    주 단위 shift rotation을 적용해 타깃 날짜가 포함된 주의 주휴 요일을 계산합니다.
    
    Args:
        base_weekday (int): 기준 요일
        shift_variation (int): 변동 폭
        cycle_start_date (date): 주기 시작일
        target_date (date): 대상 일자
        cycle_interval_weeks (int): 주기 간격 (주 단위)
        
    Returns:
        int: 계산된 요일 (0~6)
    """
    if cycle_interval_weeks < 1:
        cycle_interval_weeks = 1
        
    days_diff = (target_date - cycle_start_date).days
    weeks_diff = days_diff // 7
    steps = weeks_diff // cycle_interval_weeks
    
    return (base_weekday + steps * shift_variation) % 7


def get_weekday_label(weekday: Optional[int]) -> Optional[str]:
    if weekday is None:
        return None
    labels = ["월", "화", "수", "목", "금", "토", "일"]
    return labels[weekday % 7]


# ------------------------------------------------------------------
# 2. 서비스 함수
# ------------------------------------------------------------------

def get_weekly_off_settings_service(
    user: UserSchema, db: Session, group_id: Optional[str] = None
) -> WeeklyOffSettingResponse:
    """
    주휴 설정 조회
    """
    target_group_id = group_id if group_id else user.group_id
    if not target_group_id:
         raise HTTPException(status_code=400, detail="Target group_id is required")

    setting = db.query(WeeklyOffSetting).filter(
        WeeklyOffSetting.group_id == target_group_id
    ).first()

    if not setting:
        # 없으면 기본값으로 응답 (DB 저장은 안 함)
        # office_id를 찾기 위해 Group 조회
        grp = db.query(Group).filter(Group.group_id == target_group_id).first()
        if not grp:
             raise HTTPException(status_code=404, detail="Group not found")
             
        return WeeklyOffSettingResponse(
            office_id=grp.office_id,
            group_id=grp.group_id,
            activate=False,
            use_variable_cycle=False,
            cycle_type='month',
            base_year=None,
            base_month=None,
            updated_at=None
        )
    
    return setting


def update_weekly_off_settings_service(
    payload: WeeklyOffSettingUpdate,
    current_user: UserSchema,
    db: Session,
    group_id: Optional[str] = None
):
    """
    주휴 설정 저장/업데이트
    """
    target_group_id = group_id if group_id else current_user.group_id
    if not target_group_id:
         raise HTTPException(status_code=400, detail="Target group_id is required")
         
    # 오피스 ID 조회
    grp = db.query(Group).filter(Group.group_id == target_group_id).first()
    if not grp:
         raise HTTPException(status_code=404, detail="Group not found")
    
    setting = db.query(WeeklyOffSetting).filter(
        WeeklyOffSetting.group_id == target_group_id
    ).first()
    
    if not setting:
        setting = WeeklyOffSetting(
            office_id=grp.office_id,
            group_id=target_group_id
        )
        db.add(setting)
    
    print('payload.activate', payload.activate)
    print('payload.use_variable_cycle', payload.use_variable_cycle)
    print('payload.cycle_type', payload.cycle_type)
    print('payload.cycle_start_date', payload.cycle_start_date)
    print('payload.cycle_interval', payload.cycle_interval)
    print('payload.shift_variation', payload.shift_variation)
    # 필드 업데이트
    setting.activate = payload.activate
    setting.use_variable_cycle = payload.use_variable_cycle
    setting.cycle_type = payload.cycle_type
    setting.cycle_start_date = payload.cycle_start_date
    setting.cycle_interval = payload.cycle_interval
    setting.shift_variation = payload.shift_variation
    
    # base_year/base_month 가 없는 상태에서 최초 활성화 시, 현재 시점을 기준으로 잡을 수도 있음.
    # 여기서는 명시적 업데이트가 없으면 그대로 둠 (간호사 설정 시점에 갱신됨)
    
    setting.updated_at = datetime.now()
    db.commit()
    db.refresh(setting)
    return setting


def get_nurses_weekly_off_service(
    year: int,
    month: int,
    user: UserSchema,
    db: Session,
    group_id: Optional[str] = None
) -> WeeklyOffNurseListResponse:
    """
    간호사 주휴 설정 목록 조회 (+미리보기 계산)
    """
    target_group_id = group_id if group_id else user.group_id
    
    # 설정 조회
    setting = db.query(WeeklyOffSetting).filter(
        WeeklyOffSetting.group_id == target_group_id
    ).first()
    
    cycle_type = setting.cycle_type if setting else 'month'
    
    # 간호사 목록 조회 (Team 조인)
    nurses = db.query(Nurse, Team.team_name).outerjoin(
        Team, (Nurse.group_id == Team.group_id) & (Nurse.team_id == Team.team_id)
    ).filter(
        Nurse.group_id == target_group_id,
        Nurse.active == 1
    ).order_by(Nurse.sequence.asc(), Nurse.name.asc()).all()
    
    items = []
    for n, team_name in nurses:
        base_weekday = n.weekly_off_weekday
        preview_weekday = None
        
        # 미리보기 계산
        if n.weekly_off_enabled and base_weekday is not None and setting:
            if setting.use_variable_cycle:
                # 1. 월 단위
                if setting.cycle_type == 'month' and setting.base_year and setting.base_month:
                    preview_weekday = calc_weekly_off_weekday_by_month(
                        base_weekday=base_weekday,
                        shift_variation=setting.shift_variation,
                        base_year=setting.base_year,
                        base_month=setting.base_month,
                        target_year=year,
                        target_month=month
                    )
                # 2. 주 단위
                elif setting.cycle_type == 'week' and setting.cycle_start_date:
                    target_date = date(year, month, 1) # 해당 월 1일 기준 (혹은 별도 파라미터 필요?)
                    # 여기서는 월 단위 API이므로 해당 월의 "첫 번째 주" 기준으로 보여주거나,
                    # UI 상에서 별도 주 단위 뷰가 필요할 수 있음.
                    # 일단 1일 기준으로 계산해줌.
                    preview_weekday = calc_weekly_off_weekday_by_week(
                        base_weekday=base_weekday,
                        shift_variation=setting.shift_variation,
                        cycle_start_date=setting.cycle_start_date,
                        target_date=target_date,
                        cycle_interval_weeks=setting.cycle_interval
                    )
                else:
                    # 변동 설정이 불완전하면 base 유지
                    preview_weekday = base_weekday
            else:
                # 변동 안 함
                preview_weekday = base_weekday
        
        items.append(NurseWeeklyOffItem(
            nurse_id=n.nurse_id,
            emp_num=n.emp_num,
            name=n.name,
            role=n.role,
            team_name=team_name,
            weekly_off_enabled=bool(n.weekly_off_enabled),
            base_weekday=base_weekday,
            preview_weekday=preview_weekday,
            preview_weekday_label=get_weekday_label(preview_weekday)
        ))
        
    return WeeklyOffNurseListResponse(
        year=year,
        month=month,
        cycle_type=cycle_type,
        items=items
    )


def update_nurses_weekly_off_service(
    payload: WeeklyOffNurseUpdatePayload,
    current_user: UserSchema,
    db: Session,
    group_id: Optional[str] = None
):
    """
    간호사별 주휴 설정 저장
    중요: 저장 시점의 연/월을 '기준 시점(base_year/base_month)'으로 업데이트함.
    """
    target_group_id = group_id if group_id else current_user.group_id
    
    # 설정 조회 및 base update
    setting = db.query(WeeklyOffSetting).filter(
        WeeklyOffSetting.group_id == target_group_id
    ).first()
    
    now = datetime.now()
    
    if not setting:
        # 설정이 없으면 자동 생성
        grp = db.query(Group).filter(Group.group_id == target_group_id).first()
        setting = WeeklyOffSetting(
            office_id=grp.office_id,
            group_id=target_group_id,
            activate=True,
            created_at=now
        )
        db.add(setting)
    print('setting', setting.base_year, setting.base_month, setting.updated_at)
    # 기준 시점 갱신 (현재 저장하는 시점의 연/월이 기준이 됨)
    setting.base_year = now.year
    setting.base_month = now.month
    setting.updated_at = now
    
    # 간호사 업데이트
    update_map = {item.nurse_id: item for item in payload.items}
    print('update_map', update_map)
    nurses = db.query(Nurse).filter(
        Nurse.group_id == target_group_id,
        Nurse.nurse_id.in_(update_map.keys())
    ).all()
    print('nurses', [n.__dict__ for n in nurses])
    updated_count = 0
    for n in nurses:
        data = update_map[n.nurse_id]
        n.weekly_off_enabled = 1 if data.weekly_off_enabled else 0
        n.weekly_off_weekday = data.weekly_off_weekday if data.weekly_off_enabled else None
        updated_count += 1
        
    db.commit()
    return {
        "updated_nurses": updated_count,
        "base_year": setting.base_year,
        "base_month": setting.base_month,
        "updated_at": setting.updated_at
    }



