from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


RuleSource = Literal[
    "profile",
    "personal_rule",
    "transfer_overlay",
    "generated",
]


@dataclass(slots=True)
class DisallowedShiftRule:
    nurse_id: str
    day_indices: set[int]
    shift_codes: set[str]
    source: RuleSource
    reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class RequiredShiftRule:
    nurse_id: str
    day_indices: set[int]
    shift_codes: set[str]
    min_required: int = 1
    source: RuleSource = "generated"
    reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ShiftCountBoundRule:
    nurse_id: str
    shift_code: str
    min_count: int | None = None
    max_count: int | None = None
    exact_count: int | None = None
    scope_day_indices: set[int] | None = None
    source: RuleSource = "generated"
    reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class PrimitiveRuleBundle:
    disallowed: list[DisallowedShiftRule] = field(default_factory=list)
    required: list[RequiredShiftRule] = field(default_factory=list)
    count_bounds: list[ShiftCountBoundRule] = field(default_factory=list)

    def extend(self, other: "PrimitiveRuleBundle") -> None:
        self.disallowed.extend(other.disallowed)
        self.required.extend(other.required)
        self.count_bounds.extend(other.count_bounds)
