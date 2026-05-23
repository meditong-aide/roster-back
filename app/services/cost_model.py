"""Treatment / relaxation candidate cost — ontology-derived 단일 진실.

설계 원칙 (사용자 핵심 요구):
  코드 안에 magic constant 금지. 모든 가중치는 `ontology.yaml` 의 `meta.cost_model` 에서 derive.
  cost_model_meta 가 누락되면 CostModelError 를 raise (silent fallback 금지).

공식:
  base_cost(family)
    = priority_component
    + scope_explosion_component
    + tier_component
    + causal_layer_component
    + scenario_match_bonus * matched_scenario_count

  treatment_cost(family, ward_profile)
    = base_cost(family) * ward_profile_multiplier
    (forbid 정책이면 None 반환 — caller 가 후보에서 제외)

cost 가 낮을수록 'relaxation 추천 우선순위 높음' 이다 (학술적으로 풀기 쉬움).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from services.semantics.ontology import ConstraintOntology, get_default_ontology


WardPolicy = ("forbid", "avoid", "neutral", "prefer")


class CostModelError(ValueError):
    """ontology.cost_model_meta 가 missing / invalid 일 때 raise."""


def _meta(ontology: ConstraintOntology) -> dict[str, Any]:
    cm = getattr(ontology, "cost_model_meta", None)
    if not isinstance(cm, dict) or not cm:
        raise CostModelError(
            "ontology.cost_model_meta 가 비어있음. "
            "ontology.yaml 의 `meta.cost_model` 섹션을 확인하세요."
        )
    return cm


def _component_value(name: str, key: Any, meta: dict[str, Any], default: float = 0.0) -> float:
    section = (meta.get("components") or {}).get(name)
    if not isinstance(section, dict):
        return float(default)
    # str 비교 (yaml key 는 보통 str)
    val = section.get(key, section.get(str(key)))
    if val is None:
        return float(default)
    return float(val)


def _defaults(meta: dict[str, Any]) -> dict[str, Any]:
    return dict(meta.get("defaults") or {})


def _multiplier_for_policy(meta: dict[str, Any], policy: str) -> Optional[float]:
    """ward_profile policy → multiplier. forbid (null) 면 None 반환."""
    multipliers = meta.get("ward_profile_multipliers") or {}
    if policy not in multipliers:
        # 누락된 정책은 neutral 로 처리
        policy = "neutral"
    val = multipliers.get(policy)
    if val is None:
        return None
    return float(val)


def compute_base_cost(
    family: str,
    ontology: Optional[ConstraintOntology] = None,
    *,
    matched_scenario_count: int = 0,
) -> float:
    """ontology fields 만으로 결정적 base cost 계산. 낮을수록 추천 우선.

    cost 산식:
        priority_component (= relaxation_priority 값)
        + scope_explosion_component (yaml table lookup)
        + tier_component (yaml table lookup)
        + causal_layer_component (yaml table lookup)
        + scenario_match_bonus * matched_scenario_count (보통 음수 — 매칭 = cost 감소)
    """
    onto = ontology or get_default_ontology()
    meta = _meta(onto)
    defaults = _defaults(meta)

    entry = onto.get_constraint(family)
    priority = (
        entry.relaxation_priority if (entry and entry.relaxation_priority is not None)
        else defaults.get("priority", 3)
    )
    scope = (
        entry.scope_explosion if (entry and entry.scope_explosion)
        else defaults.get("scope_explosion", "medium")
    )
    tier = (
        entry.tier if (entry and entry.tier)
        else defaults.get("tier", "T2")
    )
    layer = (
        entry.causal_layer if (entry and entry.causal_layer)
        else defaults.get("causal_layer", "structural")
    )

    # priority component — derivation 이 'relaxation_priority' 면 값 그대로
    p_section = (meta.get("components") or {}).get("priority") or {}
    if p_section.get("derivation") == "relaxation_priority":
        priority_component = float(priority)
    else:
        priority_component = _component_value("priority", priority, meta, default=float(priority))

    scope_component = _component_value("scope_explosion", scope, meta)
    tier_component = _component_value("tier", tier, meta)
    layer_component = _component_value("causal_layer", layer, meta)

    bonus_per_match = float(meta.get("scenario_match_bonus", 0))
    scenario_component = bonus_per_match * int(matched_scenario_count or 0)

    return priority_component + scope_component + tier_component + layer_component + scenario_component


def compute_treatment_cost(
    family: str,
    ontology: Optional[ConstraintOntology] = None,
    *,
    ward_profile: Optional[Mapping[str, str]] = None,
    matched_scenario_count: int = 0,
) -> Optional[float]:
    """ward_profile 곱셈 적용. forbid → None (caller 가 후보에서 제외).

    ward_profile 예: {"TeamMin": "forbid", "GradeMax": "prefer"}.
    """
    base = compute_base_cost(family, ontology, matched_scenario_count=matched_scenario_count)
    onto = ontology or get_default_ontology()
    meta = _meta(onto)

    policy = "neutral"
    if ward_profile:
        policy = str(ward_profile.get(family) or "neutral").lower()

    mult = _multiplier_for_policy(meta, policy)
    if mult is None:
        return None
    return base * mult
