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
    off_swap_target: Optional[bool] = None  # None이면 기존 값 유지 (초과 OFF 변환 타깃)
    description: Optional[str] = None  # 근무코드 설명. None이면 기존 값 유지


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
    off_swap_target: Optional[bool] = False  # 초과 OFF 변환 타깃 (그룹당 1개)
    description: Optional[str] = None  # 근무코드 설명


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
    # 대상 병동 그룹. 미지정 시 호출자 DB home 으로 해석(토큰 group_id 에 의존하지 않음).
    # HN(그룹관리자)이 home 외 관리 그룹을 선택해 생성할 때 사용.
    group_id: Optional[str] = None
    # algorithm: str = "cp_sat"  # "cp_sat" or "random_sampling"
    config_id: Optional[int] = None
    # 모달의 현재(편집 포함) 설정 payload. 제공 시 생성 직전 materialize 로 config row 를
    # 굳히고(변경 시 '새로운 설정n') 라이브 동기화 후 config_id 를 결정한다. dict 로 받아
    # 서버에서 RosterConfigCreate 로 검증(스키마 정의 순서상 forward-ref 회피).
    config: Optional[Dict[str, Any]] = None
    grade_strategy: Optional[str] = None  # 미지정 시 DB/서버 해석 전략 사용
    preceptor_gauge: Optional[int] = Field(default=None, ge=0, le=10)
    # 고급 추론: True 시 fallback_lex 솔버 시간 60s → 180s. 빡센 케이스(인원 vs demand
    # 비대칭, GRADE 제약 다수)에서 outlier 짜내기. 프론트 옵트인.
    advanced_inference: bool = Field(default=False)
    # A1: 표는 나왔지만 hard 위반(hv>0)이 있을 때, 위반을 0으로 만드는 완화 옵션을
    # probe 로 실측해 infeasibility.resolution_options 로 함께 반환한다. 흔한 케이스라
    # 매 생성마다 돌면 느려지므로 기본 OFF(프론트 "위반 해결책 보기" 시 opt-in, async 권장).
    suggest_fixes: bool = Field(default=False)
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
    off_swap_enabled: bool = Field(
        default=False,
        description="초과 OFF 후처리 — True 시 baseline(off_days) 초과 OFF 를 shifts.off_swap_target=True 인 코드로 변환. 보호: 회복 OFF(1N/2N/3N 직후) / fixed_wanted / '주' / N전담",
    )


