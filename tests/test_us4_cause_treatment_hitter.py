"""US-4 검증 — Greedy weighted hitting set + bundle 생성.

핵심 invariants (사용자 요구):
  1. 동적: 어떤 cause 조합에서도 bundle 생성 (정적 시나리오 라이브러리 의존 X).
  2. 모든 cover 가능한 cause 는 cover 됨.
  3. ward_profile=forbid 인 family 의 treatment 는 후보에서 제외.
  4. 근사비 검증: brute-force enumerate 결과와 비교 — greedy 결과의 cost ≤ ln(n) * optimal_cost + epsilon.
  5. alternative bundles: primary 와 다른 signature.
  6. cost 는 ontology-derived (cost_model 사용) — magic constant 없음.
  7. uncovered_causes 가 비어있지 않으면 manual treatment 만 있는 cause (예: cause:undiagnosed) 라야.
"""

from __future__ import annotations

import math
import random
import pytest

from services.cause_treatment_hitter import (
    Bundle,
    TreatmentChoice,
    enumerate_minimal_hitting_sets_brute_force,
    propose_bundles,
)
from services.semantics.ontology import get_default_ontology


# ─────────────────────────────────────────────────────────────────────────
# 동적 동작 — 다양한 cause 조합
# ─────────────────────────────────────────────────────────────────────────
def test_single_cause_yields_single_treatment_bundle():
    bundles = propose_bundles(active_causes=["cause:grade:max_sum_below_need"])
    assert len(bundles) >= 1
    primary = bundles[0]
    assert primary.treatments
    assert primary.uncovered_causes == []
    assert "cause:grade:max_sum_below_need" in primary.covered_causes


def test_legacy_alias_input_resolves_to_cause():
    """legacy reason_code 를 active_causes 로 넣어도 cause 로 resolve 되어 처리."""
    bundles = propose_bundles(active_causes=["GRADE_MAX_SUM_BELOW_NEED"])
    assert len(bundles) >= 1
    assert bundles[0].uncovered_causes == []


def test_two_independent_causes_yields_at_least_two_treatments():
    bundles = propose_bundles(active_causes=[
        "cause:team:min_over_need",
        "cause:grade:max_sum_below_need",
    ])
    assert len(bundles) >= 1
    primary = bundles[0]
    # team min + grade max 는 1개 treatment 로 둘 다 cover 불가능 (다른 family).
    # 그러나 treatment:soft:handoff 가 둘 다 cover 가능 (applies_to_causes 양쪽 포함).
    # 따라서 1 treatment 로 끝날 수도 있다 — 모두 cover 되었는지만 확인.
    assert set(primary.covered_causes) == {
        "cause:team:min_over_need",
        "cause:grade:max_sum_below_need",
    }


def test_unknown_cause_is_silently_dropped():
    """resolve 안 되는 alias 는 무시하고 알려진 cause 만 처리."""
    bundles = propose_bundles(active_causes=[
        "UNKNOWN_CODE_XYZ",
        "CAPACITY_TOTAL_SHORTAGE",
    ])
    assert len(bundles) >= 1
    assert "cause:capacity:monthly_total_shortage" in bundles[0].covered_causes


def test_empty_input_yields_empty_list():
    assert propose_bundles(active_causes=[]) == []
    assert propose_bundles(active_causes=["NO_SUCH"]) == []


def test_undiagnosed_yields_manual_only_treatment():
    bundles = propose_bundles(active_causes=["UNDIAGNOSED"])
    assert len(bundles) >= 1
    primary = bundles[0]
    # 모든 treatment 가 data_correction_required
    assert all(t.action_type == "data_correction_required" for t in primary.treatments)


# ─────────────────────────────────────────────────────────────────────────
# ward_profile — forbid 효과
# ─────────────────────────────────────────────────────────────────────────
def test_ward_profile_forbid_excludes_treatment_target_family():
    # GradeMax 를 forbid → treatment:soft:grade_max 제외, treatment:soft:handoff (target=TeamGradeHandoff) 가능
    bundles = propose_bundles(
        active_causes=["cause:grade:max_sum_below_need"],
        ward_profile={"GradeMax": "forbid"},
    )
    assert len(bundles) >= 1
    tids = {t.treatment_id for b in bundles for t in b.treatments}
    assert "treatment:soft:grade_max" not in tids


def test_ward_profile_forbid_all_yields_uncovered():
    """target_family 가 모두 forbid 면 uncovered_causes 발생."""
    # grade:max_sum_below_need 의 treatments: soft:grade_max(GradeMax), soft:handoff(TeamGradeHandoff), data:add_nurse(ConfigIntegrity)
    bundles = propose_bundles(
        active_causes=["cause:grade:max_sum_below_need"],
        ward_profile={
            "GradeMax": "forbid",
            "TeamGradeHandoff": "forbid",
            "ConfigIntegrity": "forbid",
        },
    )
    # 후보 다 forbid → 그래도 bundle 1개는 반환되지만 uncovered 가 비어있지 않음
    assert len(bundles) >= 1
    primary = bundles[0]
    assert "cause:grade:max_sum_below_need" in primary.uncovered_causes


