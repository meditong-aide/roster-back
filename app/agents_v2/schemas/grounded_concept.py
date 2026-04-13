"""Grounded concept — the output of a grounding step."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConceptType(str, Enum):
    LEXICAL = "lexical"
    TEMPORAL = "temporal"
    STATUS = "status"
    WORKFLOW = "workflow"
    ENTITY = "entity"


class GroundingStatus(str, Enum):
    GROUNDED = "grounded"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


@dataclass
class GroundingCandidate:
    value: Any
    confidence: float
    evidence: list[str] = field(default_factory=list)


@dataclass
class GroundedConcept:
    """Result of grounding a single surface expression."""

    concept_type: ConceptType
    input_text: str
    status: GroundingStatus = GroundingStatus.UNRESOLVED

    # Populated when status == GROUNDED
    grounded_value: Any = None

    # Evidence trail for traceability
    evidence: list[str] = field(default_factory=list)

    # Populated when status == AMBIGUOUS
    candidates: list[GroundingCandidate] = field(default_factory=list)

    # Clarification
    requires_clarification: bool = False
    clarification_question: str | None = None

    # ── helpers ──────────────────────────────────────────────

    @property
    def is_grounded(self) -> bool:
        return self.status == GroundingStatus.GROUNDED

    @property
    def is_ambiguous(self) -> bool:
        return self.status == GroundingStatus.AMBIGUOUS

    def to_dict(self) -> dict:
        return {
            "concept_type": self.concept_type.value,
            "input_text": self.input_text,
            "status": self.status.value,
            "grounded_value": self.grounded_value,
            "evidence": self.evidence,
            "candidates": [
                {"value": c.value, "confidence": c.confidence, "evidence": c.evidence}
                for c in self.candidates
            ],
            "requires_clarification": self.requires_clarification,
            "clarification_question": self.clarification_question,
        }
