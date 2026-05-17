"""Cause-Treatment Hitter — 동적 hitting set + bundle 후보 생성 (US-4).

설계 (사용자 핵심 요구):
  - cause 가 식별되면 그 cause 들을 cover 하는 최소 비용 treatment 묶음 bundle 을 **동적** 생성.
  - 정적 시나리오 라이브러리 의존 없음 — 어떤 새 cause 조합에도 동작.
  - cost 는 services.cost_model.compute_treatment_cost (ontology-derived) — magic 가중치 0.
  - ward_profile (forbid/avoid/neutral/prefer) 자연스럽게 반영.

알고리즘:
  Chvátal (1979) 의 weighted greedy set cover (근사비 O(ln n)).
  대안 bundle 은 'most-expensive treatment 제거 후 재계산' 으로 다양화 (HS-tree 의 가지).

bundle 출력 구조:
  - treatments[]: 각 atomic treatment 의 rationale_ko, trade_off_ko, config_key, direction
  - covered_causes / uncovered_causes: 해결 가능 / manual 필요 분리
  - total_cost + overhead: 정렬 키
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Mapping, Optional

from services.cost_model import compute_treatment_cost
from services.semantics.ontology import (
    ConstraintOntology,
    OntologyTreatment,
    get_default_ontology,
)


@dataclass(slots=True)
class TreatmentChoice:
    treatment_id: str
    target_family: str
    action_type: str
    config_key: str | None
    direction: str
    rationale_ko: str
    trade_off_ko: str
    cost: float
    covers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Bundle:
    bundle_id: str
    treatments: list[TreatmentChoice]
    total_cost: float
    covered_causes: list[str]
    uncovered_causes: list[str]
    overhead: float = 0.0


def propose_bundles(
    *,
    active_causes: list[str],
    ontology: Optional[ConstraintOntology] = None,
    ward_profile: Optional[Mapping[str, str]] = None,
    matched_scenario_count_by_family: Optional[Mapping[str, int]] = None,
    max_alternatives: int = 3,
    bundle_overhead_per_extra: Optional[float] = None,
) -> list[Bundle]:
    """active_causes 를 cover 하는 bundle 후보들 (cost 오름차순).

    bundle_overhead_per_extra:
      None 이면 ontology meta.cost_model.bundle_overhead_per_extra 에서 derive.
      명시값은 호출자가 override.
    """
    onto = ontology or get_default_ontology()
    matched_count = dict(matched_scenario_count_by_family or {})
    if bundle_overhead_per_extra is None:
        meta = getattr(onto, "cost_model_meta", None) or {}
        bundle_overhead_per_extra = float(meta.get("bundle_overhead_per_extra", 0))

    # 1. cause alias → 정식 id normalize
    causes_normalized: list[str] = []
    seen: set[str] = set()
    for raw in active_causes:
        resolved = onto.resolve_cause_alias(raw)
        if resolved and resolved not in seen:
            causes_normalized.append(resolved)
            seen.add(resolved)
    if not causes_normalized:
        return []

    # 2. cause → 후보 treatment (forbid 제외, cost-tagged)
    cause_to_treatments: dict[str, list[tuple[OntologyTreatment, float]]] = {}
    for cid in causes_normalized:
        candidates: list[tuple[OntologyTreatment, float]] = []
        for t in onto.treatments_for_cause(cid):
            cost = compute_treatment_cost(
                t.target_family,
                onto,
                ward_profile=ward_profile,
                matched_scenario_count=int(matched_count.get(t.target_family, 0)),
            )
            if cost is None:
                continue
            candidates.append((t, float(cost)))
        cause_to_treatments[cid] = candidates

    # 3. Primary bundle: greedy weighted set cover
    primary = _greedy_set_cover(causes_normalized, cause_to_treatments)
    primary.overhead = bundle_overhead_per_extra * max(0, len(primary.treatments) - 1)
    bundles: list[Bundle] = [primary]

    # 4. Alternative bundles: 비싼 treatment 1개 제외 후 재계산 (다양화)
    seen_sigs: set[tuple] = {_bundle_signature(primary)}
    iterations = 0
    while len(bundles) < max_alternatives and iterations < max_alternatives * 4:
        iterations += 1
        excluded = _most_expensive_unique_treatment(bundles)
        if not excluded:
            break
        alt_ct = {
            cid: [(t, c) for t, c in cands if t.treatment_id not in excluded]
            for cid, cands in cause_to_treatments.items()
        }
        alt = _greedy_set_cover(causes_normalized, alt_ct)
        sig = _bundle_signature(alt)
        if sig in seen_sigs or not alt.treatments:
            continue
        seen_sigs.add(sig)
        alt.overhead = bundle_overhead_per_extra * max(0, len(alt.treatments) - 1)
        bundles.append(alt)

    # 정렬 우선순위:
    #   1) uncovered 적은 것 (전체 cover 가능한 bundle 가 있으면 그것 primary)
    #   2) cost 낮은 것
    #   3) treatment 수 적은 것
    # 사용자 정신: "추천대로 적용하면 cause 가 실제 해결돼야 한다".
    # cost 만으로 정렬하면 partial-cover bundle 이 full-cover 위로 가서 추천 실패.
    bundles.sort(key=lambda b: (len(b.uncovered_causes), b.total_cost + b.overhead, len(b.treatments)))
    return bundles[:max_alternatives]


def enumerate_minimal_hitting_sets_brute_force(
    *,
    active_causes: list[str],
    ontology: Optional[ConstraintOntology] = None,
    ward_profile: Optional[Mapping[str, str]] = None,
    max_size: int = 6,
) -> list[Bundle]:
    """작은 케이스 (≤ max_size treatments) 용 brute-force enumeration.

    greedy 의 근사비를 검증할 때 사용. 운영에선 사용 금지 (지수).
    """
    onto = ontology or get_default_ontology()
    causes_normalized = [c for c in (onto.resolve_cause_alias(x) for x in active_causes) if c]
    if not causes_normalized:
        return []

    cause_to_treatments: dict[str, list[tuple[OntologyTreatment, float]]] = {}
    all_tids: set[str] = set()
    for cid in causes_normalized:
        cands: list[tuple[OntologyTreatment, float]] = []
        for t in onto.treatments_for_cause(cid):
            cost = compute_treatment_cost(t.target_family, onto, ward_profile=ward_profile)
            if cost is None:
                continue
            cands.append((t, float(cost)))
            all_tids.add(t.treatment_id)
        cause_to_treatments[cid] = cands

    treatment_to_causes: dict[str, set[str]] = {}
    treatment_to_obj: dict[str, OntologyTreatment] = {}
    treatment_to_cost: dict[str, float] = {}
    for cid, cands in cause_to_treatments.items():
        for t, c in cands:
            treatment_to_causes.setdefault(t.treatment_id, set()).add(cid)
            treatment_to_obj[t.treatment_id] = t
            treatment_to_cost[t.treatment_id] = c

    tids = sorted(all_tids)
    minimal_solutions: list[set[str]] = []
    universe = set(causes_normalized)
    for size in range(1, min(max_size, len(tids)) + 1):
        for combo in combinations(tids, size):
            covered: set[str] = set()
            for tid in combo:
                covered |= treatment_to_causes.get(tid, set())
            if not (universe <= covered):
                continue
            # minimal 검사: subset 으로 더 작은 cover 없는지
            subset_already_minimal = any(set(s) < set(combo) for s in minimal_solutions)
            if subset_already_minimal:
                continue
            minimal_solutions.append(set(combo))

    bundles: list[Bundle] = []
    for sol in minimal_solutions:
        treatments: list[TreatmentChoice] = []
        for tid in sorted(sol):
            t = treatment_to_obj[tid]
            treatments.append(TreatmentChoice(
                treatment_id=t.treatment_id,
                target_family=t.target_family,
                action_type=t.action_type,
                config_key=t.config_key,
                direction=t.direction,
                rationale_ko=t.rationale_ko,
                trade_off_ko=t.trade_off_ko,
                cost=treatment_to_cost[tid],
                covers=sorted(treatment_to_causes[tid] & universe),
            ))
        total_cost = sum(t.cost for t in treatments)
        bundles.append(Bundle(
            bundle_id=f"bundle:{','.join(sorted(sol))}",
            treatments=treatments,
            total_cost=total_cost,
            covered_causes=sorted(universe),
            uncovered_causes=[],
        ))
    bundles.sort(key=lambda b: (b.total_cost, len(b.treatments)))
    return bundles


# ─────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────
def _greedy_set_cover(
    causes: list[str],
    cause_to_treatments: dict[str, list[tuple[OntologyTreatment, float]]],
) -> Bundle:
    uncovered = set(causes)
    chosen: list[TreatmentChoice] = []

    treatment_to_causes: dict[str, set[str]] = {}
    treatment_to_cost: dict[str, float] = {}
    treatment_to_obj: dict[str, OntologyTreatment] = {}
    for cid, cands in cause_to_treatments.items():
        for t, cost in cands:
            treatment_to_causes.setdefault(t.treatment_id, set()).add(cid)
            treatment_to_cost[t.treatment_id] = cost
            treatment_to_obj[t.treatment_id] = t

    while uncovered:
        best_tid: str | None = None
        best_ratio: float | None = None
        for tid, covers in treatment_to_causes.items():
            new_cover = covers & uncovered
            if not new_cover:
                continue
            # ratio = cost per newly-covered cause (낮을수록 더 좋은 선택)
            ratio = treatment_to_cost[tid] / max(1, len(new_cover))
            if best_ratio is None or ratio < best_ratio:
                best_ratio = ratio
                best_tid = tid
        if best_tid is None:
            break
        new_covered = treatment_to_causes[best_tid] & uncovered
        t_obj = treatment_to_obj[best_tid]
        chosen.append(TreatmentChoice(
            treatment_id=t_obj.treatment_id,
            target_family=t_obj.target_family,
            action_type=t_obj.action_type,
            config_key=t_obj.config_key,
            direction=t_obj.direction,
            rationale_ko=t_obj.rationale_ko,
            trade_off_ko=t_obj.trade_off_ko,
            cost=treatment_to_cost[best_tid],
            covers=sorted(new_covered),
        ))
        uncovered -= new_covered

    covered = set(causes) - uncovered
    total_cost = sum(t.cost for t in chosen)
    bundle_id = "bundle:" + ",".join(sorted(c.treatment_id for c in chosen)) if chosen else "bundle:empty"
    return Bundle(
        bundle_id=bundle_id,
        treatments=chosen,
        total_cost=total_cost,
        covered_causes=sorted(covered),
        uncovered_causes=sorted(uncovered),
    )


def _bundle_signature(b: Bundle) -> tuple:
    return tuple(sorted(t.treatment_id for t in b.treatments))


def _most_expensive_unique_treatment(bundles: list[Bundle]) -> set[str]:
    """기존 bundle 들에서 가장 비싼 treatment ID 를 모아 다음 시도에서 제외 후보로."""
    out: set[str] = set()
    for b in bundles:
        if not b.treatments:
            continue
        most_expensive = max(b.treatments, key=lambda t: t.cost)
        out.add(most_expensive.treatment_id)
    return out
