from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from services.constraint_impact.snapshot import SemanticsSnapshot
from services.constraint_impact.types import ConstraintFamily, ConstraintMode


GraphNodeTier = Literal["hard", "soft", "risk"]


@dataclass(slots=True)
class ConstraintNode:
    node_id: str
    family: ConstraintFamily | str
    mode: ConstraintMode
    tier: GraphNodeTier
    scope: dict[str, Any]
    operator: str
    target: Any
    related_atom_ids: list[str]
    explanation_template: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConstraintEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation: str  # depends_on | pressures | seeded_by | coupled_with
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GraphExpansionState:
    nurse_index: int
    nurse_id: str
    day_index: int
    assigned_shift: str | None
    previous_shift: str | None
    consecutive_work: int
    consecutive_nights: int
    recovery_debt: float
    fatigue_score: float
    hard_violations: list[str] = field(default_factory=list)
    soft_penalties: list[dict[str, Any]] = field(default_factory=list)
    risk_flags: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PropagationResult:
    valid: bool
    hard_violations: list[str]
    soft_penalties: list[dict[str, Any]]
    risk_flags: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)


def make_expansion_state(snapshot: SemanticsSnapshot, nurse_index: int, day_index: int, day_state) -> GraphExpansionState:
    nurse = snapshot.nurses[nurse_index]
    return GraphExpansionState(
        nurse_index=nurse_index,
        nurse_id=nurse.nurse_id,
        day_index=day_index,
        assigned_shift=day_state.assigned_shift,
        previous_shift=day_state.previous_shift,
        consecutive_work=day_state.consecutive_work,
        consecutive_nights=day_state.consecutive_nights,
        recovery_debt=day_state.recovery_debt,
        fatigue_score=day_state.fatigue_score,
        notes=list(day_state.notes),
    )


def propagate_layered_constraints(*, snapshot: SemanticsSnapshot, state: GraphExpansionState) -> PropagationResult:
    hard: list[str] = []
    soft: list[dict[str, Any]] = []
    risk: list[dict[str, Any]] = []

    if bool(snapshot.config_payload.get("ban_n_to_d", True)) and state.previous_shift == "N" and state.assigned_shift == "D":
        hard.append("transition_ban:N->D")
    if bool(snapshot.config_payload.get("ban_e_to_d", True)) and state.previous_shift == "E" and state.assigned_shift == "D":
        hard.append("transition_ban:E->D")
    if bool(snapshot.config_payload.get("ban_n_to_e", True)) and state.previous_shift == "N" and state.assigned_shift == "E":
        hard.append("transition_ban:N->E")

    if state.day_index >= 0 and state.recovery_debt > 0 and state.assigned_shift not in (None, "O"):
        hard.append("recovery_debt:first_visible_day")

    max_work = int(snapshot.config_payload.get("max_consecutive_work_days") or 0)
    if max_work > 0 and state.consecutive_work > max_work:
        hard.append("consecutive_work_limit")

    max_nights = int(snapshot.config_payload.get("max_consecutive_nights") or 0)
    if max_nights > 0 and state.consecutive_nights > max_nights:
        hard.append("consecutive_night_limit")

    if state.fatigue_score >= 8.0:
        risk.append({"kind": "fatigue_risk", "score": state.fatigue_score})
        soft.append({"kind": "fatigue_penalty", "weight": state.fatigue_score})

    return PropagationResult(
        valid=not hard,
        hard_violations=hard,
        soft_penalties=soft,
        risk_flags=risk,
        notes=list(state.notes),
    )
