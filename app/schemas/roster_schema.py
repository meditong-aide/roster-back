from pydantic import BaseModel, Field, field_validator, ValidationInfo, EmailStr
from typing import List, Dict, Any, Optional, Literal
from datetime import date, datetime
from enum import StrEnum


class ShiftManageSaveRequest(BaseModel):
    class_name: str
    slots: list  # [{"shift_slot": 1, "codes": ["D"], "manpower": 3}, ...]


class WantedDeadlineRequest(BaseModel):
    year: int
    month: int
    # exp_date: Optional[datetime] = None
    exp_date: Optional[datetime] = Field(
        None,
        description="마감일 (선택). null 또는 생략 시 '마감일 없음'으로 처리됩니다.",
    )


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
    show_in_preference: Optional[bool] = None  # None이면 기존 값 유지


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
    show_in_preference: Optional[bool] = (
        False  # 기본 False, 프론트에서 안 보내면 자동 숨김
    )


class ShiftUploadConfirmRequest(BaseModel):
    """근무코드 엑셀 업로드 확정 요청 모델."""

    rows: List[dict]
    group_id: Optional[str] = None


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
    not_one_night: Optional[bool] = Field(
        default=None, description="야간 단발성(1N) 금지 여부"
    )
    # 확정 원티드 사용 여부 (True: FixedWantedEntry 사용, False: 기존 WantedRequest 사용)
    use_fixed_wanted: bool = Field(
        default=False, description="(미사용) 확정 원티드가 존재하면 자동 적용됨"
    )


class PreferenceSubmit(BaseModel):
    year: int
    month: int


class PreferenceData(BaseModel):
    year: int
    month: int
    # data: dict
    data: Dict[str, Any] = Field(default_factory=dict)
    preference: Optional[List[Dict[str, Any]]] = None


class PublishRequest(BaseModel):
    schedule_id: str
    issue_comment: str = None


class CaseItem(BaseModel):
    """Wanted case 항목 스키마입니다.

    날짜는 YYYY-MM-DD 문자열 또는 date로 받으며,
    year/month가 함께 들어오면 월 검증에 활용합니다.
    """

    date: date | int | str
    shift: str
    year: int | None = None
    month: int | None = None
    # 사유작성
    comment: Optional[str] = Field(None)

    class Config:
        extra = "allow"


class WantedInvokeRequest(BaseModel):
    request: str | List[str] | None = None
    schema: List[Dict[str, Any]]
    case: List[CaseItem | Dict[str, Any]] | None = None
    year: int
    month: int


class WantedInvokeResponse(BaseModel):
    response: Any


# Wanted Config 관련 스키마 (DAILY_LIMIT 전용)
# - GLOBAL, NURSE_LIMIT 설정은 nurses 테이블로 이동됨
class WantedConfigBase(BaseModel):
    """일자별 원티드 제한 설정 기본 스키마 (DAILY_LIMIT 전용)"""

    year: Optional[int] = None
    month: Optional[int] = None
    max_requests: Optional[int] = None  # 해당 일자 최대 요청 개수
    # target_date: Optional[str] = None  # 특정 일자 (YYYY-MM-DD)
    target_date: Optional[date] = None
    shift_type: Optional[str] = None  # 근무 타입 (휴무/휴가)


class WantedConfigCreate(WantedConfigBase):
    """원티드 설정 생성/수정 요청 스키마"""

    pass


class WantedConfig(WantedConfigBase):
    """원티드 설정 조회 응답 스키마"""

    config_id: int
    group_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


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
    use_mid: bool = Field(default=False)
    preceptor_gauge: float
    preceptee_on: bool = Field(
        default=False, description="프리셉티 팔로우 모드 (ON 시 프리셉터 근무 따라감)"
    )
    preceptee_shift_count: bool = Field(
        default=True,
        description="프리셉티 커버리지 포함 여부 (ON: DEN 포함, OFF: DEN 제외, preceptee_on=True일 때만 유효)",
    )
    weekly_off_group: bool = Field(default=False)
    team_balance_enable: bool = Field(default=False)
    team_balance_gauge: int = Field(default=0, ge=0, le=10)
    team_balance_mode: str = Field(default="balanced")
    off_placement_mode: int = Field(
        default=1, description="주휴 인접 OFF 배치 모드(0=미적용, 1=앞/뒤, 2=앞 우선)"
    )
    fixed_wanted_use_yn: bool = Field(
        default=False, description="확정 원티드 DENO 전체 고정 여부"
    )
    show_level: bool = Field(
        default=True, description="근무표에 직책(level_) 표시 여부"
    )
    show_preceptor: bool = Field(
        default=True, description="근무표에 프리셉터-프리셉티 관계 표시 여부"
    )
    use_max_coverage: bool = Field(
        default=False,
        description="True 시 daily coverage를 max coverage로 적용 (초과 배정 불가, 잔여 인원 Off)",
    )


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


