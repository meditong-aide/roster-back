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
    ranking_scope: Literal["ALL", "OFF_ONLY", "ON_DUTY_ONLY", "VACATION_ONLY"] = "ALL"


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


class SlotRecommendation(BaseModel):
    slot: ReplacementSlot
    recommendation_status: Literal["OK", "LIMITED", "NONE"]
    candidates: List[CandidateRecommendation] = Field(default_factory=list)
    excluded_summary: Dict[str, int] = Field(default_factory=dict)


class ReplacementRecommendResponse(BaseModel):
    schedule_id: str
    mode: Literal["SINGLE", "BULK"]
    target_nurse_id: str
    results: List[SlotRecommendation]
    metadata: Dict[str, Any] = Field(default_factory=dict)
