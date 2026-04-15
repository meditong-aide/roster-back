from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ReplacementMode(str):
    SINGLE = "SINGLE"
    BULK = "BULK"


class ReplacementSlot(BaseModel):
    date: date
    shift: str


class AbsenceWindow(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_window(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be greater than or equal to start_date")
        return self


class ReplacementRecommendOptions(BaseModel):
    allow_non_off_candidates: bool = True
    max_candidate_scan: int = Field(default=50, ge=1, le=300)
    include_explanations: bool = True
    ranking_scope: Literal[
        "ALL",
        "OFF_ONLY",
        "ON_DUTY_ONLY",
        "VACATION_ONLY",
        "ON_DUTY_OR_OFF",
        "ON_DUTY_OR_VACATION",
        "OFF_OR_VACATION",
    ] = "ALL"


class ReplacementRecommendRequest(BaseModel):
    schedule_id: str
    mode: Literal["SINGLE", "BULK"]
    target_nurse_id: str
    slots: Optional[List[ReplacementSlot]] = None
    absence_window: Optional[AbsenceWindow] = None
    top_k: int = Field(default=10, ge=1, le=50)
    options: ReplacementRecommendOptions = Field(default_factory=ReplacementRecommendOptions)

    @model_validator(mode="after")
    def validate_mode_payload(self):
        if self.mode == "SINGLE":
            if not self.slots:
                raise ValueError("slots are required when mode is SINGLE")
        if self.mode == "BULK":
            if self.absence_window is None:
                raise ValueError("absence_window is required when mode is BULK")
        return self


class CandidateScoreBreakdown(BaseModel):
    rule_safety: float
    off_priority: float
    grade_fit: float
    preference: float
    pair: float
    fairness: float
    change_cost: float
    estimated_violation_delta: float


class CandidateRecommendation(BaseModel):
    nurse_id: str
    name: str
    candidate_grade: Optional[int] = None
    current_assigned_shift_code: Optional[str] = None
    current_assigned_shift_pk_id: Optional[str] = None
    final_score: float
    rank: int
    tags: List[str] = Field(default_factory=list)
    breakdown: CandidateScoreBreakdown
    no_candidate: bool = Field(default=False, description="추천 후보가 없을 때 true. 프론트에서 버튼 disabled 처리용.")


class SlotRecommendation(BaseModel):
    slot: ReplacementSlot
    recommendation_status: Literal["OK", "LIMITED", "NONE"]
    candidates: List[CandidateRecommendation] = Field(default_factory=list)
    excluded_summary: Dict[str, int] = Field(default_factory=dict)


class ConstraintFactors(BaseModel):
    """각 step에서 적용된 제약 조건 곱연산 factor (0.0~1.0).
    1.0 = 제약 없음, 낮을수록 해당 제약에 의해 점수 감쇄됨."""
    f_violation: float = Field(default=1.0, description="위반 유형별 가중 감쇄")
    f_reuse: float = Field(default=1.0, description="path 내 동일 간호사 재사용 감쇄")
    f_back_to_back: float = Field(default=1.0, description="직전 step 동일 간호사 감쇄")
    f_nurse_repeat: float = Field(default=1.0, description="동일 간호사 다일 위반 감쇄")
    f_consecutive: float = Field(default=1.0, description="연속근무 초과 감쇄")
    f_coverage: float = Field(default=1.0, description="커버리지 부족 감쇄")
    combined: float = Field(default=1.0, description="전체 곱연산 결과")


class ExcludedCandidate(BaseModel):
    """상위 시나리오에서 선점되어 제외된 후보 정보."""
    nurse_id: str
    name: str
    original_score: float = Field(description="제외 전 원래 점수")
    excluded_by_scenario: str = Field(description="어떤 시나리오에서 선점했는지 (A/B)")


class BulkPathStep(BaseModel):
    slot: ReplacementSlot
    candidate: CandidateRecommendation
    original_shift_id: Optional[str] = Field(
        default=None,
        description="해당 일자 후보의 기존 시프트 코드 (예: Dxd, 나s, of)",
    )
    original_shift_pk: Optional[str] = Field(
        default=None,
        description="해당 일자 후보의 기존 시프트 PK (shifts 테이블 id)",
    )
    transition_score: float = Field(
        default=0.0,
        description="base_score x constraint_multiplier 적용 후 점수",
    )
    base_score: float = Field(
        default=0.0,
        description="제약 조건 적용 전 원래 점수",
    )
    constraint_factors: Optional[ConstraintFactors] = Field(
        default=None,
        description="이 step에서 적용된 제약 조건 factor 상세",
    )
    excluded_candidates: List[ExcludedCandidate] = Field(
        default_factory=list,
        description="상위 시나리오에서 선점되어 이 시나리오에서 제외된 후보 목록",
    )


class PathViolationDetail(BaseModel):
    type: str
    nurse_id: str
    nurse_name: str
    day: int
    description: str


class PathViolationSummary(BaseModel):
    total_count: int = 0
    by_type: Dict[str, int] = Field(default_factory=dict)
    details: List[PathViolationDetail] = Field(default_factory=list)


class BulkPathRecommendation(BaseModel):
    path_rank: int
    scenario_label: str = Field(
        default="",
        description="시나리오 설명 (일자별 최적/균등 분할/근무유형 분할)",
    )
    steps: List[BulkPathStep] = Field(default_factory=list)
    total_path_score: float = 0.0
    violations: Optional[PathViolationSummary] = None


class ReplacementRecommendResponse(BaseModel):
    schedule_id: str
    mode: Literal["SINGLE", "BULK"]
    target_nurse_id: str
    results: List[SlotRecommendation]
    bulk_paths: Optional[List[BulkPathRecommendation]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
