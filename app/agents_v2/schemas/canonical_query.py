"""Canonical query — the normalised intermediate representation of a user request."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Scope(str, Enum):
    WANTED_SUBMISSIONS = "wanted_submissions"
    WANTED_ADJUSTMENT = "wanted_adjustment"
    DRAFT_SCHEDULE = "draft_schedule"
    PUBLISHED_SCHEDULE = "published_schedule"
    SHIFT_DEFINITIONS = "shift_definitions"
    GENERATION_JOB = "generation_job"
    NURSE_INFO = "nurse_info"
    CONSTRAINT_CONFIG = "constraint_config"


class Subject(str, Enum):
    PERSON = "person"
    REQUEST_ROW = "request_row"
    SCHEDULE_ROW = "schedule_row"
    SHIFT_CODE = "shift_code"
    GENERATION_JOB = "generation_job"
    CONSTRAINT = "constraint"
    TEAM = "team"


class Operation(str, Enum):
    LIST = "list"
    COUNT = "count"
    SUMMARIZE = "summarize"
    COMPARE = "compare"
    VALIDATE = "validate"
    BULK_UPDATE = "bulk_update"
    SINGLE_UPDATE = "single_update"
    GENERATE = "generate"
    EXPLAIN = "explain"
    RECOMMEND = "recommend"
    CANCEL = "cancel"


@dataclass
class GroundedPredicate:
    """A predicate whose meaning has been grounded via tool lookup."""
    name: str  # e.g. "shift_code_in", "is_nonworking", "matches_grade"
    arguments: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


@dataclass
class MutationAction:
    action: str  # e.g. "set_field", "cancel_request", "generate_schedule_async"
    target_field: str | None = None
    target_value: Any = None
    followup_actions: list[str] = field(default_factory=list)


@dataclass
class SlotStatus:
    filled: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)


@dataclass
class CanonicalQuery:
    """Normalised representation of a user scheduling request."""

    scope: Scope | None = None
    subject: Subject | None = None
    operation: Operation | None = None

    # Grounded meaning
    grounded_predicate: GroundedPredicate | None = None

    # Filters
    group_id: str | None = None
    ward_id: str | None = None
    year: int | None = None
    month: int | None = None
    date: str | None = None  # specific date if applicable
    date_range_start: str | None = None
    date_range_end: str | None = None
    nurse_ids: list[str] = field(default_factory=list)
    shift_codes: list[str] = field(default_factory=list)
    schedule_id: str | None = None

    # Mutation (for write operations)
    mutation: MutationAction | None = None

    # Aggregation
    aggregation_unit: str | None = None  # "person", "person_day", "date", "team"
    sort_by: str | None = None
    sort_order: str = "desc"
    limit: int | None = None

    # Slot tracking
    slot_status: SlotStatus = field(default_factory=SlotStatus)

    # Confidence
    confidence: float = 0.0

    # Notes for traceability
    notes: list[str] = field(default_factory=list)

    # ── helpers ──────────────────────────────────────────────

    @property
    def has_missing_slots(self) -> bool:
        return len(self.slot_status.missing) > 0

    @property
    def has_ambiguity(self) -> bool:
        return len(self.slot_status.ambiguous) > 0

    @property
    def is_mutation(self) -> bool:
        return self.operation in (
            Operation.BULK_UPDATE,
            Operation.SINGLE_UPDATE,
            Operation.GENERATE,
            Operation.CANCEL,
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "scope": self.scope.value if self.scope else None,
            "subject": self.subject.value if self.subject else None,
            "operation": self.operation.value if self.operation else None,
            "group_id": self.group_id,
            "year": self.year,
            "month": self.month,
            "date": self.date,
            "nurse_ids": self.nurse_ids,
            "shift_codes": self.shift_codes,
            "schedule_id": self.schedule_id,
            "confidence": self.confidence,
            "notes": self.notes,
        }
        if self.grounded_predicate:
            d["grounded_predicate"] = {
                "name": self.grounded_predicate.name,
                "arguments": self.grounded_predicate.arguments,
                "evidence": self.grounded_predicate.evidence,
            }
        if self.mutation:
            d["mutation"] = {
                "action": self.mutation.action,
                "target_field": self.mutation.target_field,
                "target_value": self.mutation.target_value,
                "followup_actions": self.mutation.followup_actions,
            }
        d["slot_status"] = {
            "filled": self.slot_status.filled,
            "missing": self.slot_status.missing,
            "ambiguous": self.slot_status.ambiguous,
        }
        return d
