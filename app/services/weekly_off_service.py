from typing import List, Optional
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func
from fastapi import HTTPException, status

from db.models import WeeklyOffSetting, Nurse, Team, Group
from schemas.weekly_off_schema import (
    WeeklyOffSettingUpdate,
    WeeklyOffSettingResponse,
    WeeklyOffNurseUpdatePayload,
    WeeklyOffNurseListResponse,
    NurseWeeklyOffItem,
    MyWeeklyOffResponse
)
from schemas.auth_schema import User as UserSchema
from services.group_access import assert_caller_can_access_group

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


def resolve_weekly_off_base_weekday(nurse: Nurse) -> Optional[int]:
    """
    기준 주휴 요일을 반환합니다.
    is_weekend_off 여부와 무관하게 저장된 weekly_off_weekday를 그대로 반환합니다.
    (주말휴무 간호사의 일요일 주휴 스킵은 엔트리 생성 단계에서 처리)

    Args:
        nurse (Nurse): 주휴 요일을 조회할 간호사 객체

    Returns:
        Optional[int]: 저장된 주휴 요일 (없으면 None)
    """
    if nurse is None:
        return None
    return nurse.weekly_off_weekday


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
    assert_caller_can_access_group(db, user, target_group_id)

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
    assert_caller_can_access_group(db, current_user, target_group_id)

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
    
    # 추가
    if payload.cycle_start_date is not None:
        setting.base_year = payload.cycle_start_date.year
        setting.base_month = payload.cycle_start_date.month
    # base_year/base_month 가 없는 상태에서 최초 활성화 시, 현재 시점을 기준으로 잡을 수도 있음.
    # 여기서는 명시적 업데이트가 없으면 그대로 둠 (간호사 설정 시점에 갱신됨)
    
    # print("[UPDATE SETTINGS] cycle_start_date:", payload.cycle_start_date)
    # if payload.cycle_start_date:
        # print("  → year:", payload.cycle_start_date.year, "month:", payload.cycle_start_date.month)
    
    # print("[BEFORE COMMIT] base_year:", setting.base_year, "base_month:", setting.base_month)
    
    setting.updated_at = datetime.now()
    db.commit()
    db.refresh(setting)
    # print("[AFTER REFRESH] base_year:", setting.base_year, "base_month:", setting.base_month)
    
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
    if not target_group_id:
        raise HTTPException(status_code=400, detail="Target group_id is required")
    assert_caller_can_access_group(db, user, target_group_id)

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
        base_weekday = resolve_weekly_off_base_weekday(n)
        is_weekend_off = bool(getattr(n, "is_weekend_off", False))
        preview_weekday = None
        
        # 미리보기 계산 (is_weekend_off 여부와 무관하게 동일한 사이클 로직 적용)
        if n.weekly_off_enabled and base_weekday is not None:
            if setting and setting.use_variable_cycle:
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
                # 변동 안 함 또는 설정 없음
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
    if not target_group_id:
        raise HTTPException(status_code=400, detail="Target group_id is required")
    assert_caller_can_access_group(db, current_user, target_group_id)

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
    # 기준 시점 갱신 (현재 저장하는 시점의 연/월이 기준이 됨)
    setting.base_year = now.year
    setting.base_month = now.month
    setting.updated_at = now
    
    # 간호사 업데이트
    update_map = {item.nurse_id: item for item in payload.items}

    nurses = db.query(Nurse).filter(
        Nurse.group_id == target_group_id,
        Nurse.nurse_id.in_([n for n in update_map.keys()])
    ).all()

    updated_count = 0
    for n in nurses:
        data = update_map[n.nurse_id]
        n.weekly_off_enabled = 1 if data.weekly_off_enabled else 0
        n.weekly_off_weekday = data.weekly_off_weekday if data.weekly_off_enabled else None
        updated_count += 1

    # ── 프리셉티 주휴 자동 동기화: 프리셉터의 주휴가 변경되면 프리셉티도 동일하게 ──
    updated_preceptor_ids = set(update_map.keys())
    preceptees = db.query(Nurse).filter(
        Nurse.group_id == target_group_id,
        Nurse.preceptor_id.in_(list(updated_preceptor_ids)),
        Nurse.active == 1,
    ).all()
    if preceptees:
        # 프리셉터 nurse_id → 최신 주휴 설정 매핑
        preceptor_map = {n.nurse_id: n for n in nurses}
        synced = 0
        for pte in preceptees:
            ptr = preceptor_map.get(pte.preceptor_id)
            if not ptr:
                continue
            pte.weekly_off_enabled = ptr.weekly_off_enabled
            pte.weekly_off_weekday = ptr.weekly_off_weekday
            synced += 1
        if synced:
            print(f"[WeeklyOff] 프리셉티 주휴 동기화: {synced}명 (프리셉터 주휴 따라감)")
            updated_count += synced

    db.commit()
    return {
        "updated_nurses": updated_count,
        "base_year": setting.base_year,
        "base_month": setting.base_month,
        "updated_at": setting.updated_at
    }


# def get_my_weekly_off_service(
#     year: int,
#     month: int,
#     user: UserSchema,
#     db: Session
# ) -> MyWeeklyOffResponse:
#     nurse = db.query(Nurse).filter(Nurse.nurse_id == user.nurse_id).first()
#     if not nurse:
#         raise HTTPException(status_code=404, detail="Nurse not found")
    
