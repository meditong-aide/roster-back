from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class OntologyConstraintEntry:
    constraint_id: str
    label: str
    parent: str
    scope: list[str]
    effective_modes: list[str]
    connects: list[str]
    explanation_template: str
    produces: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    default_severity: str | None = None
    notes: str | None = None
    relaxation_priority: int | None = None
    scope_explosion: str | None = None
    tier: str | None = None
    causal_layer: str | None = None


@dataclass(slots=True)
class OntologyConflictScenario:
    scenario_id: str
    involved_families: list[str]
    trigger_condition: str
    why_infeasible: str
    suggested_relaxation: str
    detection_hint: str


@dataclass(slots=True)
class OntologyOverrideEntry:
    override_id: str
    label: str
    parent: str
    bypasses: list[str]
    does_not_bypass: list[str]
    conditional_effects: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OntologyModeEntry:
    mode_id: str
    label: str
    meaning: str
    is_enforced: bool
    severity: str


@dataclass(slots=True)
class OntologyCause:
    """US-6 (v4) — Cause 노드. reason_code 로 노출되는 유일한 카테고리."""
    cause_id: str
    label: str
    category: str
    causal_layer: str
    tier: str
    is_hard: bool
    aliases: list[str] = field(default_factory=list)
    problem_template_ko: str = ""


@dataclass(slots=True)
class OntologyTreatment:
    """US-6 (v4) — Treatment 노드. atomic relaxation action.

    applies_to_causes 가 hitting set 의 cause→treatment edge. US-4 hitter 가 이걸 lookup.
    """
    treatment_id: str
    label: str
    action_type: str        # force_soft_mode / disable_module / set_threshold / narrow_scope / data_correction_required
    target_family: str
    config_key: str | None
    direction: str          # enable / disable / increase / decrease / clear / remove_key / manual
    rationale_ko: str
    trade_off_ko: str
    applies_to_causes: list[str] = field(default_factory=list)


# ── 친화 라벨링 (사용자 dashboard 노출용) ─────────────────────────────────
# raw setting key 가 사용자에게 jargon 으로 보이지 않도록 한국어 운영 어휘로 매핑.
# yaml 미수정 — 코드 측 중앙 사전.
CONFIG_KEY_LABELS_KO: dict[str, str] = {
    "_force_grade_max_soft_fallback": "등급 상한 soft 전환",
    "_force_grade_min_soft_fallback": "등급 최저 soft 전환",
    "team_min_soft_fallback":         "팀 최소 soft 전환",
    "team_handoff_soft_fallback":     "팀 인계 soft 전환",
    "team_min_by_team":               "팀 최소 인원",
    "daily_shift_requirements":       "일별 시프트 요구 인원",
    "max_night_shifts_per_month":     "월 야간 한도",
    "max_consecutive_work_days":      "최대 연속 근무일",
    "ban_n_to_d":                     "야간→주간 전이 금지",
    "ban_night_before_fixed_off":     "고정 OFF 직전 N 금지",
    "two_offs_after_two_nig":         "2N 후 2일 OFF 규칙",
    "weekend_off_only_enable":        "주말 OFF 전용 정책",
    "allowed_shifts":                 "가능 시프트 목록",
    "prev_month_schedule":            "전월 근무표",
    "preceptee_pair":                 "프리셉터-프리셉티 페어",
    "nurse_assignment":               "간호사 배정 (파견 등)",
}

DIRECTION_LABELS_KO: dict[str, str] = {
    "enable":     "활성화",
    "disable":    "비활성화",
    "increase":   "상향",
    "decrease":   "하향",
    "clear":      "초기화",
    "remove_key": "특정 키 제거",
    "manual":     "수동 점검",
}


def friendly_config_key_label(config_key: str | None) -> str | None:
    """raw config_key → 사용자 친화 라벨. 미정의 키는 raw 그대로 반환."""
    if not config_key:
        return None
    return CONFIG_KEY_LABELS_KO.get(config_key, config_key)


def friendly_direction_label(direction: str | None) -> str | None:
    """raw direction → 사용자 친화 라벨."""
    if not direction:
        return None
    return DIRECTION_LABELS_KO.get(direction, direction)


