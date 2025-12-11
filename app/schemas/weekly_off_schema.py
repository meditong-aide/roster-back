from typing import List, Optional
from pydantic import BaseModel
from datetime import date, datetime

class WeeklyOffSettingBase(BaseModel):
    activate: bool = False
    use_variable_cycle: bool = False
    cycle_type: Optional[str] = 'month'
    cycle_start_date: Optional[date] = None
    cycle_interval: Optional[int] = 1
    shift_variation: int = -1

class WeeklyOffSettingUpdate(WeeklyOffSettingBase):
    pass

class WeeklyOffSettingResponse(WeeklyOffSettingBase):
    office_id: str
    group_id: str
    base_year: Optional[int]
    base_month: Optional[int]
    updated_at: Optional[datetime]

    class Config:
        orm_mode = True

class NurseWeeklyOffItem(BaseModel):
    nurse_id: str
    emp_num: Optional[str]
    name: str
    role: Optional[str]
    team_name: Optional[str]
    
    weekly_off_enabled: bool
    base_weekday: Optional[int]  # 0=월, 6=일
    
    # 미리보기용 (읽기 전용)
    preview_weekday: Optional[int]
    preview_weekday_label: Optional[str]

class WeeklyOffNurseListResponse(BaseModel):
    year: int
    month: int
    cycle_type: str
    items: List[NurseWeeklyOffItem]

class NurseWeeklyOffUpdateItem(BaseModel):
    nurse_id: str
    weekly_off_enabled: bool
    weekly_off_weekday: Optional[int]

class WeeklyOffNurseUpdatePayload(BaseModel):
    items: List[NurseWeeklyOffUpdateItem]



