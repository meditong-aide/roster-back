"""US-2 검증 — Cost function 이 완전히 ontology-derived. magic constant 없음.

핵심 invariants:
  1. cost 의 모든 수치는 ontology.yaml meta.cost_model 에서 derive.
  2. cost_model_meta 가 없으면 CostModelError raise (silent fallback 금지).
  3. priority 낮은 family 의 cost 가 높은 family 보다 작거나 같다.
  4. scope_explosion=high 가 low 보다 cost 가 크거나 같다.
  5. tier T0 (config integrity) cost 는 T2 보다 훨씬 크다 (사실상 후보 제외).
  6. ward_profile=forbid 면 cost=None (= ∞).
  7. ward_profile=prefer 가 neutral 보다 cost 작다 (prefer 가중치 multiplier < 1).
  8. ward_profile=avoid 가 neutral 보다 cost 크다.
  9. scenario_match_bonus 가 음수이면 매칭 1건이 cost 를 감소시킨다.
 10. conflict_probe.rank_relaxation_candidates 는 cost_model 만 통해 순위 결정 (magic 0).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.cost_model import (
    CostModelError,
    compute_base_cost,
    compute_treatment_cost,
)
from services.semantics.ontology import ConstraintOntology, get_default_ontology


# ─────────────────────────────────────────────────────────────────────────
# 1. cost_model_meta 가 비면 raise
# ─────────────────────────────────────────────────────────────────────────
def test_cost_model_error_when_meta_missing(tmp_path):
    yaml_text = "version: 1\nconstraints: {}\n"  # meta 없음
    p = tmp_path / "min_ontology.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    onto = ConstraintOntology(path=p)
    with pytest.raises(CostModelError):
        compute_base_cost("TeamMin", onto)


# ─────────────────────────────────────────────────────────────────────────
# 2. base cost 순서 — priority 단조
# ─────────────────────────────────────────────────────────────────────────
def test_priority_low_yields_lower_cost_than_priority_high():
    onto = get_default_ontology()
    # TeamMin / GradeMax: relaxation_priority=2 (CoverageConstraint)
    # OffCap / NightRecovery: relaxation_priority=5 (NurseLocalConstraint)
    cost_low_priority = compute_base_cost("TeamMin", onto)
    cost_high_priority = compute_base_cost("OffCap", onto)
    assert cost_low_priority < cost_high_priority, (
        f"priority 낮은 TeamMin cost({cost_low_priority}) 가 OffCap({cost_high_priority}) 보다 작아야 함"
    )


# ─────────────────────────────────────────────────────────────────────────
# 3. scope_explosion 순서 — low ≤ medium ≤ high (다른 조건 동일 시)
# ─────────────────────────────────────────────────────────────────────────
def test_scope_explosion_monotonic_via_yaml_table(tmp_path):
    yaml_text = """
version: 1
meta:
  cost_model:
    components:
      priority:
        derivation: relaxation_priority
      scope_explosion:
        low: 0
        medium: 1
        high: 2
      tier:
        T2: 0
      causal_layer:
        policy: 0
    scenario_match_bonus: 0
    ward_profile_multipliers:
      forbid: null
      neutral: 1.0
    defaults:
      priority: 3
      scope_explosion: medium
      tier: T2
      causal_layer: policy
constraints:
  L: { label: L, parent: CoverageConstraint, scope: [day], effective_modes: [enforced], connects: [], explanation_template: "", relaxation_priority: 2, scope_explosion: low, tier: T2 }
  M: { label: M, parent: CoverageConstraint, scope: [day], effective_modes: [enforced], connects: [], explanation_template: "", relaxation_priority: 2, scope_explosion: medium, tier: T2 }
  H: { label: H, parent: CoverageConstraint, scope: [day], effective_modes: [enforced], connects: [], explanation_template: "", relaxation_priority: 2, scope_explosion: high, tier: T2 }
