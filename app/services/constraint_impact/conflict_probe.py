"""Conflict Probe — UNSAT 진단 및 자동 relaxation 후보 ranking.

본 모듈은 진단/추천만 수행. 실제 disable 시뮬레이션 실행은 control layer 에서 호출.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from services.constraint_impact.solver_emit import EmittedConstraint
from services.semantics.ontology import (
    ConstraintOntology,
    OntologyConflictScenario,
    get_default_ontology,
)


_SCOPE_EXPLOSION_PENALTY = {"low": 0.0, "medium": -0.5, "high": -1.0}
_DEFAULT_RELAXATION_PRIORITY = 3
_DEFAULT_SCOPE_EXPLOSION = "medium"


@dataclass(slots=True)
class RankedCandidate:
    family: str
    score: float
    relaxation_priority: int
    scope_explosion: str
    emit_count: int
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
) -> list[RankedCandidate]:
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
        priority = onto.get_relaxation_priority(family) or _DEFAULT_RELAXATION_PRIORITY
        explosion = onto.get_scope_explosion(family) or _DEFAULT_SCOPE_EXPLOSION

        # base: 풀기 쉬운 (priority 낮은) 일수록 높은 점수
        score = float(6 - priority)
        # scope_explosion 페널티 — 너무 많이 풀면 부작용
        score += _SCOPE_EXPLOSION_PENALTY.get(explosion, _SCOPE_EXPLOSION_PENALTY[_DEFAULT_SCOPE_EXPLOSION])
        # scenario 매칭 가산점
        scenario_hits = scenario_family_count.get(family, 0)
        score += scenario_hits * 0.5

        reasons: list[str] = [f"relaxation_priority={priority} (낮을수록 풀기 쉬움)"]
        reasons.append(f"scope_explosion={explosion}")
        if scenario_hits:
            reasons.append(f"matched_scenarios={scenario_hits}")
        reasons.append(f"emit_count={len(records)}")

        scenario_ids_for_family = [
            s.scenario_id for s in matched if family in s.involved_families
        ]

        candidates.append(
            RankedCandidate(
                family=family,
                score=round(score, 4),
                relaxation_priority=priority,
                scope_explosion=explosion,
                emit_count=len(records),
                matched_scenario_ids=scenario_ids_for_family,
                reasons=reasons,
                sample_records=[r.to_dict() for r in records[:sample_per_family]],
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.family))
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
