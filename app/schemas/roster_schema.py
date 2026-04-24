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
    off_first: bool = Field(
        default=False,
        description="OFF 우선 모드(False, default): OFF cap 충족 우선 + max coverage 미부과 + 남는 셀 근무 배정 + fixed_wanted O 차감 / 근무 우선 모드(True): 현행 min-max coverage 균등화 유지",
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


# ── NurseAssignment.reason 입력 정규화 헬퍼 ─────────────────
# 프론트/구버전 호환: '부서이동' → 정식 '병동이동' 으로 교정.
# 빈 문자열/None 은 그대로 통과시킨다 (상위에서 required 검증).
def _normalize_assignment_reason(v):
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "부서이동":
            return "병동이동"
        return s
    return v


class InboundEntry(BaseModel):
    """활성 파견/병동이동/휴직/퇴사/프리셉티 1건에 대한 프론트 노출용 엔트리.

    관리(수정/취소)에 필요한 assignment_id / source_group_id / status 및
    target_* overlay 값을 포함한다. PATCH /nurses/{nurse_id}의 assignment payload에서
    operation='update'|'cancel'을 호출하려면 여기 `id`(assignment_id)를 사용한다.
    휴직/퇴사/프리셉티는 target_group_id가 NULL일 수 있다.
    """

    id: Optional[int] = Field(default=None, description="nurse_assignment.id")
    status: Optional[str] = Field(
        default=None, description="active / completed / cancelled"
    )
    reason: str = Field(
        description="'파견' / '병동이동' / '휴직' / '퇴사' / '프리셉티'"
    )
    startDate: Optional[str] = Field(default=None, description="시작일 ISO 문자열")
    endDate: Optional[str] = Field(
        default=None, description="예정 종료일 ISO 문자열 (미정이면 null)"
    )
    source_group_id: Optional[str] = Field(
        default=None, description="source 그룹 ID (생성/수정 권한 판정용)"
    )
    target_group_id: Optional[str] = Field(
        default=None, description="target 그룹 ID (휴직/퇴사/프리셉티는 null)"
    )
    target_group_name: str = Field(default="", description="target 그룹명")
    office_id: Optional[str] = Field(
        default=None, description="office 경계 검증용"
    )
    note: Optional[str] = Field(
        default=None, description="assignment별 메모 (수간호사 기록용, 선택)"
    )
    # target 그룹 overlay 현재값 (수정 화면 프리필용; 파견/병동이동에서만 의미)
    target_weekly_off_type: Optional[str] = None
    target_weekly_off_enabled: Optional[int] = None
    target_weekly_off_weekday: Optional[int] = None
    target_shift_types: Optional[list] = None
    target_team_id: Optional[int] = None
    target_grade: Optional[int] = None
    target_fixed_shift: Optional[str] = None
    target_wanted_max_requests: Optional[int] = None


class CurrentAssignment(BaseModel):
    """간호사의 '현재 대표' assignment 요약 — 프론트 폼 바인딩/상태 표시용.

    우선순위: 휴직/퇴사 > 프리셉티 > 파견/병동이동, 동률 시 start_date DESC.
    assignment가 없으면 응답에서 null.
    """

    id: Optional[int] = Field(default=None, description="nurse_assignment.id")
    status: Optional[str] = Field(default=None, description="active 등")
    reason: Optional[str] = Field(
        default=None, description="'파견' / '병동이동' / '휴직' / '퇴사' / '프리셉티'"
    )
    startDate: Optional[str] = Field(default=None, description="시작일 ISO 문자열")
    endDate: Optional[str] = Field(default=None, description="종료/예정 종료 ISO 문자열")
    source_group_id: Optional[str] = None
    target_group_id: Optional[str] = None
    target_group_name: Optional[str] = None
    note: Optional[str] = Field(default=None, description="assignment별 메모")


class NurseAssignmentPayload(BaseModel):
    """사이드 프로필 업데이트와 함께 전달되는 배정(파견/병동이동/휴직/프리셉티) payload.

    operation:
      - 'create': 신규 배정 등록 (reason/start_date/target_group_id 등 필수)
      - 'update': 기존 배정 수정 (assignment_id 필수)
      - 'cancel': 기존 배정 취소 (assignment_id 필수)
    """

    operation: str = Field(description="'create' | 'update' | 'cancel'")
    assignment_id: Optional[int] = Field(default=None, description="update/cancel 시 필수")
    reason: Optional[str] = Field(default=None, description="파견/휴직/퇴사/프리셉티/병동이동")
    source_group_id: Optional[str] = None
    target_group_id: Optional[str] = None
    office_id: Optional[str] = None
    start_date: Optional[date] = None
    expected_end_date: Optional[date] = None
    end_date: Optional[date] = None
    status: Optional[str] = None
    note: Optional[str] = Field(default=None, description="assignment별 메모 (수간호사 기록용)")
    target_weekly_off_type: Optional[str] = None
    target_weekly_off_enabled: Optional[int] = None
    target_weekly_off_weekday: Optional[int] = None
    target_shift_types: Optional[list] = None
    target_team_id: Optional[int] = None
    target_grade: Optional[int] = None
    target_fixed_shift: Optional[str] = None
    target_wanted_max_requests: Optional[int] = None

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, v):
        return _normalize_assignment_reason(v)


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
    # Target 병동 시점 구분용 (파견/병동이동 inbound 간호사는 True)
    is_inbound: bool = Field(
        default=False,
        description="호출자 병동이 target(파견/병동이동 수신측)일 때 True",
    )
    # 기간 설정(파견/병동이동/휴직/퇴사/프리셉티) 활성 이력 (source/target 양쪽에서 동일 표시)
    inbound: List["InboundEntry"] = Field(
        default_factory=list,
        description="활성 파견/병동이동/휴직/퇴사/프리셉티 이력 배열. 없으면 빈 배열 []",
    )
    # 현재 대표 1건 flat 요약 (프론트 폼 바인딩용)
    current_assignment: Optional["CurrentAssignment"] = Field(
        default=None,
        description="휴직/퇴사 > 프리셉티 > 파견/병동이동 우선, 동률 시 start_date DESC",
    )
    # 일괄 업데이트(POST /bulk-update) 시 동반 전달 가능한 배정 payload
    assignment: Optional[NurseAssignmentPayload] = Field(
        default=None,
        description="bulk 업데이트와 함께 배정(파견/병동이동/휴직/퇴사/프리셉티) create/update/cancel 수행",
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
    assignment: Optional[NurseAssignmentPayload] = Field(
        default=None,
        description="배정(파견/병동이동 등) create/update/cancel을 프로필 수정과 함께 수행",
    )


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


class AdjustmentBlockedDay(BaseModel):
    """원티드 조정 화면에서 저장/편집 불가로 표시해야 하는 일자 1건.

    reason 값:
    - "outside_dispatch_window": 파견 인바운드 간호사의 파견기간 밖 일자 (원 소속 담당)
    - "dispatched_elsewhere": 소속 간호사가 타병동 파견중인 기간
    """

    day: int = Field(description="1~31, 프론트 컬럼 인덱스 즉시 매칭용")
    date: date
    reason: str


class AdjustmentNurse(BaseModel):
    """원티드 조정판 - 간호사별 데이터"""

    nurse_id: str
    name: str
    entries: List[FixedWantedEntryResponse]
    monthly_summary: Dict[str, int]  # {"D": 5, "E": 3, "N": 2, "주": 4, ...}
    # 조회 병동 관할 외(파견/병동이동 상대 병동 소유) 일자.
    # 프론트는 이 일자를 저장/편집 불가(blocked)로 표기해야 한다.
    blocked_days: List[AdjustmentBlockedDay] = Field(default_factory=list)


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
    note: Optional[str] = Field(default=None, description="assignment별 메모 (수간호사 기록용)")
    # target 그룹 전용 설정
    target_weekly_off_type: Optional[str] = None
    target_weekly_off_enabled: Optional[int] = None
    target_weekly_off_weekday: Optional[int] = None
    target_shift_types: Optional[list] = Field(default=None, description="target 그룹 근무 가능 shift ID 목록")
    target_team_id: Optional[int] = None
    target_grade: Optional[int] = None
    target_fixed_shift: Optional[str] = None
    target_wanted_max_requests: Optional[int] = None

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, v):
        return _normalize_assignment_reason(v)


