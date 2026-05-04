from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from services.constraint_impact.types import ConstraintFamily, ConstraintMode, SolveAttemptLabel


@dataclass(slots=True)
class SolveAttemptMeta:
    attempt_index: int
    label: SolveAttemptLabel
    grade_strategy: str
    forced_grade_soft_fallback: bool
    config_flags: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NurseFact:
    nurse_index: int
    nurse_id: str
    name: str
    team_id: str | None
    grade: int | None
    is_weekend_off: bool
    allowed_shift_codes: set[str]
    preceptor_id: str | None
    is_inbound: bool
    join_day: int
    leave_day: int


@dataclass(slots=True)
class FixedCellFact:
    nurse_index: int
    nurse_id: str
    day_index: int
    shift_code_raw: str
    shift_code_main: str
    shift_type: str | None
    fixed_source: str
    counts_to_coverage: bool


@dataclass(slots=True)
class PrecepteeFact:
    nurse_index: int
    nurse_id: str
    preceptor_index: int | None
    preceptor_id: str | None
    follow_enabled: bool
    follow_days: set[int]
    full_month_default_follow: bool
    counts_to_coverage: bool
    fixed_wanted_override_days: set[int] = field(default_factory=set)


@dataclass(slots=True)
class AssignmentWindowFact:
    nurse_id: str
    direction: Literal["inbound", "outbound", "transfer", "leave", "training"]
    source_group_id: str | None
    target_group_id: str | None
    reason: str
    active_day_indices: set[int]
    inactive_day_indices: set[int]
    allowed_shift_codes: set[str]
    carries_state: bool
    counts_to_coverage: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CarryoverStateArtifact:
    nurse_id: str
    direction: Literal["inbound", "outbound", "transfer", "training"]
    boundary_day_index: int
    reference_group_id: str | None
    selected_schedule_id: str | None
    selected_schedule_basis: Literal["issued", "latest", "blank"]
    carries_state: bool
    tail_sequence: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PreflightAlertFact:
    source: Literal["feasibility_alerts", "mid_feasibility"]
    severity: Literal["warning", "blocking"]
    code: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConstraintModeFact:
    family: ConstraintFamily
    key: str
    configured_mode: str
    effective_mode: ConstraintMode
    source_file: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SemanticsSnapshot:
    year: int
    month: int
    attempt: SolveAttemptMeta
    shift_types: list[str]
    config_payload: dict[str, Any]

    nurse_ids_in_scope: list[str]
    inbound_nurse_ids: list[str]
    nurses: list[NurseFact]
    assignment_windows: list[AssignmentWindowFact]
    carryover_artifacts: list[CarryoverStateArtifact]
    fixed_cells: list[FixedCellFact]
    special_fixed_requests: list[dict[str, Any]]
    merged_initial_constraints: dict[str, Any]

    join: list[int]
    leave: list[int]
    active_days_by_nurse: dict[int, set[int]]
    blocked_by_nurse: dict[int, set[int]]

    fixed_wanted_cells: set[tuple[int, int]]
    fixed_type_by_cell: dict[tuple[int, int], str | None]
    coverage_exclude_cells: set[tuple[int, int]]

    vacation_off_cells: set[tuple[int, int]]
    structural_off_cells: set[tuple[int, int]]
    forced_off_cap_excluded: set[tuple[int, int]]
    off_exception_cells: set[tuple[int, int]]
    off_exception_vacation_cells: set[tuple[int, int]]
    weekend_days: set[int]

    n_forbid_n: set[int]
    preceptee_facts: list[PrecepteeFact]

    preflight_alerts: list[PreflightAlertFact]
    mid_feasibility_error: str | None
    constraint_modes: list[ConstraintModeFact]
