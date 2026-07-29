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
    enforce_hard_rules: bool = Field(
        default=True,
        description=(
            "솔버가 하드로 거는 규칙을 위반하는 후보를 목록에서 제외할지. "
            "기본 켜짐 — 끄면 과거처럼 위반 후보가 감점만 된 채 추천에 남는다. "
            "장애 시 되돌릴 수 있도록 남겨 둔 스위치이며 평상시 끄지 않는다."
        ),
    )
    include_chain_proposals: bool = Field(
        default=False,
        description=(
            "결원 칸을 메우는 수정안(1인 스왑 / 다인 연쇄)을 함께 계산할지. "
            "판정 재료를 추가로 로드하므로 필요할 때만 켠다. SINGLE 모드 전용."
        ),
    )
    chain_proposal_limit: int = Field(
        default=10, ge=1, le=50, description="슬롯당 반환할 수정안 개수 상한",
    )
    include_lns_fallback: bool = Field(
        default=False,
        description=(
            "1단(당일 1열)으로 수정안이 안 나올 때 인접 일자까지 열어 다시 푸는 2단. "
            "나이트 결원용이며 **12~18초** 걸린다. 화면에 진행 표시가 있을 때만 켠다."
        ),
    )
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


class ChainMove(BaseModel):
    """근무표에서 셀 하나를 바꾸는 단위 이동. 프론트는 이걸 그리드에 반영한다."""

    nurse_id: str
    name: str
    date: date
    from_shift: str = Field(description="현재 근무코드")
    to_shift: str = Field(description="바꿀 근무코드")
    is_absence: bool = Field(
        default=False,
        description="결원 당사자를 비우는 이동인지. true 면 사용자가 요청한 결원 처리 자체다.",
    )


class ChainProposal(BaseModel):
    """결원 한 칸에 대한 수정안 하나.

    `moves` 를 순서대로 적용하면 결원이 메워지며, 솔버가 하드로 거는 규칙을
    원본 근무표 대비 새로 깨뜨리지 않는다. 적용은 기존 `POST /save` 로 한다.
    """

    rank: int
    kind: Literal["SINGLE_SWAP", "CHAIN", "LNS"] = Field(
        description=(
            "SINGLE_SWAP=대체인력 1명, CHAIN=여러 명이 같은 날 연쇄 이동, "
            "LNS=인접 일자까지 함께 재배치(1단으로 못 푸는 나이트 결원용)"
        ),
    )
    participant_count: int = Field(description="결원자를 뺀 이동 인원 수")
    changed_cell_count: int = Field(description="결원 처리를 포함해 바뀌는 셀 수")
    score: float = Field(description="소프트 점수. 낮을수록 우선.")
    moves: List[ChainMove] = Field(default_factory=list)
    soft_warnings: List[str] = Field(
        default_factory=list,
        description="솔버가 소프트로 두는 규칙의 신규 위반. 배제 사유가 아니라 참고용.",
    )


class SlotChainRecommendation(BaseModel):
    slot: ReplacementSlot
    proposals: List[ChainProposal] = Field(default_factory=list)


class ReplacementRecommendResponse(BaseModel):
    schedule_id: str
    mode: Literal["SINGLE", "BULK"]
    target_nurse_id: str
    results: List[SlotRecommendation]
    bulk_paths: Optional[List[BulkPathRecommendation]] = None
    chain_results: Optional[List[SlotChainRecommendation]] = Field(
        default=None,
        description="options.include_chain_proposals=true 일 때만 채워진다.",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)