def build_apply_hint(treatment: "OntologyTreatment") -> dict | None:
    """treatment → frontend 가 재실행 호출에 사용할 hint.

    사용자 정책 (2026-05-17):
      team_min 자동 soften 은 막고, 대신 결과 payload 에 lever 노출 → 사용자
      클릭 → 재실행. 본 hint 는 그 클릭을 위한 호출 정보.

    None 반환 = manual only (data_correction_required) — frontend 는 안내 텍스트
    만 노출, 자동 재실행 불가.

    Returns:
        {
          method, url, config_overrides, user_consent_required, human_message_ko
        }
    """
    if treatment is None:
        return None
    if treatment.action_type == "data_correction_required":
        return None
    if not treatment.config_key:
        return None

    overrides: dict[str, object]
    if treatment.action_type == "force_soft_mode":
        overrides = {treatment.config_key: True}
    elif treatment.action_type == "disable_module":
        overrides = {treatment.config_key: False}
    elif treatment.action_type == "set_threshold":
        # 정확한 새 값은 사용자/frontend 가 결정. direction 만 hint.
        overrides = {treatment.config_key: {"adjust": treatment.direction}}
    elif treatment.action_type == "narrow_scope":
        overrides = {treatment.config_key: {"narrow": treatment.direction}}
    else:
        return None

    key_label = friendly_config_key_label(treatment.config_key) or treatment.config_key
    dir_label = friendly_direction_label(treatment.direction) or treatment.direction
    return {
        "method": "POST",
        "url": "/roster_create/generate",
        "config_overrides": overrides,
        "user_consent_required": True,
        "human_message_ko": (
            f"'{treatment.label}' ({key_label} → {dir_label}) 적용 후 재시도합니다. "
            f"동의하시겠습니까?"
        ),
    }


