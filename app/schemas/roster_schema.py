from pydantic import BaseModel, Field, field_validator, ValidationInfo, EmailStr
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime
from enum import StrEnum

class ShiftManageSaveRequest(BaseModel):
    class_name: str
    slots: list  # [{"shift_slot": 1, "codes": ["D"], "manpower": 3}, ...]

class WantedDeadlineRequest(BaseModel):
    year: int
    month: int
    exp_date: Optional[datetime] = None

class MoveShiftRequest(BaseModel):
    """시프트 순서 이동 요청 모델."""
    shift_id: str
    new_sequence: int

class MoveNurseRequest(BaseModel):
    nurse_id: str
    new_sequence: int


class RemoveShiftRequest(BaseModel):
    """시프트 삭제 요청 모델."""
    shift_id: str


class ShiftUpdateRequest(BaseModel):
    """시프트 수정 요청 모델."""
    default_shift: Optional[str] = None
    shift_gb: Optional[str] = None
    shift_id: str
    name: str
    color: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    type: str  # Changed from 'type' to 'shift_type' to match frontend
    # time_type: str = "range"
    duration: Optional[int] = None
    allday: Optional[int] = 0
    auto_schedule: Optional[int] = 1
    id: int
    # 추가
    show_in_preference: Optional[bool] = None # None이면 기존 값 유지

class ShiftAddRequest(BaseModel):
    """시프트 등록 요청 모델."""
    default_shift: Optional[str] = None
    shift_gb: Optional[str] = None
    shift_id: str
    name: str
    color: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    type: str  # Changed from 'type' to 'shift_type' to match frontend
    # time_type: str = "range"
    duration: Optional[int] = None
    allday: Optional[int] = 0
    auto_schedule: Optional[int] = 1
    # id: int
    # 추가 내역
    show_in_preference: Optional[bool] = False # 기본 False, 프론트에서 안 보내면 자동 숨김

class RosterRequest(BaseModel):
    """근무표 생성 요청(임시: UI 미구현 상태에서 req로 정책 파라미터를 주입하기 위한 모델).

    Notes:
        - `preceptor_gauge` 등 기존 게이지와 동일하게, 분배 정책도 req로 주입받아 실행마다 조절한다.
        - 월단위 선호는 개인 입력값(간호사별)이고, 반영 강도/모드는 수간호사(생성 요청자)가 선택한다.
    """
    year: int
    month: int
    # algorithm: str = "cp_sat"  # "cp_sat" or "random_sampling"
    config_id: Optional[int] = None
    grade_strategy: str = "BASE"  # "BASE" | "TEAM" | "GRADE"
    preceptor_gauge: Optional[int] = Field(default=None, ge=0, le=10)
    # ── Shift 분배 정책(임시: UI 대신 req로 제어) ──
    # mode:
    # - auto/hybrid: 균등 + 월선호를 함께 고려(기본)
    # - balanced: 균등 분배만 강화
    # - preference: 월단위 선호를 상대적으로 강화
    # - off: 기존처럼 분배 정책 항을 끈다(디버깅/레거시)
    distribution_mode: str = Field(default="hybrid")
    oversupply_balance_gauge: Optional[int] = Field(default=6, ge=0, le=10)
    monthly_preference_gauge: Optional[int] = Field(default=3, ge=0, le=10)
    # 월단위 선호(개인 입력): nurse_id -> {"shift": "D|E|N", "strength": 0~10}
    monthly_shift_preferences: Optional[Dict[str, Dict[str, Any]]] = None

class PreferenceSubmit(BaseModel):
    year: int
    month: int

class PreferenceData(BaseModel):
    year: int
    month: int
    # data: dict
    data: Dict[str, Any] = Field(default_factory=dict)


class PublishRequest(BaseModel):
    schedule_id: str
    issue_comment: str = None

class WantedInvokeRequest(BaseModel):
    request: str| List[str]
    schema: List[Dict[str, Any]]
    case: object | None = None
    year: int
    month: int

class WantedInvokeResponse(BaseModel):
    response: Any

class RosterConfigBase(BaseModel):
    day_req: Optional[int] = 0
    eve_req: Optional[int] = 0
    nig_req: Optional[int] = 0
    min_exp_per_shift: int
    req_exp_nurses: int
    two_offs_per_week: bool
    max_nig_per_month: int
    three_seq_nig: bool
    two_offs_after_three_nig: bool
    two_offs_after_two_nig: bool
    banned_day_after_eve: bool
    max_conseq_work: int
    off_days: int
    shift_priority: float
    weekend_shift_ratio: float
    patient_amount: int
    even_nights: bool
    sequential_offs: bool
    nod_noe: bool
    not_one_night: bool = Field(default=False, description="야간 단발성(1N) 금지 여부")
    preceptor_gauge: float
    weekly_off_group: bool = Field(default=False)
    team_balance_enable: bool = Field(default=False)
    team_balance_gauge: int = Field(default=0, ge=0, le=10)
    team_balance_mode: str = Field(default="balanced")