# class CodeMapp(StrEnum):
#     D = "D"
#     E = "E"
#     N = "N"


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
    # is_night_nurse: List[CodeMapp] = Field(default_factory=list, max_items = 2)
    is_night_nurse: List[str] = Field(default_factory=list)
    personal_off_adjustment: int = Field(default=0)
    preceptor_id: Optional[str] = None
    joining_date: Optional[datetime] = None
    resignation_date: Optional[datetime] = None
    resignation_reason: Optional[str] = None
    resignation_reason_memo: Optional[str] = None
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
    email: Optional[str] = None
    age: Optional[int] = None  # 나이
    gender: Optional[str] = None
    profile_image_url: Optional[str] = None
    # 추가
    team_id: Optional[int] = None
    is_weekend_off: bool = Field(default=False)  # 주말 휴무 여부
    # 추가
    work_shifts: Optional[List[str]] = Field(
        default_factory=list,
        description="근무 가능 형태 배열. 예: ['D', 'E2', 'N1', 'MD'] 또는 ['D', 'N']",
    )
    # 원티드 설정 (간호사별 개별 설정)
    enable_nurse_pair_preference: Optional[bool] = None  # 시크릿 기능 활성화
    enable_aide: Optional[bool] = None  # AIDE 기능 활성화
    wanted_max_requests: Optional[int] = None  # 원티드 요청 개수 제한 (휴무/휴가)
    # 근무표 설정 메타 플래그 (roster_config 참조)
    show_level: bool = Field(default=True, description="직책(level_) 표시 여부")
    show_preceptor: bool = Field(
        default=True, description="프리셉터-프리셉티 관계 표시 여부"
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
    active: Optional[int] = Field(
        default=None, description="0: 비활성, 1: 활성, None: 변경 없음"
    )


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


class AddToGroupRequest(BaseModel):
    nurse_ids: List[str]
    group_id: str


class ShiftImportRequest(BaseModel):
    """근무코드 타 병동 가져오기 요청"""

    shift_ids: List[str]
    group_id: str


# 마이 페이지 테스트
class PersonnelUpdate(BaseModel):
    """마이페이지 기본 정보 수정 (이메일 + 총경력만 허용)"""

    email: Optional[EmailStr] = Field(None, description="이메일 주소")
    experience: Optional[int] = Field(None, ge=0, description="총 경력(년)")


class NurseProfileUpdate(BaseModel):
    """nurse_id 기반 단건 프로필 업데이트 (사이드 프로필 / 마이페이지)"""

    name: Optional[str] = None
    experience: Optional[int] = Field(None, ge=0)
    role: Optional[str] = None
    level_: Optional[str] = None
    grade: Optional[int] = None
    birth_date: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[EmailStr] = None
    gender: Optional[str] = None
    joining_date: Optional[datetime] = None
    resignation_date: Optional[datetime] = None
    resignation_reason: Optional[str] = None
    resignation_reason_memo: Optional[str] = None
    nurse_memo: Optional[str] = None
    is_head_nurse: Optional[bool] = None
    preceptor_id: Optional[str] = None
    fixed_shift: Optional[str] = None
    weekly_off_weekday: Optional[int] = None
    is_weekend_off: Optional[bool] = None
    is_night_nurse: Optional[List[str]] = None
    work_shifts: Optional[List[str]] = None
    enable_nurse_pair_preference: Optional[bool] = None
    enable_aide: Optional[bool] = None
    wanted_max_requests: Optional[int] = None


class PasswordChangeRequest(BaseModel):
    """비밀번호 변경 요청 (SMS 인증 후 사용)"""

    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)
    confirm_password: str = Field(..., min_length=8)
    verification_code: Optional[str] = Field(
        None, description="SMS 인증번호 (최초 요청 시 생략)"
    )