"""
    p = tmp_path / "ontology.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    onto = ConstraintOntology(path=p)
    cL = compute_base_cost("L", onto)
    cM = compute_base_cost("M", onto)
    cH = compute_base_cost("H", onto)
    assert cL <= cM <= cH


# ─────────────────────────────────────────────────────────────────────────
# 4. tier T0 (config integrity) cost 가 T2 보다 훨씬 큼
# ─────────────────────────────────────────────────────────────────────────
def test_tier_t0_cost_far_exceeds_t2():
    onto = get_default_ontology()
    # ConfigIntegrity: T0
    # TeamMin: T2
    t0_cost = compute_base_cost("ConfigIntegrity", onto)
    t2_cost = compute_base_cost("TeamMin", onto)
    assert t0_cost > t2_cost + 100, (
        f"T0 cost ({t0_cost}) 가 T2 ({t2_cost}) 보다 충분히 커야 함 — 사실상 후보 제외"
    )


# ─────────────────────────────────────────────────────────────────────────
# 5. ward profile — forbid → None / avoid > neutral > prefer
# ─────────────────────────────────────────────────────────────────────────
def test_ward_profile_forbid_yields_none():
    onto = get_default_ontology()
    cost = compute_treatment_cost("TeamMin", onto, ward_profile={"TeamMin": "forbid"})
    assert cost is None


def test_ward_profile_prefer_less_than_neutral_less_than_avoid():
    onto = get_default_ontology()
    c_prefer = compute_treatment_cost("TeamMin", onto, ward_profile={"TeamMin": "prefer"})
    c_neutral = compute_treatment_cost("TeamMin", onto, ward_profile={"TeamMin": "neutral"})
    c_avoid = compute_treatment_cost("TeamMin", onto, ward_profile={"TeamMin": "avoid"})
    assert c_prefer is not None and c_neutral is not None and c_avoid is not None
    assert c_prefer < c_neutral < c_avoid


def test_ward_profile_missing_key_defaults_to_neutral():
    onto = get_default_ontology()
    c_with = compute_treatment_cost("TeamMin", onto, ward_profile={"OffCap": "forbid"})
    c_default = compute_treatment_cost("TeamMin", onto)
    # TeamMin 에 정책 없음 — neutral 로 fallback. 동일 cost.
    assert c_with == c_default


# ─────────────────────────────────────────────────────────────────────────
# 6. scenario_match_bonus 가 음수 → 매칭 1건 = cost 감소
# ─────────────────────────────────────────────────────────────────────────
def test_scenario_match_bonus_reduces_cost_when_negative():
    onto = get_default_ontology()
    bonus = float((onto.cost_model_meta or {}).get("scenario_match_bonus", 0))
    if bonus >= 0:
        pytest.skip("scenario_match_bonus 가 음수가 아니면 본 테스트 의미 없음")
    c0 = compute_base_cost("TeamMin", onto, matched_scenario_count=0)
    c2 = compute_base_cost("TeamMin", onto, matched_scenario_count=2)
    assert c2 < c0
    assert c2 == c0 + bonus * 2


# ─────────────────────────────────────────────────────────────────────────
# 7. conflict_probe 가 magic constant 없이 cost_model 만 사용 — 코드 audit
# ─────────────────────────────────────────────────────────────────────────
def test_conflict_probe_has_no_magic_numbers():
    """code review-style: conflict_probe.py 에 cost 산식 magic number 없음."""
    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "constraint_impact" / "conflict_probe.py").read_text(encoding="utf-8")
    # 옛 magic 들이 모두 사라졌는지
    assert "_SCOPE_EXPLOSION_PENALTY" not in src
    assert "_DEFAULT_RELAXATION_PRIORITY = 3" not in src
    # 옛 산식 6 - priority 흔적 없음 (단, '6' 자체는 max_steps 등에 들어갈 수 있음)
    assert "6 - priority" not in src
    # scenario_hits * 0.5 magic 없음
    assert re.search(r"scenario_hits\s*\*\s*0\.5", src) is None
    # cost_model 호출 존재
    assert "compute_treatment_cost" in src


# ─────────────────────────────────────────────────────────────────────────
# 8. 옛 conflict_probe 테스트 회귀 — 순서 invariant 보존
# ─────────────────────────────────────────────────────────────────────────
def test_legacy_ranking_order_preserved_team_min_before_off_cap():
    from services.constraint_impact.conflict_probe import rank_relaxation_candidates
    from services.constraint_impact.solver_emit import EmittedConstraint

    records = [
        EmittedConstraint(family="TeamMin", scope={"team": "1", "day": 1}, target="x", mode="enforced"),
        EmittedConstraint(family="OffCap", scope={"nurse_index": 0}, target="x", mode="enforced"),
        EmittedConstraint(family="GradeMax", scope={"grade": 1, "day": 1}, target="x", mode="enforced"),
    ]
    ranked = rank_relaxation_candidates(emit_records=records)
    families = [c.family for c in ranked]
    assert families.index("TeamMin") < families.index("OffCap")
    assert families.index("GradeMax") < families.index("OffCap")
    # 가장 마지막은 OffCap (priority=5, T1)
    assert ranked[-1].family == "OffCap"
    # cost 값 노출 (US-2 신규 필드)
    assert all(c.cost is not None for c in ranked)
    # score 와 cost 의 관계: score = -cost (legacy 호환)
    for c in ranked:
        assert abs(c.score + c.cost) < 1e-6


def test_ward_profile_forbid_removes_candidate():
    from services.constraint_impact.conflict_probe import rank_relaxation_candidates
    from services.constraint_impact.solver_emit import EmittedConstraint

    records = [
        EmittedConstraint(family="TeamMin", scope={"team": "1"}, target="x", mode="enforced"),
        EmittedConstraint(family="GradeMax", scope={"grade": 1}, target="x", mode="enforced"),
    ]
    ranked = rank_relaxation_candidates(
        emit_records=records,
        ward_profile={"TeamMin": "forbid"},
    )
    families = [c.family for c in ranked]
    assert "TeamMin" not in families
    assert "GradeMax" in families