class RosterConfigCreate(RosterConfigBase):
    config_version: Optional[str] = None
    # pass

class RosterConfig(RosterConfigBase):
    config_id: int
    office_id: str
    group_id: str
    created_at: str

    class Config:
        from_attributes = True

class CodeMapp(StrEnum):
    D = "D"
    E = "E"
    N = "N"

class NurseProfile(BaseModel):
    office_id: str
    # EmpAuthGbn: Optional[str] = None
    nurse_id: str
    group_id: str
    account_id: str
    name: str
    experience: Optional[int] = None
    role: Optional[str] = None
    level_: Optional[str] = None
    is_head_nurse: bool = Field(default=False)
    is_night_nurse: List[CodeMapp] = Field(default_factory=list, max_items = 2)
    personal_off_adjustment: int = Field(default=0)
    preceptor_id: Optional[str] = None
    joining_date: Optional[datetime] = None
    resignation_date: Optional[datetime] = None
    sequence: Optional[int] = 0
    active: int = 1
    fixed_shift: Optional[str] = None
    # weekly_off_enabled: int = Field(default=0)
    weekly_off_weekday: Optional[int] = None
    nurse_memo: Optional[str] = None
    grade: Optional[int] = None
    emp_num: Optional[str] = None
    # Side-Profile 추가 컬럼
    birth_date: Optional[str] = None
    phone_number: Optional[str] = None
    age: Optional[int] = None # 나이
    gender: Optional[str] = None
    # 추가
    team_id: Optional[int] = None
    is_weekend_off: bool = Field(default=False)  # 주말 휴무 여부
    # 추가
    work_shifts: Optional[List[str]] = Field(
        default_factory=list,
        description="근무 가능 형태 배열. 예: ['D', 'E2', 'N1', 'MD'] 또는 ['D', 'N']"
    )
    
    # @field_validator('fixed_shift')
    # @classmethod
    # def check_fixed_shift_with_weekend_off(cls, v: Any, info: ValidationInfo) -> Any:
    #     # info.data 에서 이미 검증된 다른 필드 값들을 가져옴
    #     is_weekend_off = info.data.get('is_weekend_off', False) if info.data else False
    #     if not is_weekend_off:
    #         return None  # 주말 휴무 미적용 시 fixed_shift는 None으로 강제
    #     if v in ('M', 'D', None, ''):
    #         return None if v in ('', None) else v
    #     raise ValueError('fixed_shift는 주말 휴무 적용 시 "M", "D" 또는 없음만 가능합니다.')

    class Config:
        from_attributes = True

class ExcelValidationRequest(BaseModel):
    data: List[dict]
    include_rows: List[bool] = []

class NurseSequenceUpdate(BaseModel):
    nurse_id: str
    new_sequence: int = Field(ge=1)
    active: Optional[int] = Field(default=None, description="0: 비활성, 1: 활성, None: 변경 없음")
class ReorderPayload(BaseModel):
    active_order: List[str] = Field(default_factory=list)
    inactive_order: List[str] = Field(default_factory=list)
class ExcelConfirmRequest(BaseModel):
    data: List[dict]
    include_rows: List[bool]
    new_groups_to_create: List[str] = []

class ScheduleMemoUpdate(BaseModel):
    schedule_id: str
    memo: str | None = None
    group_id: str | None = None 

class IntegratedRegisterRequest(BaseModel):
    group_id: str
    members: List[Dict[str, Any]]  # 한국어 키로 입력



# 마이 페이지 테스트
class PersonnelUpdate(BaseModel):
    """마이페이지 기본 정보 수정 (이메일 + 총경력만 허용)"""
    email: Optional[EmailStr] = Field(None, description="이메일 주소")
    experience: Optional[int] = Field(None, ge=0, description="총 경력(년)")

class PasswordChangeRequest(BaseModel):
    """비밀번호 변경 요청 (SMS 인증 후 사용)"""
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)
    confirm_password: str = Field(..., min_length=8)
    verification_code: Optional[str] = Field(None, description="SMS 인증번호 (최초 요청 시 생략)")

class PhoneChangeRequest(BaseModel):
    """휴대폰 번호 변경 요청"""
    new_phone_number: str = Field(..., pattern=r"^01[0-9]{8,9}$", description="새 휴대폰 번호")
    verification_code: Optional[str] = Field(None, description="인증번호 (검증 단계에서만 사용)")