class ConstraintOntology:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path(__file__).with_name("ontology.yaml")
        self.version = 0
        self.constraints: dict[str, OntologyConstraintEntry] = {}
        self.overrides: dict[str, OntologyOverrideEntry] = {}
        self.modes: dict[str, OntologyModeEntry] = {}
        self.relations: dict[str, dict[str, Any]] = {}
        self.conflict_scenarios: list[OntologyConflictScenario] = []
        self._alias_to_id: dict[str, str] = {}
        # US-2: cost model meta — 모든 cost 가중치의 단일 진실 (ontology.yaml meta.cost_model)
        self.cost_model_meta: dict[str, Any] = {}
        self.parent_to_default_causal_layer: dict[str, str] = {}
        # US-6 (v4): cause / treatment 카탈로그
        self.causes: dict[str, OntologyCause] = {}
        self.treatments: dict[str, OntologyTreatment] = {}
        self._cause_alias_to_id: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.version = int(raw.get("version") or 0)
        meta = raw.get("meta") or {}
        self.cost_model_meta = dict(meta.get("cost_model") or {})
        self.parent_to_default_causal_layer = dict(meta.get("parent_to_default_causal_layer") or {})
        for cid, body in (raw.get("constraints") or {}).items():
            parent = str(body.get("parent") or "")
            explicit_layer = body.get("causal_layer")
            causal_layer = (
                str(explicit_layer) if explicit_layer
                else self.parent_to_default_causal_layer.get(parent)
            )
            entry = OntologyConstraintEntry(
                constraint_id=cid,
                label=str(body.get("label") or cid),
                parent=parent,
                scope=list(body.get("scope") or []),
                effective_modes=list(body.get("effective_modes") or []),
                connects=list(body.get("connects") or []),
                explanation_template=str(body.get("explanation_template") or ""),
                produces=list(body.get("produces") or []),
                consumes=list(body.get("consumes") or []),
                aliases=list(body.get("aliases") or []),
                default_severity=body.get("default_severity"),
                notes=body.get("notes"),
                relaxation_priority=body.get("relaxation_priority"),
                scope_explosion=body.get("scope_explosion"),
                tier=body.get("tier"),
                causal_layer=causal_layer,
            )
            self.constraints[cid] = entry
            self._alias_to_id[cid.upper()] = cid
            for alias in entry.aliases:
                self._alias_to_id[str(alias).upper()] = cid
        for oid, body in (raw.get("overrides") or {}).items():
            self.overrides[oid] = OntologyOverrideEntry(
                override_id=oid,
                label=str(body.get("label") or oid),
                parent=str(body.get("parent") or ""),
                bypasses=list(body.get("bypasses") or []),
                does_not_bypass=list(body.get("does_not_bypass") or []),
                conditional_effects=dict(body.get("conditional_effects") or {}),
            )
        for mid, body in (raw.get("modes") or {}).items():
            self.modes[mid] = OntologyModeEntry(
                mode_id=mid,
                label=str(body.get("label") or mid),
                meaning=str(body.get("meaning") or ""),
                is_enforced=bool(body.get("is_enforced", False)),
                severity=str(body.get("severity") or "none"),
            )
        self.relations = dict(raw.get("relations") or {})
        for scen in (raw.get("conflict_scenarios") or []):
            try:
                self.conflict_scenarios.append(
                    OntologyConflictScenario(
                        scenario_id=str(scen.get("id") or ""),
                        involved_families=list(scen.get("involved_families") or []),
                        trigger_condition=str(scen.get("trigger_condition") or "").strip(),
                        why_infeasible=str(scen.get("why_infeasible") or "").strip(),
                        suggested_relaxation=str(scen.get("suggested_relaxation") or "").strip(),
                        detection_hint=str(scen.get("detection_hint") or "").strip(),
                    )
                )
            except Exception:
                continue
        # US-6 (v4): causes / treatments
        for cid, body in (raw.get("causes") or {}).items():
            entry = OntologyCause(
                cause_id=cid,
                label=str(body.get("label") or cid),
                category=str(body.get("category") or "uncategorized"),
                causal_layer=str(body.get("causal_layer") or "structural"),
                tier=str(body.get("tier") or "T2"),
                is_hard=bool(body.get("is_hard", True)),
                aliases=list(body.get("aliases") or []),
                problem_template_ko=str(body.get("problem_template_ko") or ""),
            )
            self.causes[cid] = entry
            self._cause_alias_to_id[cid.upper()] = cid
            for alias in entry.aliases:
                self._cause_alias_to_id[str(alias).upper()] = cid
        for tid, body in (raw.get("treatments") or {}).items():
            self.treatments[tid] = OntologyTreatment(
                treatment_id=tid,
                label=str(body.get("label") or tid),
                action_type=str(body.get("action_type") or "manual"),
                target_family=str(body.get("target_family") or ""),
                config_key=body.get("config_key"),
                direction=str(body.get("direction") or "manual"),
                rationale_ko=str(body.get("rationale_ko") or "").strip(),
                trade_off_ko=str(body.get("trade_off_ko") or "").strip(),
                applies_to_causes=list(body.get("applies_to_causes") or []),
            )

    # ---- US-6 (v4) — cause/treatment API ----
    def get_cause(self, cause_id_or_alias: str) -> OntologyCause | None:
        cid = self.resolve_cause_alias(cause_id_or_alias)
        return self.causes.get(cid) if cid else None

    def resolve_cause_alias(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        key = str(raw).strip()
        if not key:
            return None
        # 정확 id 우선
        if key in self.causes:
            return key
        return self._cause_alias_to_id.get(key.upper())

    def get_treatment(self, treatment_id: str) -> OntologyTreatment | None:
        return self.treatments.get(treatment_id)

    def treatments_for_cause(self, cause_id_or_alias: str) -> list[OntologyTreatment]:
        cid = self.resolve_cause_alias(cause_id_or_alias)
        if not cid:
            return []
        return [t for t in self.treatments.values() if cid in t.applies_to_causes]

    def get_constraint(self, family: str) -> OntologyConstraintEntry | None:
        resolved = self.resolve_alias(family)
        return self.constraints.get(resolved) if resolved else None

    def get_mode(self, mode_id: str) -> OntologyModeEntry | None:
        return self.modes.get(str(mode_id or ""))

    def get_override(self, override_id: str) -> OntologyOverrideEntry | None:
        return self.overrides.get(str(override_id or ""))

    def get_parent(self, family: str) -> str | None:
        entry = self.get_constraint(family)
        return entry.parent if entry else None

    def get_scope(self, family: str) -> list[str]:
        entry = self.get_constraint(family)
        return list(entry.scope) if entry else []

    def get_template(self, family: str) -> str | None:
        entry = self.get_constraint(family)
        return entry.explanation_template if entry else None

    def get_default_severity(self, family: str) -> str | None:
        entry = self.get_constraint(family)
        return entry.default_severity if entry else None

    def resolve_alias(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        return self._alias_to_id.get(str(raw).strip().upper())

    def can_bypass(self, override_type: str, family: str) -> bool | None:
        ov = self.get_override(override_type)
        target = self.resolve_alias(family)
        if ov is None or target is None:
            return None
        if target in ov.bypasses:
            return True
        if target in ov.does_not_bypass:
            return False
        return None

    def get_relaxation_priority(self, family: str) -> int | None:
        entry = self.get_constraint(family)
        return entry.relaxation_priority if entry else None

    def get_scope_explosion(self, family: str) -> str | None:
        entry = self.get_constraint(family)
        return entry.scope_explosion if entry else None

    def get_tier(self, family: str) -> str | None:
        """Return 4-tier hard-constraint classification.

        T0=절대/물리 hard, T1=안전 hard, T2=운영 hard, T3=품질 soft.
        Unknown family returns None (caller decides fallback).
        """
        entry = self.get_constraint(family)
        return entry.tier if entry else None

    def families_by_tier(self, tier: str) -> list[str]:
        return [cid for cid, e in self.constraints.items() if (e.tier or "") == tier]

    def scenarios_involving(self, family: str) -> list[OntologyConflictScenario]:
        target = self.resolve_alias(family)
        if target is None:
            return []
        return [
            s for s in self.conflict_scenarios
            if target in s.involved_families
        ]


@lru_cache(maxsize=1)
def get_default_ontology() -> ConstraintOntology:
    return ConstraintOntology()
