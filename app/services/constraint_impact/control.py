"""Agent-facing control layer for constraint module/instance adjustments.

이 모듈은 ontology 에 정의된 constraint family 를 module 단위로 보고,
에이전트가 disable / force_soft / set_threshold / narrow_scope 4 종 액션으로
조절할 수 있게 해 준다. 적용은 솔버 input config_dict 수정으로만 이루어진다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable

from services.constraint_impact.atoms import build_assignment_atoms
from services.constraint_impact.graph_builder import build_constraint_nodes
from services.constraint_impact.snapshot import SemanticsSnapshot
from services.semantics.ontology import ConstraintOntology, get_default_ontology


SUPPORTED_ACTIONS = ("disable_module", "force_soft_mode", "set_threshold", "narrow_scope")


@dataclass(slots=True)
class ConstraintAdjustment:
    family: str
    action: str
    scope_filter: dict[str, Any] = field(default_factory=dict)
    target_value: Any = None
    reason: str | None = None

    def normalized_family(self, ontology: ConstraintOntology) -> str:
        resolved = ontology.resolve_alias(self.family)
        if resolved is None:
            raise ValueError(f"unknown ontology family: {self.family!r}")
        return resolved

    def validate_action(self) -> None:
        if self.action not in SUPPORTED_ACTIONS:
            raise ValueError(
                f"unsupported action: {self.action!r}; expected one of {SUPPORTED_ACTIONS}"
            )


@dataclass(slots=True)
class ModuleSummary:
    family: str
    label: str
    parent: str
    scope: list[str]
    effective_modes: list[str]
    current_mode_estimate: str
    instance_count_estimate: int
    supported_actions: list[str]


@dataclass(slots=True)
class InstanceView:
    node_id: str
    family: str
    mode: str
    scope: dict[str, Any]
    operator: str
    target: Any
    explanation: str


# ---- supported adjustment matrix ---------------------------------------------------

_FAMILY_ACTION_MATRIX: dict[str, set[str]] = {
    "TeamMin": {"disable_module", "force_soft_mode", "set_threshold", "narrow_scope"},
    "GradeMin": {"disable_module", "force_soft_mode"},
    "GradeMax": {"disable_module", "force_soft_mode"},
    "TeamGradeHandoff": {"force_soft_mode"},
    "CoverageMin": {"set_threshold"},
    "BoundaryTransitionBan": {"disable_module"},
    "ConsecutiveWorkLimit": {"set_threshold"},
    "ConsecutiveNightLimit": {"set_threshold"},
    "NightRecovery": {"disable_module"},
    "MonthlyNightCap": {"set_threshold"},
    "WeekendOffOnly": {"disable_module"},
    "BanNightBeforeFixedOff": {"disable_module"},
    # OffWindow는 derived 제약 (NightRecovery + ConsecutiveWorkLimit + 월경계).
    # 직접 조절 action 없음 — upstream family 를 조절하면 자동 제거됨.
    "OffWindow": set(),
}


def supported_actions_for(family: str) -> list[str]:
    return sorted(_FAMILY_ACTION_MATRIX.get(family, set()))


# ---- read-side --------------------------------------------------------------------


def _estimate_days(snapshot: SemanticsSnapshot) -> int:
    days = len(snapshot.join)
    if days > 0:
        return days
    max_day = 0
    for s in snapshot.active_days_by_nurse.values():
        if s:
            max_day = max(max_day, max(s) + 1)
    return max_day


def _estimate_team_min_instances(snapshot: SemanticsSnapshot) -> int:
    cfg = snapshot.config_payload.get("team_min_by_team") or {}
    days = _estimate_days(snapshot)
    count = 0
    for _team, mins in cfg.items():
        for _shift, val in (mins or {}).items():
            if int(val or 0) > 0:
                count += days
    return count


def _estimate_grade_instances(snapshot: SemanticsSnapshot) -> int:
    grade_cfg = snapshot.config_payload.get("grade_config") or {}
    constraints_map = grade_cfg.get("constraints") or grade_cfg.get("constraints_json") or {}
    days = _estimate_days(snapshot)
    instances = 0
    for _shift, by_grade in (constraints_map or {}).items():
        for _grade in (by_grade or {}).keys():
            instances += days
    return instances


def _estimate_coverage_instances(snapshot: SemanticsSnapshot) -> int:
    by_day = snapshot.config_payload.get("daily_shift_requirements_by_day")
    if isinstance(by_day, list):
        return sum(1 for d in by_day if isinstance(d, dict) for s in d if s != "O")
    base = snapshot.config_payload.get("daily_shift_requirements") or {}
    days = _estimate_days(snapshot)
    return len([s for s in base if s != "O"]) * days


def _module_mode_estimate(family: str, snapshot: SemanticsSnapshot) -> str:
    cfg = snapshot.config_payload
    if family == "TeamMin":
        if not (cfg.get("team_min_by_team") or {}):
            return "inactive"
        return "soft_fallback" if bool(cfg.get("team_min_soft_fallback", False)) else "enforced"
    if family in ("GradeMin", "GradeMax"):
        gc = cfg.get("grade_config") or {}
        cmap = gc.get("constraints") or gc.get("constraints_json") or {}
        if not cmap:
            return "inactive"
        if family == "GradeMax" and bool(cfg.get("_force_grade_max_soft_fallback", False)):
            return "soft_fallback"
        if family == "GradeMin" and bool(cfg.get("_force_grade_min_soft_fallback", False)):
            return "soft_fallback"
        return "soft_fallback" if bool(gc.get("allow_soft_fallback", False)) else "enforced"
    if family == "TeamGradeHandoff":
        return "soft_fallback" if bool(cfg.get("team_handoff_soft_fallback", False)) else "enforced"
    if family == "BoundaryTransitionBan":
        any_active = (
            bool(cfg.get("ban_n_to_d", True))
            or bool(cfg.get("ban_e_to_d", True))
            or bool(cfg.get("ban_n_to_e", True))
        )
        return "enforced" if any_active else "inactive"
    if family == "NightRecovery":
        any_on = bool(cfg.get("two_offs_after_two_nig", False)) or bool(
            cfg.get("two_offs_after_three_nig", False)
        )
        return "enforced" if any_on else "inactive"
    if family == "ConsecutiveWorkLimit":
        return "enforced" if int(cfg.get("max_consecutive_work_days") or 0) > 0 else "inactive"
    if family == "ConsecutiveNightLimit":
        return "enforced" if int(cfg.get("max_consecutive_nights") or 0) > 0 else "inactive"
    if family == "CoverageMin":
        return "enforced"
    return "enforced"


def _instance_count_estimate(family: str, snapshot: SemanticsSnapshot) -> int:
    if family == "TeamMin":
        return _estimate_team_min_instances(snapshot)
    if family in ("GradeMin", "GradeMax"):
        return _estimate_grade_instances(snapshot)
    if family == "CoverageMin":
        return _estimate_coverage_instances(snapshot)
    if family in (
        "BoundaryTransitionBan",
        "NightRecovery",
        "ConsecutiveWorkLimit",
        "ConsecutiveNightLimit",
    ):
        return len(snapshot.nurses)
    return 0


def list_constraint_modules(
    snapshot: SemanticsSnapshot, *, ontology: ConstraintOntology | None = None
) -> list[ModuleSummary]:
    onto = ontology or get_default_ontology()
    summaries: list[ModuleSummary] = []
    for family_id, entry in onto.constraints.items():
        summaries.append(
            ModuleSummary(
                family=family_id,
                label=entry.label,
                parent=entry.parent,
                scope=list(entry.scope),
                effective_modes=list(entry.effective_modes),
                current_mode_estimate=_module_mode_estimate(family_id, snapshot),
                instance_count_estimate=_instance_count_estimate(family_id, snapshot),
                supported_actions=supported_actions_for(family_id),
            )
        )
    summaries.sort(key=lambda m: (m.parent, m.family))
    return summaries


def find_constraint_instances(
    snapshot: SemanticsSnapshot,
    *,
    family: str | None = None,
    scope_filter: dict[str, Any] | None = None,
    ontology: ConstraintOntology | None = None,
    rs=None,
) -> list[InstanceView]:
    onto = ontology or get_default_ontology()
    target_family: str | None = None
    if family:
        target_family = onto.resolve_alias(family)
        if target_family is None:
            raise ValueError(f"unknown ontology family: {family!r}")
    atoms = []
    if rs is not None:
        try:
            atoms = build_assignment_atoms(snapshot, rs.roster)
        except Exception:
            atoms = []
    nodes = build_constraint_nodes(snapshot=snapshot, atoms=atoms)
    out: list[InstanceView] = []
    for node in nodes:
        if target_family and onto.resolve_alias(str(node.family)) != target_family:
            continue
        if scope_filter:
            ok = True
            for k, v in scope_filter.items():
                if str(node.scope.get(k)) != str(v):
                    ok = False
                    break
            if not ok:
                continue
        out.append(
            InstanceView(
                node_id=node.node_id,
                family=str(node.family),
                mode=str(node.mode),
                scope=dict(node.scope),
                operator=str(node.operator),
                target=node.target,
                explanation=str(node.explanation_template).format(
                    **{**node.scope, "target": node.target}
                )
                if node.explanation_template
                else "",
            )
        )
    return out


# ---- write-side: apply adjustments ------------------------------------------------


def _apply_team_min(
    cfg: dict[str, Any], adj: ConstraintAdjustment
) -> None:
    if adj.action == "disable_module":
        cfg["team_min_by_team"] = {}
        return
    if adj.action == "force_soft_mode":
        cfg["team_min_soft_fallback"] = True
        return
    if adj.action == "set_threshold":
        team = adj.scope_filter.get("team")
        shift = adj.scope_filter.get("shift")
        if team is None or shift is None:
            raise ValueError(
                "TeamMin set_threshold requires scope_filter.team and scope_filter.shift"
            )
        if not isinstance(adj.target_value, int):
            raise ValueError("TeamMin set_threshold target_value must be int")
        cfg.setdefault("team_min_by_team", {})
        cfg["team_min_by_team"].setdefault(str(team), {})
        cfg["team_min_by_team"][str(team)][str(shift)] = int(adj.target_value)
        return
    if adj.action == "narrow_scope":
        team = adj.scope_filter.get("team")
        if team is None:
            raise ValueError("TeamMin narrow_scope requires scope_filter.team")
        existing = cfg.get("team_min_by_team") or {}
        existing.pop(str(team), None)
        cfg["team_min_by_team"] = existing
        return
    raise ValueError(f"unsupported TeamMin action: {adj.action}")


def _apply_grade(family: str, cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    if adj.action == "force_soft_mode":
        if family == "GradeMax":
            cfg["_force_grade_max_soft_fallback"] = True
        else:
            cfg["_force_grade_min_soft_fallback"] = True
            gc = dict(cfg.get("grade_config") or {})
            gc["allow_soft_fallback"] = True
            cfg["grade_config"] = gc
        return
    if adj.action == "disable_module":
        gc = copy.deepcopy(cfg.get("grade_config") or {})
        gc["constraints"] = {}
        gc["constraints_json"] = {}
        cfg["grade_config"] = gc
        return
    raise ValueError(f"unsupported {family} action: {adj.action}")


def _apply_team_grade_handoff(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    if adj.action == "force_soft_mode":
        cfg["team_handoff_soft_fallback"] = True
        return
    raise ValueError(f"unsupported TeamGradeHandoff action: {adj.action}")


def _apply_boundary_transition(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    if adj.action == "disable_module":
        cfg["ban_n_to_d"] = False
        cfg["ban_e_to_d"] = False
        cfg["ban_n_to_e"] = False
        return
    raise ValueError(f"unsupported BoundaryTransitionBan action: {adj.action}")


def _apply_consec(cfg: dict[str, Any], key: str, adj: ConstraintAdjustment) -> None:
    if adj.action == "set_threshold":
        if not isinstance(adj.target_value, int) or adj.target_value < 0:
            raise ValueError(f"{key} set_threshold target_value must be non-negative int")
        cfg[key] = int(adj.target_value)
        return
    raise ValueError(f"unsupported {key} action: {adj.action}")


def _apply_night_recovery(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    if adj.action == "disable_module":
        cfg["two_offs_after_two_nig"] = False
        cfg["two_offs_after_three_nig"] = False
        return
    raise ValueError(f"unsupported NightRecovery action: {adj.action}")


def _apply_monthly_night_cap(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    if adj.action == "set_threshold":
        if not isinstance(adj.target_value, int) or adj.target_value < 0:
            raise ValueError("MonthlyNightCap set_threshold target_value must be non-negative int")
        cfg["max_night_shifts_per_month"] = int(adj.target_value)
        return
    raise ValueError(f"unsupported MonthlyNightCap action: {adj.action}")


def _apply_weekend_off_only(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    if adj.action == "disable_module":
        cfg["weekend_off_only_enable"] = False
        return
    raise ValueError(f"unsupported WeekendOffOnly action: {adj.action}")


def _apply_ban_night_before_fixed_off(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    if adj.action == "disable_module":
        cfg["ban_night_before_fixed_off"] = False
        return
    raise ValueError(f"unsupported BanNightBeforeFixedOff action: {adj.action}")


def _apply_coverage_min(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    if adj.action != "set_threshold":
        raise ValueError(f"unsupported CoverageMin action: {adj.action}")
    day = adj.scope_filter.get("day")
    shift = adj.scope_filter.get("shift")
    if day is None or shift is None:
        raise ValueError("CoverageMin set_threshold requires scope_filter.day and shift")
    if not isinstance(adj.target_value, int) or adj.target_value < 0:
        raise ValueError("CoverageMin set_threshold target_value must be non-negative int")
    by_day = cfg.get("daily_shift_requirements_by_day")
    day_idx = int(day) - 1 if int(day) > 0 else int(day)
    if isinstance(by_day, list) and 0 <= day_idx < len(by_day):
        existing = dict(by_day[day_idx] or {})
        existing[str(shift)] = int(adj.target_value)
        by_day[day_idx] = existing
        cfg["daily_shift_requirements_by_day"] = by_day
    else:
        base = dict(cfg.get("daily_shift_requirements") or {})
        base[str(shift)] = int(adj.target_value)
        cfg["daily_shift_requirements"] = base


_DISPATCH: dict[str, Any] = {
    "TeamMin": _apply_team_min,
    "GradeMin": lambda cfg, adj: _apply_grade("GradeMin", cfg, adj),
    "GradeMax": lambda cfg, adj: _apply_grade("GradeMax", cfg, adj),
    "TeamGradeHandoff": _apply_team_grade_handoff,
    "BoundaryTransitionBan": _apply_boundary_transition,
    "ConsecutiveWorkLimit": lambda cfg, adj: _apply_consec(cfg, "max_consecutive_work_days", adj),
    "ConsecutiveNightLimit": lambda cfg, adj: _apply_consec(cfg, "max_consecutive_nights", adj),
    "NightRecovery": _apply_night_recovery,
    "MonthlyNightCap": _apply_monthly_night_cap,
    "WeekendOffOnly": _apply_weekend_off_only,
    "BanNightBeforeFixedOff": _apply_ban_night_before_fixed_off,
    "CoverageMin": _apply_coverage_min,
}


def apply_adjustments_to_config(
    config_dict: dict[str, Any],
    adjustments: Iterable[ConstraintAdjustment | dict[str, Any]],
    *,
    ontology: ConstraintOntology | None = None,
) -> dict[str, Any]:
    onto = ontology or get_default_ontology()
    new_cfg = copy.deepcopy(config_dict or {})
    applied: list[dict[str, Any]] = list(new_cfg.get("_applied_constraint_adjustments") or [])
    for raw in adjustments:
        adj = (
            raw
            if isinstance(raw, ConstraintAdjustment)
            else ConstraintAdjustment(
                family=str(raw.get("family")),
                action=str(raw.get("action")),
                scope_filter=dict(raw.get("scope_filter") or {}),
                target_value=raw.get("target_value"),
                reason=raw.get("reason"),
            )
        )
        adj.validate_action()
        family = adj.normalized_family(onto)
        allowed = _FAMILY_ACTION_MATRIX.get(family, set())
        if adj.action not in allowed:
            raise ValueError(
                f"unsupported adjustment: {family}/{adj.action} (supported={sorted(allowed)})"
            )
        handler = _DISPATCH.get(family)
        if handler is None:
            raise ValueError(f"no handler for family: {family}")
        handler(new_cfg, adj)
        applied.append(
            {
                "family": family,
                "action": adj.action,
                "scope_filter": dict(adj.scope_filter or {}),
                "target_value": adj.target_value,
                "reason": adj.reason,
            }
        )
    new_cfg["_applied_constraint_adjustments"] = applied
    return new_cfg


__all__ = [
    "ConstraintAdjustment",
    "InstanceView",
    "ModuleSummary",
    "SUPPORTED_ACTIONS",
    "apply_adjustments_to_config",
    "find_constraint_instances",
    "list_constraint_modules",
    "supported_actions_for",
]
