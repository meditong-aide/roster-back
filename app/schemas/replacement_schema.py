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
        default=True,
        description=(
            "결원 칸을 메우는 수정안(1인 스왑 / 다인 연쇄 / 인접일 재배치)을 함께 계산할지. "
            "SINGLE 모드 전용. 판정 재료는 `enforce_hard_rules` 가 이미 적재하므로 "
            "추가 로드 비용이 없다. 끄면 `chain_results` 가 null 로 나간다."
        ),
    )
    chain_proposal_limit: int = Field(
        default=10, ge=1, le=50, description="슬롯당 반환할 수정안 개수 상한",
    )
    search_scope: Literal["SINGLE_DAY", "WIDE"] = Field(
        default="SINGLE_DAY",
        description=(
            "수정안을 어디까지 찾을지. 기본은 변경이 가장 작은 SINGLE_DAY 이며, "
            "결과가 없거나 부족하면 사용자가 넓힌다.\n"
            "- SINGLE_DAY(기본): 결원 **당일 한 열만**. 최대 4칸. CP-SAT 을 호출하지 않는다. "
            "나이트 결원은 1열로 원리적으로 안 풀려(1N 금지 탓에 최소 2일 블록 필요) "
            "차선안 비중이 커진다.\n"
            "- WIDE: 결원일~**+3일**을 열고 최대 **12칸**. 날짜를 더 여는 것은 실측상 "
            "효과가 없었고(+5일·+7일 모두 0건 추가), 병목은 'N 을 2연속으로 묶을 "
            "여유 칸' 이었다.\n"
            "중간 단계(+3일·6칸)는 뒀다가 없앴다 — 전수 33,250건에서 해결률이 12칸과 "
            "똑같이 96.0% 였고 조건 준수만 272건 적었다.\n"
            "전수 실측(2026년 87개 근무표 33,250건) — SINGLE_DAY 해결 94.4%"
            "(준수 28,077·차선 3,313·없음 1,860) / WIDE 96.0%(준수 29,670·차선 2,251·없음 1,329)."
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

    `moves` 를 순서대로 적용하면 결원이 메워진다. `hard_warnings` 가 비어 있으면
    솔버가 하드로 거는 규칙을 원본 근무표 대비 새로 깨뜨리지 않는다는 뜻이고,
    비어 있지 않으면 그 위반을 감수하는 차선안이다. 적용은 기존 `POST /save` 로 한다.
    """

    rank: int
    kind: Literal["ABSENCE_ONLY", "SINGLE_SWAP", "CHAIN", "LNS"] = Field(
        description=(
            "ABSENCE_ONLY=대체 불필요(빼도 인원이 요구치를 만족), "
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
    hard_warnings: List[str] = Field(
        default_factory=list,
        description=(
            "이 안이 새로 깨뜨리는 **하드** 근무규칙. 비어 있으면 조건을 다 지키는 안이다. "
            "비어 있지 않은 안은 조건을 지키는 안이 하나도 없을 때만 나오는 차선안이며, "
            "화면에서 접힌 영역에 따로 모아 사용자가 보고 고르게 한다. "
            "인원·등급·팀 같은 구조적 제약은 여기 절대 오지 않는다 — 그건 항상 배제된다."
        ),
    )


class SlotChainRecommendation(BaseModel):
    """결원 한 칸에 대한 수정안. **조건을 지키는 안과 아닌 안을 분리해서** 준다.

    섞어 두면 화면에서 구분이 안 돼 위반이 있는 안을 그냥 추천으로 오인한다.
    `proposals` 가 비어 있다는 것 자체가 "조건을 지키는 방법이 없다" 는 신호다.
    """

    slot: ReplacementSlot
    proposals: List[ChainProposal] = Field(
        default_factory=list,
        description="솔버 하드 규칙을 하나도 새로 깨뜨리지 않는 안. 이것만 정식 추천이다.",
    )
    fallback_proposals: List[ChainProposal] = Field(
        default_factory=list,
        description=(
            "`proposals` 가 비었을 때만 채워지는 차선안. 각 안의 `hard_warnings` 에 "
            "무엇을 감수하는지 들어 있다. 화면에서는 접힌 영역에 따로 모아 "
            "사용자가 보고 고르게 한다 — 정식 추천과 같은 목록에 두지 않는다."
        ),
    )
    blocked_reason: Optional[str] = Field(
        default=None,
        description=(
            "둘 다 비었을 때만 채워지는 한 줄 사유. 그날 근무별 인원 현황을 담는다. "
            "빈 화면 대신 왜 불가인지 보여줘 다음 조치(연장근무·타 병동 지원)를 "
            "판단할 수 있게 한다."
        ),
    )
    blocked_detail: Dict[str, int] = Field(
        default_factory=dict,
        description="후보가 걸린 사유별 인원 수. 무엇을 풀면 대체가 가능해지는지 가리킨다.",
    )


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
