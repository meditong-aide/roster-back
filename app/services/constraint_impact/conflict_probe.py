"""Conflict Probe — UNSAT 진단 및 자동 relaxation 후보 ranking.

본 모듈은 진단/추천만 수행. 실제 disable 시뮬레이션 실행은 control layer 에서 호출.

US-2 (cost model 통합): 모든 cost 가중치는 services.cost_model + ontology.yaml meta.cost_model
에서 derive. 본 모듈에는 magic constant 가 없다 (테스트로 enforced).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping

from services.constraint_impact.solver_emit import EmittedConstraint
from services.cost_model import compute_treatment_cost
from services.semantics.ontology import (
    ConstraintOntology,
    OntologyConflictScenario,
    get_default_ontology,
)


@dataclass(slots=True)
class RankedCandidate:
    family: str
    score: float          # = -cost. 높을수록 추천 우선 (legacy 호환)
    relaxation_priority: int
    scope_explosion: str
    emit_count: int
    cost: float = 0.0     # cost_model 의 결정적 cost (낮을수록 우선)
    matched_scenario_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    sample_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class MatchedScenario:
    scenario_id: str
    involved_families: list[str]
    confidence: float
    suggested_relaxation: str
    why_infeasible: str
    detection_hint: str


@dataclass(slots=True)
class ProbeStep:
    order: int
    family: str
    action: dict[str, Any]
    rationale: str


@dataclass(slots=True)
class ConflictProbeReport:
    ranked_candidates: list[RankedCandidate]
    matched_scenarios: list[MatchedScenario]
    probe_plan: list[ProbeStep]
    notes: list[str] = field(default_factory=list)


# ---- ranking ---------------------------------------------------------------------


def rank_relaxation_candidates(
    *,
    emit_records: list[EmittedConstraint],
    ontology: ConstraintOntology | None = None,
    matched_scenarios: list[MatchedScenario] | None = None,
    sample_per_family: int = 3,
    ward_profile: Mapping[str, str] | None = None,
) -> list[RankedCandidate]:
    """Ontology-derived cost 기반 후보 ranking.

    cost 가 낮을수록 추천 우선 (score = -cost 로 외부 노출).
    ward_profile 의 'forbid' 정책 family 는 후보에서 제외.
    """
    onto = ontology or get_default_ontology()
    matched = matched_scenarios or []

    by_family: dict[str, list[EmittedConstraint]] = {}
    for r in emit_records or []:
        if r.mode != "enforced":
            continue
        by_family.setdefault(r.family, []).append(r)

    scenario_family_count: Counter[str] = Counter()
    for s in matched:
        for f in s.involved_families:
            scenario_family_count[f] += 1

    candidates: list[RankedCandidate] = []
    for family, records in by_family.items():
        scenario_hits = int(scenario_family_count.get(family, 0))
        cost = compute_treatment_cost(
            family,
            onto,
            ward_profile=ward_profile,
            matched_scenario_count=scenario_hits,
        )
        if cost is None:
            # ward_profile.forbid — 후보에서 제외
            continue

        # entry lookup (정보 표시용 — cost 계산엔 이미 들어감)
        entry = onto.get_constraint(family)
        priority = (entry.relaxation_priority if entry and entry.relaxation_priority is not None
                    else int((onto.cost_model_meta.get("defaults") or {}).get("priority", 3)))
        explosion = (entry.scope_explosion if entry and entry.scope_explosion
                     else str((onto.cost_model_meta.get("defaults") or {}).get("scope_explosion", "medium")))

        reasons: list[str] = [
            f"cost={round(cost, 4)} (ontology-derived)",
            f"relaxation_priority={priority}",
            f"scope_explosion={explosion}",
        ]
        if entry and entry.tier:
            reasons.append(f"tier={entry.tier}")
        if entry and entry.causal_layer:
            reasons.append(f"causal_layer={entry.causal_layer}")
        if scenario_hits:
            reasons.append(f"matched_scenarios={scenario_hits}")
        reasons.append(f"emit_count={len(records)}")

        scenario_ids_for_family = [
            s.scenario_id for s in matched if family in s.involved_families
        ]

        candidates.append(
            RankedCandidate(
                family=family,
                score=round(-cost, 4),    # 높을수록 우선 (legacy 호환)
                relaxation_priority=priority,
                scope_explosion=explosion,
                emit_count=len(records),
                cost=round(cost, 4),
                matched_scenario_ids=scenario_ids_for_family,
                reasons=reasons,
                sample_records=[r.to_dict() for r in records[:sample_per_family]],
            )
        )
    candidates.sort(key=lambda c: (c.cost, c.family))
    return candidates


# ---- scenario matching -----------------------------------------------------------


def _scenario_confidence(
    scenario: OntologyConflictScenario,
    by_family: dict[str, int],
) -> float:
    present = sum(1 for f in scenario.involved_families if by_family.get(f, 0) > 0)
    if present == 0:
        return 0.0
    return present / max(1, len(scenario.involved_families))


def match_known_conflict_scenarios(
    *,
    emit_records: list[EmittedConstraint],
    ontology: ConstraintOntology | None = None,
    min_confidence: float = 0.5,
) -> list[MatchedScenario]:
    onto = ontology or get_default_ontology()
    by_family: dict[str, int] = {}
    for r in emit_records or []:
        if r.mode != "enforced":
            continue
        by_family[r.family] = by_family.get(r.family, 0) + 1

    matched: list[MatchedScenario] = []
    for scenario in onto.conflict_scenarios:
        conf = _scenario_confidence(scenario, by_family)
        if conf < min_confidence:
            continue
        matched.append(
            MatchedScenario(
                scenario_id=scenario.scenario_id,
                involved_families=list(scenario.involved_families),
                confidence=round(conf, 3),
                suggested_relaxation=scenario.suggested_relaxation,
                why_infeasible=scenario.why_infeasible,
                detection_hint=scenario.detection_hint,
            )
        )
    matched.sort(key=lambda m: (-m.confidence, m.scenario_id))
    return matched


# ---- probe plan ------------------------------------------------------------------


def build_probe_plan(
    *,
    ranked_candidates: list[RankedCandidate],
    max_steps: int = 5,
) -> list[ProbeStep]:
    plan: list[ProbeStep] = []
    for i, cand in enumerate(ranked_candidates[:max_steps]):
        rationale_parts = [f"score={cand.score}"]
        if cand.matched_scenario_ids:
            rationale_parts.append(f"scenarios={','.join(cand.matched_scenario_ids)}")
        rationale_parts.append(f"priority={cand.relaxation_priority}")
        plan.append(
            ProbeStep(
                order=i + 1,
                family=cand.family,
                action={
                    "family": cand.family,
                    "action": "disable_module",
                    "reason": f"conflict_probe step {i + 1}",
                },
                rationale="; ".join(rationale_parts),
            )
        )
    return plan


# ---- public entry ----------------------------------------------------------------


def build_conflict_probe_report(
    *,
    emit_records: list[EmittedConstraint],
    ontology: ConstraintOntology | None = None,
    max_probe_steps: int = 5,
) -> ConflictProbeReport:
    onto = ontology or get_default_ontology()
    matched = match_known_conflict_scenarios(emit_records=emit_records, ontology=onto)
    ranked = rank_relaxation_candidates(
        emit_records=emit_records, ontology=onto, matched_scenarios=matched
    )
    plan = build_probe_plan(ranked_candidates=ranked, max_steps=max_probe_steps)
    notes: list[str] = []
    if not emit_records:
        notes.append("no emit records — solver did not run or no instrumented family fired")
    if not ranked:
        notes.append(
            "no ranked candidates — every emit family is bypassed_by_fixed or matrix-disabled"
        )
    if not matched:
        notes.append("no known conflict scenario matched — fall back to ranking only")
    return ConflictProbeReport(
        ranked_candidates=ranked,
        matched_scenarios=matched,
        probe_plan=plan,
        notes=notes,
    )


__all__ = [
    "ConflictProbeReport",
    "MatchedScenario",
    "ProbeStep",
    "RankedCandidate",
    "build_conflict_probe_report",
    "build_probe_plan",
    "match_known_conflict_scenarios",
    "rank_relaxation_candidates",
]