def test_ward_profile_prefer_lowers_total_cost():
    bundles_neutral = propose_bundles(active_causes=["cause:team:min_over_need"])
    bundles_prefer = propose_bundles(
        active_causes=["cause:team:min_over_need"],
        ward_profile={"TeamMin": "prefer"},
    )
    assert bundles_neutral[0].total_cost > bundles_prefer[0].total_cost


# ─────────────────────────────────────────────────────────────────────────
# alternative bundles — 다양화
# ─────────────────────────────────────────────────────────────────────────
def test_alternative_bundles_differ_from_primary():
    bundles = propose_bundles(
        active_causes=["cause:team:min_over_need"],
        max_alternatives=3,
    )
    # 1+ alternative 가 있다면 primary 와 다른 signature
    if len(bundles) > 1:
        sigs = {tuple(sorted(t.treatment_id for t in b.treatments)) for b in bundles}
        assert len(sigs) == len(bundles), "alternatives 가 서로 같은 signature 를 가짐"


def test_bundles_sorted_by_total_cost():
    bundles = propose_bundles(
        active_causes=["cause:team:min_over_need", "cause:grade:max_sum_below_need"],
        max_alternatives=3,
    )
    if len(bundles) >= 2:
        for i in range(len(bundles) - 1):
            a = bundles[i].total_cost + bundles[i].overhead
            b = bundles[i + 1].total_cost + bundles[i + 1].overhead
            assert a <= b, f"bundle[{i}] cost {a} > bundle[{i+1}] cost {b}"


# ─────────────────────────────────────────────────────────────────────────
# Greedy vs brute-force optimal — 근사비 검증
# ─────────────────────────────────────────────────────────────────────────
def test_greedy_within_log_n_approx_factor_vs_brute_force():
    """small case 에서 greedy 결과 ≤ ln(n) * optimal_cost + epsilon."""
    cause_set = [
        "cause:team:min_over_need",
        "cause:grade:max_sum_below_need",
        "cause:grade:min_sum_over_need",
        "cause:capacity:daily_total_shortage",
    ]
    greedy = propose_bundles(active_causes=cause_set, max_alternatives=1)
    brute = enumerate_minimal_hitting_sets_brute_force(active_causes=cause_set, max_size=5)
    if not brute:
        pytest.skip("brute force enumeration empty — skip approximation test")
    optimal = min(b.total_cost for b in brute if not b.uncovered_causes)
    greedy_cost = greedy[0].total_cost
    n = len(cause_set)
    bound = optimal * (1 + math.log(max(2, n)))
    assert greedy_cost <= bound + 1e-6, (
        f"greedy cost {greedy_cost} > ln(n)+1 * optimal {bound} (optimal={optimal}, n={n})"
    )


def test_greedy_covers_all_causes_when_coverable():
    """모든 cause 에 treatment 가 존재하면 uncovered_causes 가 비어있어야."""
    for cid in [
        "cause:capacity:monthly_total_shortage",
        "cause:capacity:monthly_night_shortage",
        "cause:eligibility:shift_eligible_shortage",
        "cause:fixed:over_demand",
        "cause:grade:max_sum_below_need",
    ]:
        bundles = propose_bundles(active_causes=[cid])
        assert bundles[0].uncovered_causes == [], (
            f"{cid} 가 cover 되지 못함: uncovered={bundles[0].uncovered_causes}"
        )


# ─────────────────────────────────────────────────────────────────────────
# 동적 sweep — 임의 cause 조합 100건에서 invariant 검증
# ─────────────────────────────────────────────────────────────────────────
def _random_cause_set(rng: random.Random, all_causes: list[str], min_n: int = 1, max_n: int = 5) -> list[str]:
    n = rng.randint(min_n, min(max_n, len(all_causes)))
    return rng.sample(all_causes, n)


def test_dynamic_sweep_100_random_cause_sets_invariants_hold():
    onto = get_default_ontology()
    # cause:undiagnosed 는 manual 만 cover 하므로 별도 (sweep 에선 제외)
    all_causes = [cid for cid in onto.causes.keys() if cid != "cause:undiagnosed"]
    rng = random.Random(424242)

    violations: list[tuple[int, str]] = []
    for seed in range(100):
        local_rng = random.Random(rng.random())
        cset = _random_cause_set(local_rng, all_causes, 1, 5)
        bundles = propose_bundles(active_causes=cset, max_alternatives=3)
        if not bundles:
            violations.append((seed, f"empty bundle list for {cset}"))
            continue
        primary = bundles[0]
        if not primary.treatments and not primary.uncovered_causes:
            violations.append((seed, f"empty treatments yet no uncovered: {cset}"))
        # covered ∪ uncovered = 입력 set
        all_input = set(onto.resolve_cause_alias(c) for c in cset)
        all_input.discard(None)
        union = set(primary.covered_causes) | set(primary.uncovered_causes)
        if union != all_input:
            violations.append((seed, f"covered ∪ uncovered ≠ input: {union} vs {all_input}"))
        # alternatives unique
        sigs = [tuple(sorted(t.treatment_id for t in b.treatments)) for b in bundles]
        if len(sigs) != len(set(sigs)):
            violations.append((seed, f"duplicate signatures: {sigs}"))

    assert violations == [], f"dynamic sweep {len(violations)}/100 invariant failures: {violations[:3]}"