class RosterConfigCreate(RosterConfigBase):
    config_version: Optional[str] = None
    # ── 설정 프리셋(저장한 설정 모달) ──
    # config_id 있으면 해당 프리셋 UPDATE(in-place upsert), 없으면 신규 INSERT(version=MAX+1).
    config_id: Optional[int] = None
    config_name: Optional[str] = None  # 프리셋 이름
    config_memo: Optional[str] = None  # 간단 메모
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
        default=None,
        description="유효 종료일 ISO 문자열 (end_date 우선, 없으면 expected_end_date)",
    )
    expectedEndDate: Optional[str] = Field(
        default=None,
        description="예정 종료일 ISO 문자열 (expected_end_date 원본, 미정이면 null)",
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
    endDate: Optional[str] = Field(
        default=None,
        description="유효 종료일 ISO 문자열 (end_date 우선, 없으면 expected_end_date)",
    )
    expectedEndDate: Optional[str] = Field(
        default=None,
        description="예정 종료일 ISO 문자열 (expected_end_date 원본, 미정이면 null)",
    )
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


class PreceptorPeerAssignment(BaseModel):
    """프리셉터 사이드 프로필에 동봉되는 1건 프리셉티 nurse_assignment 메타.

    reason='프리셉티' active row 를 직접 매핑한다. PATCH 시 `assignment_id`
    를 그대로 update/cancel 식별자로 사용한다.
    """
    assignment_id: int
    start_date: date
    expected_end_date: Optional[date] = None
    end_date: Optional[date] = None
    status: str
    note: Optional[str] = None


class PreceptorPeer(BaseModel):
    """프리셉터 본인의 사이드 프로필에 N명 노출되는 프리셉티 1명 (period SSOT).

    식별 = nurse_id (assignment_id 폐기). 기간 = start_date/expected_end_date(inclusive).
    """
    nurse_id: str
    name: str
    start_date: Optional[date] = None
    expected_end_date: Optional[date] = None


class PrecepteeSelfPeriodRead(BaseModel):
    """이 간호사(프리셉티 본인)의 현재 유효 프리셉터 관계 + 기간 (사이드 프로필 폼 바인딩)."""
    preceptor_id: Optional[str] = None
    start_date: Optional[date] = None
    expected_end_date: Optional[date] = None


class NurseMembership(BaseModel):
    """근무자관리 표시용 월 기준 소속/배지 묶음(nested).

    기존 /nurses/members(group_members_in_month)가 주던 월 소속 정보를 nurse row 안에
    nested 로 제공해, 프론트가 /nurses 한 번으로 명단+월소속을 받아 memberStatusMap/
    synthInbound 합성 없이 렌더하도록 한다. 월 명단에 없는 nurse(전출 완료 등)는 membership=None.
    """
    status: Optional[str] = Field(
        default=None,
        description="월 기준 소속 상태: active/inbound/outbound/dispatch_out/leave/resigned",
    )
    marker: Optional[str] = Field(default=None, description="방향 마커(전입 →, 전출 ← 등)")
    badge: Optional[str] = Field(default=None, description="표시 배지(병동이동/파견 중/휴직/N전담 등)")
    as_of_team: Optional[int] = Field(default=None, description="해당 월 기준 소속 팀(nurse_team_period)")
    as_of_grade: Optional[int] = Field(default=None, description="해당 월 기준 직급")
    is_night_dedicated: Optional[bool] = Field(default=None, description="N전담 여부")
    display_group_id: Optional[str] = Field(
        default=None, description="이 membership 이 표시되는 기준 그룹(선택/조회 그룹)"
    )
    # 월 스코프 퇴사 정보(status=='resigned' 인 퇴사月에만 채워짐, nurses.resignation_date SSOT).
    # 다음 달부터는 명단에서 제외되어 membership 자체가 None 이므로 여기도 자연히 사라진다.
    resign_date: Optional[str] = Field(default=None, description="퇴사일(ISO, 퇴사月에만)")
    resign_reason: Optional[str] = Field(default=None, description="퇴사 사유(퇴사月에만)")
    # FE 스펙(전 상태 공통): 상태 적용 시작/종료일 + 사유. 퇴사=resignation_date, 휴직/파견/병동이동=assignment.
    status_start_date: Optional[str] = Field(default=None, description="상태 적용 시작일(ISO·UI 표시 기준). active=None")
    status_end_date: Optional[str] = Field(default=None, description="상태 종료일(ISO, 없으면 None=지속)")
    status_reason: Optional[str] = Field(default=None, description="상태 사유(퇴사/휴직/파견/병동이동/None)")


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
    # allowed_shifts: List[CodeMapp] = Field(default_factory=list, max_items = 2)
    allowed_shifts: List[str] = Field(default_factory=list)
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
    # 근무자관리 get-nurse 에 year/month 가 주어질 때만 채워지는 월별 야간 한도(nurse_monthly_limits)
    # n_exact=고정(정확값), n_max=최대(상한). 행마다 한쪽만 유효(고정 우선 해석).
    n_exact: Optional[int] = None
    n_max: Optional[int] = None
    # /members(group_members_in_month) 배지 병합 — year/month 가 함께 주어질 때만 채워진다.
    # 프론트가 /nurses 한 번으로 명단+월배지를 받도록 nurse_id 기준 부착(없으면 None).
    membership_status: Optional[str] = None
    marker: Optional[str] = None
    badge: Optional[str] = None
    as_of_team: Optional[int] = None
    as_of_grade: Optional[int] = None
    is_night_dedicated: Optional[bool] = None
    # 위 flat 6필드를 묶은 nested 표현(프론트 단일 소스용). flat 은 전환기 호환 유지.
    membership: Optional[NurseMembership] = None
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
    # 기간 설정(파견/병동이동/휴직/퇴사/프리셉티) 활성 이력.
    inbound: List["InboundEntry"] = Field(
        default_factory=list,
        description="활성 파견/병동이동/휴직/퇴사/프리셉티 이력. 없으면 [].",
    )
    # 현재 대표 1건 flat 요약 (프론트 폼 바인딩용)
    current_assignment: Optional["CurrentAssignment"] = Field(
        default=None,
        description="휴직/퇴사 > 프리셉티 > 파견/병동이동 우선, 동률 시 start_date DESC",
    )
    # 본인이 프리셉터인 경우 자기를 따르는 N명의 프리셉티. preceptee 이거나 0명이면 [].
    preceptees: List[PreceptorPeer] = Field(
        default_factory=list,
        description="본인이 preceptor 일 때 자기를 따르는 N명(period SSOT, start/end 포함).",
    )
    # 본인이 프리셉티인 경우 자기 프리셉터 관계 + 기간(현재 유효). 관계 없으면 None.
    preceptee_period: Optional[PrecepteeSelfPeriodRead] = Field(
        default=None,
        description="본인이 프리셉티일 때 프리셉터+기간(현재 유효, 사이드 프로필 폼 바인딩용).",
    )
    # 일괄 업데이트(POST /bulk-update) 시 동반 전달 가능한 배정 payload
    assignment: Optional[NurseAssignmentPayload] = Field(
        default=None,
        description="bulk 업데이트와 함께 배정(파견/병동이동/휴직/퇴사/프리셉티) create/update/cancel 수행 — 단건",
    )
    # 사이드 프로필에서 한 번에 여러 파견을 작성/수정/취소할 때 사용하는 다건 payload.
    # `assignment`(단건) 와 동시 전달되면 둘 다 처리(assignments 먼저, 그 다음 단건).
    assignments: Optional[List[NurseAssignmentPayload]] = Field(
        default=None,
        description="다건 배정 처리 — 한 간호사의 여러 파견을 한 번에 create/update/cancel",
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


class PreceptorPeerUpdate(BaseModel):
    """프리셉터 사이드 프로필 PATCH 시 preceptees N명 변경 1건 (nurse_preceptee_period 직접 write).

    대상 식별 = **target_nurse_id** (프리셉티 1:1 이라 assignment_id 불요·폐기).
    operation 별 필수:
    - create/update: target_nurse_id, start_date, **expected_end_date(종료예정일 필수 — 무기한 폐지)**
    - cancel: target_nurse_id 만
    """
    operation: Literal["create", "update", "cancel"]
    target_nurse_id: str
    assignment_id: Optional[int] = None  # deprecated(무시) — 하위호환 위해 수용만
    start_date: Optional[date] = None
    expected_end_date: Optional[date] = None
    note: Optional[str] = None


class PrecepteeSelfPeriod(BaseModel):
    """이 간호사(프로필 owner)가 **프리셉티**일 때 프리셉터+기간 지정 (preceptee-self).

    nurse_preceptee_period(SSOT) 로 직접 write — assignment 미경유.
    operation 별 필수:
    - create/update: preceptor_id, start_date, **expected_end_date(필수 — 무기한 폐지)**
    - cancel: (owner 의 현재/미래 구간 삭제)
    """
    operation: Literal["create", "update", "cancel"]
    preceptor_id: Optional[str] = None
    start_date: Optional[date] = None
    expected_end_date: Optional[date] = None


class NurseProfileUpdate(BaseModel):
    """nurse_id 기반 단건 프로필 업데이트 (사이드 프로필 / 마이페이지)"""

    name: Optional[str] = None
    experience: Optional[int] = Field(None, ge=0)
    role: Optional[str] = None
    level_: Optional[str] = None
    grade: Optional[int] = None
    team_id: Optional[int] = None
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
    weekly_off_enabled: Optional[int] = None
    weekly_off_weekday: Optional[int] = None
    weekly_off_type: Optional[str] = None
    is_weekend_off: Optional[bool] = None
    allowed_shifts: Optional[List[str]] = None
    work_shifts: Optional[List[str]] = None
    enable_nurse_pair_preference: Optional[bool] = None
    enable_aide: Optional[bool] = None
    wanted_max_requests: Optional[int] = None
    assignment: Optional[NurseAssignmentPayload] = Field(
        default=None,
        description="배정(파견/병동이동 등) create/update/cancel을 프로필 수정과 함께 수행 — 단건",
    )
    assignments: Optional[List[NurseAssignmentPayload]] = Field(
        default=None,
        description="사이드 프로필에서 여러 파견을 한 번에 create/update/cancel — 다건",
    )
    preceptees_assignment: Optional[List[PreceptorPeerUpdate]] = Field(
        default=None,
        description="프리셉터 본인 입장에서 N명 preceptees 관계를 nurse_preceptee_period 로 일괄 create/update/cancel (target_nurse_id 기반)",
    )
    preceptee_period: Optional[PrecepteeSelfPeriod] = Field(
        default=None,
        description="이 간호사가 프리셉티일 때 프리셉터+기간(start/end 필수) 지정 — nurse_preceptee_period 직접 write",
    )


class NurseMonthlyLimitItem(BaseModel):
    nurse_id: str
    group_id: str
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)

    d_min: Optional[int] = Field(default=None, ge=0)
    d_max: Optional[int] = Field(default=None, ge=0)
    d_exact: Optional[int] = Field(default=None, ge=0)

    e_min: Optional[int] = Field(default=None, ge=0)
    e_max: Optional[int] = Field(default=None, ge=0)
    e_exact: Optional[int] = Field(default=None, ge=0)

    n_min: Optional[int] = Field(default=None, ge=0)
    n_max: Optional[int] = Field(default=None, ge=0)
    n_exact: Optional[int] = Field(default=None, ge=0)

    o_min: Optional[int] = Field(default=None, ge=0)
    o_max: Optional[int] = Field(default=None, ge=0)
    o_exact: Optional[int] = Field(default=None, ge=0)


class NurseMonthlyLimitBulkUpsertRequest(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    limits: List[NurseMonthlyLimitItem]


class NightBulkApplyRequest(BaseModel):
    """나이트 개수(월 한도) 일괄 적용 — 현재 병동/월의 야간 가능 근무자 전체에
    하나의 값을 고정(n_exact) 또는 최대(n_max)로 일괄 반영."""

    group_id: str
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    kind: Literal["fixed", "max"] = Field(
        description="fixed=고정(n_exact), max=최대(n_max)"
    )
    value: int = Field(ge=0, description="전 야간가능 근무자에 적용할 나이트 개수")


class NurseMonthlyLimitWarning(BaseModel):
    code: str
    message: str
    severity: str = "warning"


class NurseMonthlyLimitMeta(BaseModel):
    target_nurse_count: int
    active_nurse_count: int
    override_ratio: float
    recommended_ratio: float = 0.30


class NurseMonthlyLimitListResponse(BaseModel):
    items: List[NurseMonthlyLimitItem]
    meta: Optional[NurseMonthlyLimitMeta] = None
    warnings: Optional[List[NurseMonthlyLimitWarning]] = None


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


class AdjustmentAssignmentWindow(BaseModel):
    """간호사의 파견/병동이동 기간 1건 (조회 병동 기준 방향 포함).

    프론트는 이 정보를 이용해 월 그리드에 파견/이동 구간을 표기한다.
    """

    reason: str = Field(description='"파견" | "병동이동"')
    direction: str = Field(
        description='caller 기준 방향. "inbound"(caller==target) | "outbound"(caller==source)'
    )
    source_group_id: str
    source_group_name: str
    target_group_id: str
    target_group_name: str
    start_date: date
    end_date: Optional[date] = None
    period_start_day: Optional[int] = Field(
        default=None, description="해당 월 가시 구간 시작 일자(1~말일)"
    )
    period_end_day: Optional[int] = Field(
        default=None, description="해당 월 가시 구간 종료 일자(1~말일)"
    )
    source_schedule_id: Optional[str] = Field(
        default=None, description="이전(source) 병동 issued schedule_id (없으면 null)"
    )
    source_shifts: Optional[Dict[str, Optional[str]]] = Field(
        default=None,
        description='이전(source) 병동의 가시 구간 일자별 shift_id 매핑. key="day"(str), value=shift_id|null',
    )
    target_schedule_id: Optional[str] = Field(
        default=None, description="변경(target) 병동 issued schedule_id (없으면 null)"
    )
    target_shifts: Optional[Dict[str, Optional[str]]] = Field(
        default=None,
        description='변경(target) 병동의 가시 구간 일자별 shift_id 매핑. key="day"(str), value=shift_id|null',
    )


class AdjustmentNurse(BaseModel):
    """원티드 조정판 - 간호사별 데이터"""

    nurse_id: str
    name: str
    entries: List[FixedWantedEntryResponse]
    monthly_summary: Dict[str, int]  # {"D": 5, "E": 3, "N": 2, "주": 4, ...}
    # 조회 병동 관할 외(파견/병동이동 상대 병동 소유) 일자.
    # 프론트는 이 일자를 저장/편집 불가(blocked)로 표기해야 한다.
    blocked_days: List[AdjustmentBlockedDay] = Field(default_factory=list)
    # 해당 간호사의 활성 파견/병동이동 구간 (inbound/outbound 양쪽 포함).
    assignments: List[AdjustmentAssignmentWindow] = Field(default_factory=list)


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