class NurseAssignmentUpdate(BaseModel):
    """배정/상태 변경 수정 요청"""
    # 식별/기간/사유 (단건 endpoint 1개로 전경로 커버)
    start_date: Optional[date] = None
    expected_end_date: Optional[date] = None
    end_date: Optional[date] = None
    status: Optional[str] = Field(default=None, description="active / completed / cancelled")
    reason: Optional[str] = Field(default=None, description="파견 / 휴직 / 퇴사 / 프리셉티 / 병동이동")
    target_group_id: Optional[str] = Field(default=None, description="파견/병동이동 target 교체 시 지정")
    note: Optional[str] = Field(default=None, description="assignment별 메모 (수간호사 기록용)")
    # target 그룹 전용 설정
    target_weekly_off_type: Optional[str] = None
    target_weekly_off_enabled: Optional[int] = None
    target_weekly_off_weekday: Optional[int] = None
    target_shift_types: Optional[list] = None
    target_team_id: Optional[int] = None
    target_grade: Optional[int] = None
    target_fixed_shift: Optional[str] = None
    target_wanted_max_requests: Optional[int] = None

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, v):
        return _normalize_assignment_reason(v)


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
    note: Optional[str] = None
    # target 그룹 전용 설정
    target_weekly_off_type: Optional[str] = None
    target_weekly_off_enabled: Optional[int] = None
    target_weekly_off_weekday: Optional[int] = None
    target_shift_types: Optional[list] = None
    target_team_id: Optional[int] = None
    target_grade: Optional[int] = None
    target_fixed_shift: Optional[str] = None
    target_wanted_max_requests: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AssignmentStatusCounts(BaseModel):
    """assignment status 별 카운트 (필터와 무관한 전체 집계)."""

    active: int = 0
    completed: int = 0
    cancelled: int = 0
    on_hold: int = 0


class NurseAssignmentListResponse(BaseModel):
    """`GET /nurses/assignments` 리스트 응답 래퍼.

    - items: 현재 status 필터가 적용된 실제 레코드 목록
    - counts: 동일 범위(office/group/nurse)에서 status 별 전체 카운트
    - total: counts 합계 (필터 무관 전체 건수)
    - applied_status: 현재 적용된 status 필터 값 ("active" / "all" / 등)
    """

    items: List[NurseAssignmentResponse]
    counts: AssignmentStatusCounts
    total: int = 0
    applied_status: Optional[str] = None