class PhoneChangeRequest(BaseModel):
    """휴대폰 번호 변경 요청"""

    new_phone_number: str = Field(
        ..., pattern=r"^01[0-9]{8,9}$", description="새 휴대폰 번호"
    )
    verification_code: Optional[str] = Field(
        None, description="인증번호 (검증 단계에서만 사용)"
    )


# Fixed Wanted (확정 원티드) 스키마
class FixedWantedEntryCreate(BaseModel):
    """확정 원티드 항목 생성 요청"""

    nurse_id: str
    shift_date: date
    shift_id: str
    is_applied: bool = True
    source_type: Optional[str] = None  # 백엔드 자동 감지 (프론트 전송 불필요)
    original_shift_id: Optional[str] = None  # 백엔드 자동 감지 (프론트 전송 불필요)
    reason: Optional[str] = None
    head_nurse_memo: Optional[str] = None


class FixedWantedCreate(BaseModel):
    """확정 원티드 저장 요청"""

    year: int
    month: int
    entries: List[FixedWantedEntryCreate]


class FixedWantedEntryResponse(BaseModel):
    """확정 원티드 항목"""

    id: int
    group_id: str
    year: int
    month: int
    nurse_id: str
    shift_date: date
    shift_id: str
    shifts_table_id: Optional[int] = None
    is_applied: bool
    source_type: str
    original_shift_id: Optional[str] = None
    reason: Optional[str] = None
    head_nurse_memo: Optional[str] = None
    created_by: Optional[str] = None

    class Config:
        from_attributes = True


class AdjustmentNurse(BaseModel):
    """원티드 조정판 - 간호사별 데이터"""

    nurse_id: str
    name: str
    entries: List[FixedWantedEntryResponse]
    monthly_summary: Dict[str, int]  # {"D": 5, "E": 3, "N": 2, "주": 4, ...}


class AdjustmentResponse(BaseModel):
    """원티드 조정판 조회 응답"""

    nurses: List[AdjustmentNurse]
    has_fixed_wanted: bool = False  # 저장된 확정 원티드 존재 여부


class FixedWantedListResponse(BaseModel):
    """확정 원티드 목록 조회 응답 (근무표 생성용)"""

    group_id: str
    year: int
    month: int
    entries: List[FixedWantedEntryResponse]
    total_count: int

    class Config:
        from_attributes = True


class ToggleEntryResponse(BaseModel):
    """항목 토글 응답"""

    id: int
    is_applied: bool
    message: str


class ScheduleShareCreateRequest(BaseModel):
    image_url: str
    title: Optional[str] = "근무표 공유"
    description: Optional[str] = "공유된 근무표입니다."
    expires_in_days: int = Field(default=3, ge=1, le=365)


class ScheduleShareAutoCreateRequest(BaseModel):
    title: Optional[str] = "근무표 공유"
    description: Optional[str] = "공유된 근무표입니다."
    expires_in_days: int = Field(default=3, ge=1, le=365)


class ScheduleShareCaptureCreateRequest(BaseModel):
    image_data_url: str
    title: Optional[str] = "근무표 공유"
    description: Optional[str] = "공유된 근무표입니다."
    expires_in_days: int = Field(default=3, ge=1, le=365)


# ── NurseAssignment (파견/휴직/퇴사/프리셉티/병동이동) ──

class NurseAssignmentCreate(BaseModel):
    """배정/상태 변경 등록 요청"""
    nurse_id: str
    source_group_id: str
    target_group_id: Optional[str] = None
    office_id: str
    start_date: date
    expected_end_date: Optional[date] = Field(default=None, description="예상 종료일 (병동이동 시 미지정 가능)")
    reason: str = Field(description="파견 / 휴직 / 퇴사 / 프리셉티 / 병동이동")


class NurseAssignmentUpdate(BaseModel):
    """배정/상태 변경 수정 요청"""
    expected_end_date: Optional[date] = None
    end_date: Optional[date] = None
    status: Optional[str] = Field(default=None, description="active / completed / cancelled")


class NurseAssignmentResponse(BaseModel):
    """배정/상태 변경 응답"""
    id: int
    nurse_id: str
    nurse_name: Optional[str] = None
    source_group_id: str
    target_group_id: Optional[str] = None
    office_id: str
    start_date: date
    expected_end_date: Optional[date] = None
    end_date: Optional[date] = None
    reason: str
    status: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
