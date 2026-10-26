"""Agent-facing control layer for constraint module/instance adjustments.

이 모듈은 ontology 에 정의된 constraint family 를 module 단위로 보고,
에이전트가 disable / force_soft / set_threshold / narrow_scope 4 종 액션으로
조절할 수 있게 해 준다. 적용은 솔버 input config_dict 수정으로만 이루어진다.

EI-11 11d — CLAUDE.md 하드락 표지(정책 판정은 `policy.card_filter`): 모듈 목록은 action 별 하드락 판정
(`ModuleSummary.hard_lock`)을 싣고, 조정 기록은 하드락을 완화하는 조정에 '적용 불가'(`applicable: False`) 표지를 단다.
control 은 표지만 단다(모드 없음 — 적용은 지금처럼 한다). 생성 거부는 생성 진입 관문이 한다.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from services.constraint_impact.atoms import build_assignment_atoms
from services.constraint_impact.graph_builder import build_constraint_nodes
from services.constraint_impact.snapshot import SemanticsSnapshot
from services.engine_contract.defaults import registry_default
from services.semantics.ontology import ConstraintOntology, get_default_ontology

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = ("disable_module", "force_soft_mode", "set_threshold", "narrow_scope")
_APPLIED_KEY = "_applied_constraint_adjustments"


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
    #: EI-11 11d — 지원 action 중 하드락 판정이 ok 가 아닌 것 `{action: {mode, rule_nos, cap?, key?}}`(blocked·capped·marked).
    hard_lock: dict[str, dict] = field(default_factory=dict)


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
    # EI-11 11f — GradeMin force_soft 는 뺐다. 엔진·생성이 읽지 않는 죽은 쓰기였다(등급 설정은 DB 에서 다시 읽는다).
    "GradeMin": {"disable_module"},
    "GradeMax": {"disable_module", "force_soft_mode"},
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
        return "soft_fallback" if bool(gc.get("allow_soft_fallback", False)) else "enforced"
    if family == "BoundaryTransitionBan":
        any_active = (
            bool(cfg.get("ban_n_to_d", True))
            or bool(cfg.get("ban_e_to_d", True))
            or bool(cfg.get("ban_n_to_e", True))
        )
        return "enforced" if any_active else "inactive"
    if family == "NightRecovery":
        # D49: 키가 없으면 '꺼짐'이 아니라 엔진 기본(레지스트리)
        any_on = bool(cfg.get("two_offs_after_two_nig", registry_default("two_offs_after_two_nig"))) or bool(
            cfg.get("two_offs_after_three_nig", registry_default("two_offs_after_three_nig"))
        )
        return "enforced" if any_on else "inactive"
    if family == "ConsecutiveWorkLimit":
        return "enforced" if int(cfg.get("max_conseq_work") or 0) > 0 else "inactive"
    if family == "ConsecutiveNightLimit":
        return "enforced"  # 정본 three_seq_nig 는 bool — 켜면 3·꺼도 2 라 연속 나이트 상한이 늘 있다
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


def _hard_lock_policy(ontology: ConstraintOntology) -> dict | None:
    """(family, action) 하드락 판정(`card_filter.family_action_policy`) — 판정이 실패하면 None(호출부가 막는 쪽으로)."""
    from services.policy import card_filter

    try:
        return card_filter.family_action_policy(ontology)
    except Exception:
        logger.warning("모듈 목록 하드락 판정 실패 — 하드락 family 의 지원 action 을 모두 막힘으로 표시합니다(fail-closed).",
                       exc_info=True)
        return None


def _module_hard_lock(family: str, policy: dict | None) -> dict[str, dict]:
    """그 family 의 지원 action 중 하드락 판정이 ok 가 아닌 것 `{action: {mode, rule_nos, cap?, key?}}`. 판정을 못 했으면
    대체 집합(`HARD_LOCK_FAMILIES_FALLBACK`) family 의 지원 action 을 모두
    `{"mode": "blocked", "rule_nos": [], "error": True}` 로 둔다."""
    if policy is None:
        from services.policy.card_filter import HARD_LOCK_FAMILIES_FALLBACK

        if family not in HARD_LOCK_FAMILIES_FALLBACK:
            return {}
        return {action: {"mode": "blocked", "rule_nos": [], "error": True} for action in supported_actions_for(family)}
    out: dict[str, dict] = {}
    for action in supported_actions_for(family):
        entry = policy.get((family, action)) or {}
        if entry.get("mode", "ok") != "ok":
            out[action] = {k: entry[k] for k in ("mode", "rule_nos", "cap", "key", "error") if k in entry}
    return out


def list_constraint_modules(
    snapshot: SemanticsSnapshot, *, ontology: ConstraintOntology | None = None
) -> list[ModuleSummary]:
    onto = ontology or get_default_ontology()
    policy = _hard_lock_policy(onto)  # EI-11 11d — 호출당 한 번
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
                hard_lock=_module_hard_lock(family_id, policy),
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
    if adj.action == "force_soft_mode" and family == "GradeMax":
        cfg["_force_grade_max_soft_fallback"] = True
        return
    if adj.action == "disable_module":
        gc = copy.deepcopy(cfg.get("grade_config") or {})
        gc["constraints"] = {}
        gc["constraints_json"] = {}
        cfg["grade_config"] = gc
        return
    raise ValueError(f"unsupported {family} action: {adj.action}")


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


class RuntimeKeyForbidden(ValueError):
    """레지스트리가 런타임 조정을 금지한 설정(D13)을 바꾸려는 조정 — 설정은 쓰지 않고 '적용 불가'로만 기록한다."""

    def __init__(self, violation) -> None:
        super().__init__(violation.message_ko)
        self.violation = violation


def _apply_consec_nights(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    """연속 나이트 상한 — 레지스트리가 런타임 조정을 금지(D13)하면 쓰지 않고 '적용 불가'로 기록한다. 금지 표지가 없으면 정본 bool
    `three_seq_nig`(3 if x else 2)에 접어 쓴다 — 3 을 넘는 값도 True(=3)라 절대 상한(하드락 #3)을 못 넘는다."""
    from services.policy.hard_lock import runtime_forbidden_violation

    if adj.action != "set_threshold":
        raise ValueError(f"unsupported ConsecutiveNightLimit action: {adj.action}")
    if not isinstance(adj.target_value, int) or adj.target_value < 0:
        raise ValueError("ConsecutiveNightLimit set_threshold target_value must be non-negative int")
    violation = runtime_forbidden_violation("three_seq_nig")
    if violation is not None:
        raise RuntimeKeyForbidden(violation)
    cfg["three_seq_nig"] = adj.target_value >= 3  # 표지가 없으면 정본 bool 로 접어 쓴다(레지스트리 `to_canonical` x>=3)


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
        cfg["max_nig_per_month"] = int(adj.target_value)
        return
    raise ValueError(f"unsupported MonthlyNightCap action: {adj.action}")


def _apply_weekend_off_only(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    if adj.action == "disable_module":
        cfg["weekend_off_only_enable"] = False
        return
    raise ValueError(f"unsupported WeekendOffOnly action: {adj.action}")


def _apply_ban_night_before_fixed_off(cfg: dict[str, Any], adj: ConstraintAdjustment) -> None:
    """이 family 는 **설정 두 개**가 같은 이름으로 emit 된다 — 어느 축을 끌지 골라야 한다.

    · axis="type"         → ban_night_before_fixed_off        (휴가·공가 직전 N)
    · axis="fixed_wanted" → ban_night_before_fixed_wanted_off (확정 원티드 O 직전 N)
    ★ axis 가 없으면 어느 쪽이 원인인지 모른다. 완화의 목적이 infeasible 해소이므로
      **둘 다 끈다** — 한쪽만 끄면 재시도가 같은 이유로 또 실패한다.
    """
    if adj.action == "disable_module":
        axis = (adj.scope_filter or {}).get("axis")
        if axis == "fixed_wanted":
            cfg["ban_night_before_fixed_wanted_off"] = False
        elif axis == "type":
            cfg["ban_night_before_fixed_off"] = False
        else:
            cfg["ban_night_before_fixed_off"] = False
            cfg["ban_night_before_fixed_wanted_off"] = False
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


def _changed_settings(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """조정 하나가 바꾼 최상위 키 → 새 값(조정 기록 칸 제외). 하드락 키는 스칼라라 얕은 사본 비교로 충분하다."""
    keys = dict.fromkeys([*before, *after])
    return {k: after.get(k) for k in keys if k != _APPLIED_KEY and before.get(k) != after.get(k)}


def _hard_lock_record_marks(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """EI-11 11d — 조정 기록에 싣는 하드락 표지. 기본값(판정 위반 0)이면 빈 dict 라 기록 칸이 늘지 않는다.

    BLOCK 이 있으면 `applicable: False` + `blocked_by_hard_lock: [{key, rule_nos, message_ko}]`, 인접·영향 변경이면
    `hard_lock_relax_marks: [{key, message_ko}]`. 판정이 실패하면 `applicable: False`·`hard_lock_check_error: True`(§2.0.6 ⑦).
    거부는 하지 않는다 — 적용은 지금처럼 하고, 생성 경로의 거부는 생성 진입 관문이 한다."""
    from services.policy import card_filter
    from services.policy.hard_lock import BLOCK

    try:
        violations = card_filter.delta_violations(_changed_settings(before, after))
    except Exception:
        logger.warning("조정 기록 하드락 판정 실패 — 이 조정을 '적용 불가'로 표시합니다(fail-closed).", exc_info=True)
        return {"applicable": False, "hard_lock_check_error": True}
    out: dict[str, Any] = {}
    blocks = [v for v in violations if v.kind == BLOCK]
    marks = [v for v in violations if v.kind != BLOCK]
    if blocks:
        out["applicable"] = False
        out["blocked_by_hard_lock"] = [{"key": v.key, "rule_nos": sorted({int(n) for n in v.rule_nos if n}),
                                        "message_ko": v.message_ko} for v in blocks]
    if marks:
        out["hard_lock_relax_marks"] = [{"key": v.key, "message_ko": v.message_ko} for v in marks]
    return out


_DISPATCH: dict[str, Any] = {
    "TeamMin": _apply_team_min,
    "GradeMin": lambda cfg, adj: _apply_grade("GradeMin", cfg, adj),
    "GradeMax": lambda cfg, adj: _apply_grade("GradeMax", cfg, adj),
    "BoundaryTransitionBan": _apply_boundary_transition,
    "ConsecutiveWorkLimit": lambda cfg, adj: _apply_consec(cfg, "max_conseq_work", adj),
    "ConsecutiveNightLimit": _apply_consec_nights,
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
        before = dict(new_cfg)  # EI-11 11d — 하드락 표지용 얕은 사본(하드락 키는 스칼라)
        try:
            handler(new_cfg, adj)
            marks = _hard_lock_record_marks(before, new_cfg)
        except RuntimeKeyForbidden as exc:  # 설정은 그대로 — 기록만 '적용 불가'
            v = exc.violation
            marks = {"applicable": False, "blocked_by_hard_lock": [
                {"key": v.key, "rule_nos": [v.rule_no], "message_ko": v.message_ko}]}
        applied.append(
            {
                "family": family,
                "action": adj.action,
                "scope_filter": dict(adj.scope_filter or {}),
                "target_value": adj.target_value,
                "reason": adj.reason,
                **marks,
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
