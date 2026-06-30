"""Axis registry — fix_plan layer's user-facing lever vocabulary.

Bridges ontology families (e.g. CoverageMin, GradeMin) with operator-actionable
"axes" (e.g. night_capacity, grade_min) that map 1:1 to a config lever the
head nurse can tweak. fix_plan composes actions[] in axis units so the user
sees concrete, family-aligned recommendations like:

  - "5월 20일 N 필요 인원 1 줄이기" (axis=night_capacity)
  - "Grade A 최소 인원을 일부 일자 1→0" (axis=grade_min)

Tier comes from ontology (get_tier) so we never duplicate that mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from services.semantics.ontology import ConstraintOntology, get_default_ontology


LockType = str  # "capacity_shortage" | "eligibility_lock" | "fixed_lock" | "carryover_lock"


@dataclass(frozen=True)
class AxisDefinition:
    axis_id: str
    family: str
    lock_type: LockType
    label_ko: str
    config_lever: dict[str, Any]
    direction_hint: str  # "decrease" | "increase" | "review"
    matches_reason_codes: tuple[str, ...] = field(default_factory=tuple)
    matches_patterns: tuple[str, ...] = field(default_factory=tuple)


_AXES: tuple[AxisDefinition, ...] = (
    # ---- capacity_shortage ----
    AxisDefinition(
        axis_id="night_capacity",
        family="CoverageMin",
        lock_type="capacity_shortage",
        label_ko="일자별 N 필요 인원",
        config_lever={"path": "daily_shift_requirements", "key": "N", "action": "decrease_by_1"},
        direction_hint="decrease",
        matches_reason_codes=("GLOBAL_DAY_CAPACITY_SHORTAGE", "GLOBAL_SHIFT_ALLOWED_SHORTAGE"),
    ),
    AxisDefinition(
        axis_id="day_capacity",
        family="CoverageMin",
        lock_type="capacity_shortage",
        label_ko="일자별 D 필요 인원",
        config_lever={"path": "daily_shift_requirements", "key": "D", "action": "decrease_by_1"},
        direction_hint="decrease",
        matches_reason_codes=("GLOBAL_DAY_CAPACITY_SHORTAGE",),
    ),
    AxisDefinition(
        axis_id="evening_capacity",
        family="CoverageMin",
        lock_type="capacity_shortage",
        label_ko="일자별 E 필요 인원",
        config_lever={"path": "daily_shift_requirements", "key": "E", "action": "decrease_by_1"},
        direction_hint="decrease",
        matches_reason_codes=("GLOBAL_DAY_CAPACITY_SHORTAGE",),
    ),
    AxisDefinition(
        axis_id="mid_capacity",
        family="CoverageMin",
        lock_type="capacity_shortage",
        label_ko="MID 필요 인원",
        config_lever={"path": "daily_shift_requirements", "key": "M", "action": "decrease_by_1"},
        direction_hint="decrease",
        matches_reason_codes=("MID_REQUIRED_MISSING", "MID_DISABLED_BUT_USED"),
    ),
    AxisDefinition(
        axis_id="team_min",
        family="TeamMin",
        lock_type="capacity_shortage",
        label_ko="팀 최소 인원",
        config_lever={"path": "team_min_by_team", "key": "<team_id>", "action": "decrease_by_1"},
        direction_hint="decrease",
        matches_reason_codes=(
            "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
            "TEAM_SIZE_INSUFFICIENT",
            "TEAM_ACTIVE_MEMBERS_INSUFFICIENT",
            "TEAM_SHIFT_ALLOWED_SHORTAGE",
        ),
    ),
    AxisDefinition(
        axis_id="grade_min",
        family="GradeMin",
        lock_type="capacity_shortage",
        label_ko="등급 최소 인원",
        config_lever={"path": "grade_min", "key": "<grade>", "action": "decrease_by_1"},
        direction_hint="decrease",
        matches_reason_codes=("GRADE_MIN_SUM_EXCEEDS_NEED", "GRADE_MIN_AVAILABLE_SHORTAGE"),
    ),
    AxisDefinition(
        axis_id="grade_max",
        family="GradeMax",
        lock_type="capacity_shortage",
        label_ko="등급 상한",
        config_lever={"path": "grade_max", "key": "<grade>", "action": "increase_by_1"},
        direction_hint="increase",
        matches_reason_codes=("GRADE_MAX_SUM_BELOW_NEED", "GRADE_ANTIPAIR_FORCES_SHORTAGE"),
    ),
    AxisDefinition(
        axis_id="team_grade_handoff",
        family="TeamGradeHandoff",
        lock_type="capacity_shortage",
        label_ko="팀-등급 교차 분배",
        config_lever={"path": "team_grade_handoff", "key": "*", "action": "disable_or_soften"},
        direction_hint="review",
        matches_reason_codes=("TEAM_GRADE_INTERSECT_SHORTAGE",),
    ),
    AxisDefinition(
        axis_id="monthly_n_cap",
        family="MonthlyNightCap",
        lock_type="capacity_shortage",
        label_ko="월간 N 한도",
        config_lever={"path": "roster_config", "key": "max_nig_per_month", "action": "increase_by_1"},
        direction_hint="increase",
        matches_reason_codes=("MONTHLY_NIGHT_CAPACITY_SHORTAGE",),
    ),
    AxisDefinition(
        axis_id="off_cap",
        family="OffCap",
        lock_type="capacity_shortage",
        label_ko="월 OFF 한도",
        config_lever={"path": "roster_config", "key": "off_days", "action": "review_per_nurse"},
        direction_hint="review",
        matches_reason_codes=("FIXED_OFF_EXCEEDS_SPAN",),
    ),
    AxisDefinition(
        axis_id="consecutive_work_limit",
        family="ConsecutiveWorkLimit",
        lock_type="capacity_shortage",
        label_ko="연속근무 한도",
        config_lever={"path": "roster_config", "key": "max_conseq_work", "action": "review_only"},
        direction_hint="review",
    ),
    # ---- eligibility_lock ----
    AxisDefinition(
        axis_id="allowed_shift_mask",
        family="AllowedShiftMask",
        lock_type="eligibility_lock",
        label_ko="간호사 허용 시프트 마스크",
        config_lever={"path": "nurse.work_shifts", "key": "<nurse_id>", "action": "expand_eligible_shifts"},
        direction_hint="review",
        matches_reason_codes=("ALLOWED_SHIFTS_ISOLATES_NURSE", "TEAM_SHIFT_ALLOWED_SHORTAGE"),
        matches_patterns=("allowed_shift_mask", "n_only_vs_caps"),
    ),
    AxisDefinition(
        axis_id="role_isolation",
        family="AllowedShiftMask",
        lock_type="eligibility_lock",
        label_ko="역할/전담 고립",
        config_lever={"path": "nurse", "key": "allowed_shifts|fixed_shift", "action": "review_role_assignments"},
        direction_hint="review",
        matches_reason_codes=("ALLOWED_SHIFTS_ISOLATES_NURSE",),
        matches_patterns=("n_only_vs_caps",),
    ),
    # ---- fixed_lock ----
    AxisDefinition(
        axis_id="fixed_excess",
        family="FixedWanted",
        lock_type="fixed_lock",
        label_ko="고정 배정 과다",
        config_lever={"path": "fixed_wanted_entries", "key": "*", "action": "thin_out_some"},
        direction_hint="decrease",
        matches_reason_codes=("FIXED_ASSIGN_EXCEEDS_NEED",),
        matches_patterns=("fixed_assignment",),
    ),
    AxisDefinition(
        axis_id="fixed_violates_allowed",
        family="FixedWanted",
        lock_type="fixed_lock",
        label_ko="고정 배정 ↔ 허용 시프트 충돌",
        config_lever={"path": "fixed_wanted_entries", "key": "*", "action": "review_conflict_pairs"},
        direction_hint="review",
        matches_reason_codes=("FIXED_ASSIGN_VIOLATES_ALLOWED",),
        matches_patterns=("initial_forbidden",),
    ),
    AxisDefinition(
        axis_id="fixed_breaks_team_min",
        family="FixedWanted",
        lock_type="fixed_lock",
        label_ko="고정 배정으로 팀 최소 깨짐",
        config_lever={"path": "fixed_wanted_entries", "key": "*", "action": "rebalance_team_fixed"},
        direction_hint="review",
        matches_reason_codes=("FIXED_ASSIGN_BREAKS_TEAM_MIN",),
    ),
    # ---- carryover_lock ----
    AxisDefinition(
        axis_id="carryover_transition",
        family="BoundaryTransitionBan",
        lock_type="carryover_lock",
        label_ko="전월 경계 전이 금지",
        config_lever={"path": "carryover", "key": "transition", "action": "review_boundary"},
        direction_hint="review",
        matches_reason_codes=("PREV_MONTH_TRANSITION",),
        matches_patterns=("carryover_boundary", "carryover_transition"),
    ),
    AxisDefinition(
        axis_id="carryover_recovery",
        family="NightRecovery",
        lock_type="carryover_lock",
        label_ko="전월 N 회복 OFF",
        config_lever={"path": "carryover", "key": "recovery", "action": "must_not_relax"},
        direction_hint="review",
        matches_patterns=("carryover_recovery_2n2off_boundary", "carryover_recovery"),
    ),
    AxisDefinition(
        axis_id="carryover_consecutive_work",
        family="ConsecutiveWorkLimit",
        lock_type="carryover_lock",
        label_ko="전월 연속근무 꼬리",
        config_lever={"path": "carryover", "key": "consecutive_work", "action": "review_only"},
        direction_hint="review",
        matches_patterns=("carryover_consecutive_work",),
    ),
)


class AxisRegistry:
    def __init__(self, ontology: ConstraintOntology | None = None):
        self.ontology = ontology or get_default_ontology()
        self._by_id: dict[str, AxisDefinition] = {a.axis_id: a for a in _AXES}

    def all_axes(self) -> list[AxisDefinition]:
        return list(_AXES)

    def get(self, axis_id: str) -> AxisDefinition | None:
        return self._by_id.get(axis_id)

    def find_by_family(self, family: str) -> list[AxisDefinition]:
        return [a for a in _AXES if a.family == family]

    def axes_by_lock_type(self, lock_type: str) -> list[AxisDefinition]:
        return [a for a in _AXES if a.lock_type == lock_type]

    def tier_of(self, axis: AxisDefinition) -> str | None:
        return self.ontology.get_tier(axis.family)

    def relaxation_priority_of(self, axis: AxisDefinition) -> int | None:
        return self.ontology.get_relaxation_priority(axis.family)

    def axes_for_reasons(
        self,
        *,
        reason_codes: set[str] | None = None,
        patterns: set[str] | None = None,
    ) -> list[AxisDefinition]:
        """Return axes whose triggers match given reason_codes / conflict patterns.

        Order is stable (definition order). Caller is responsible for further
        sorting by tier / priority / magnitude.
        """
        codes = {str(c).upper() for c in (reason_codes or set())}
        pats = {str(p).lower() for p in (patterns or set())}
        out: list[AxisDefinition] = []
        for a in _AXES:
            hit = False
            for rc in a.matches_reason_codes:
                if rc.upper() in codes:
                    hit = True
                    break
            if not hit:
                for pat in a.matches_patterns:
                    if any(pat in p for p in pats):
                        hit = True
                        break
            if hit:
                out.append(a)
        return out

    def sort_for_user(
        self,
        axes: list[AxisDefinition],
    ) -> list[AxisDefinition]:
        """Sort axes for user-facing fix_plan: T0 first (must-fix-data),
        then T2 by relaxation_priority asc (easier to relax first), then
        T1 (protected, last - typically excluded from actions)."""
        tier_order = {"T0": 0, "T2": 1, "T3": 2, "T1": 3, None: 4}

        def sort_key(a: AxisDefinition):
            tier = self.tier_of(a)
            prio = self.relaxation_priority_of(a) or 5
            return (tier_order.get(tier, 4), prio, a.axis_id)

        return sorted(axes, key=sort_key)


@lru_cache(maxsize=1)
def get_default_axis_registry() -> AxisRegistry:
    return AxisRegistry()