#     setting = db.query(WeeklyOffSetting).filter(
#         WeeklyOffSetting.group_id == user.group_id
#     ).first()
    
#     if not nurse.weekly_off_enabled or nurse.weekly_off_weekday is None:
#         return MyWeeklyOffResponse(
#             year=year,
#             month=month,
#             my_weekly_off_dates=[],
#             my_weekly_off_weekday=None,
#             my_weekly_off_label=None
#         )
    
#     preview_weekday = nurse.weekly_off_weekday  # 기본값
#     if setting and setting.use_variable_cycle:
#         # 변동 계산 로직 (기존과 동일)
#         if setting.cycle_type == 'month' and setting.base_year and setting.base_month:
#             preview_weekday = calc_weekly_off_weekday_by_month(
#                 base_weekday=nurse.weekly_off_weekday,
#                 shift_variation=setting.shift_variation,
#                 base_year=setting.base_year,
#                 base_month=setting.base_month,
#                 target_year=year,
#                 target_month=month
#             )
#         elif setting.cycle_type == 'week' and setting.cycle_start_date:
#             target_date = date(year, month, 1)
#             preview_weekday = calc_weekly_off_weekday_by_week(
#                 base_weekday=nurse.weekly_off_weekday,
#                 shift_variation=setting.shift_variation,
#                 cycle_start_date=setting.cycle_start_date,
#                 target_date=target_date,
#                 cycle_interval_weeks=setting.cycle_interval
#             )
    
#     dates = []
#     current = date(year, month, 1)
#     while current.month == month:
#         if current.weekday() == preview_weekday:
#             dates.append(current.isoformat())
#         current += timedelta(days=1)
    
#     dates.sort()
    
#     return MyWeeklyOffResponse(
#         year=year,
#         month=month,
#         my_weekly_off_dates=dates,
#         my_weekly_off_weekday=preview_weekday,
#         my_weekly_off_label=get_weekday_label(preview_weekday)
#     )


def get_my_weekly_off_service(
    year: int,
    month: int,
    user: UserSchema,
    db: Session,
    target_nurse_id: Optional[str] = None  # nurse_id가 str이므로 str로 변경
) -> MyWeeklyOffResponse:

    is_master_admin = user.is_master_admin

    # 조회 대상 nurse 결정
    if target_nurse_id is not None:
        # 통합 권한 헬퍼: ADM / self / home·managed source / inbound 모두 처리
        from services.group_access import can_caller_access_nurse
        if not can_caller_access_nurse(db, user, target_nurse_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="해당 간호사를 조회할 권한이 없습니다.",
            )

        nurse = db.query(Nurse).filter(Nurse.nurse_id == target_nurse_id).first()
        if not nurse:
            raise HTTPException(status_code=404, detail="Target nurse not found")

    else:
        # 본인 조회
        if is_master_admin:
            # 관리자는 본인 주휴 없음 → 빈 결과 반환하거나 에러
            return MyWeeklyOffResponse(
                year=year,
                month=month,
                my_weekly_off_dates=[],
                my_weekly_off_weekday=None,
                my_weekly_off_label=None
            )
        
        nurse = db.query(Nurse).filter(Nurse.nurse_id == user.nurse_id).first()
        if not nurse:
            raise HTTPException(status_code=404, detail="Nurse not found")

    # 여기부터는 기존 로직과 완전 동일
    setting = db.query(WeeklyOffSetting).filter(
        WeeklyOffSetting.group_id == nurse.group_id
    ).first()

    base_weekday = resolve_weekly_off_base_weekday(nurse)
    if not nurse.weekly_off_enabled or base_weekday is None:
        return MyWeeklyOffResponse(
            year=year,
            month=month,
            my_weekly_off_dates=[],
            my_weekly_off_weekday=None,
            my_weekly_off_label=None
        )

    preview_weekday = base_weekday
    is_weekend_off = bool(getattr(nurse, "is_weekend_off", False))
    if not is_weekend_off and setting and setting.use_variable_cycle:
        if setting.cycle_type == 'month' and setting.base_year and setting.base_month:
            preview_weekday = calc_weekly_off_weekday_by_month(
                base_weekday=base_weekday,
                shift_variation=setting.shift_variation,
                base_year=setting.base_year,
                base_month=setting.base_month,
                target_year=year,
                target_month=month
            )
        elif setting.cycle_type == 'week' and setting.cycle_start_date:
            target_date = date(year, month, 1)
            preview_weekday = calc_weekly_off_weekday_by_week(
                base_weekday=base_weekday,
                shift_variation=setting.shift_variation,
                cycle_start_date=setting.cycle_start_date,
                target_date=target_date,
                cycle_interval_weeks=setting.cycle_interval
            )

    dates = []
    current = date(year, month, 1)
    while current.month == month:
        if current.weekday() == preview_weekday:
            dates.append(current.isoformat())
        current += timedelta(days=1)

    dates.sort()

    return MyWeeklyOffResponse(
        year=year,
        month=month,
        my_weekly_off_dates=dates,
        my_weekly_off_weekday=preview_weekday,
        my_weekly_off_weekday_base=nurse.weekly_off_weekday,
        my_weekly_off_label=get_weekday_label(preview_weekday)
    )