"""Ontology consistency audit harness — 정합성 회귀.

새 하드제약 / cause / treatment / config_key 가 추가될 때 정합성이 자동 강제됨.
이 테스트는 9개 invariant 모두 통과해야 한다 (audit_all() 결과 비어있음).

새 제약 추가 후 이 테스트가 빨강이면 docs/ONTOLOGY_HARNESS_CHECKLIST.md 따라 보강.
"""

from __future__ import annotations

import pytest

from services.semantics.ontology_audit import (
    audit_all,
    audit_summary,
    check_I1_cause_has_problem_template,
    check_I2_cause_has_treatment,
    check_I3_treatment_applies_to_valid_causes,
    check_I4_treatment_has_text,
    check_I5_config_key_has_friendly_label,
    check_I6_direction_has_friendly_label,
    check_I7_constraint_family_has_cause_mapping,
    check_I8_matrix_factories_produce_valid_causes,
    check_I9_cause_category_is_known,
)


def test_audit_all_passes_zero_findings():
    findings = audit_all()
    s = audit_summary(findings)
    assert s["pass"], (
        f"Ontology consistency audit FAILED: {s['total']} findings\n"
        f"by_invariant: {s['by_invariant']}\n"
        f"by_severity: {s['by_severity']}\n"
        f"first 5 findings:\n" +
        "\n".join(
            f"  [{f['invariant']}] {f['severity']} {f['target']}: {f['detail'][:200]}"
            for f in s["findings"][:5]
        )
    )


# ── Fail-mode tests — audit 가 실제 gap 을 잡는지 검증 (harness 자체 회귀) ────
def test_audit_catches_dangling_treatment_reference():
    """treatment 의 applies_to_causes 에 존재하지 않는 cause_id 가 있으면 I3 fail."""
    from services.semantics.ontology import (
        ConstraintOntology, OntologyCause, OntologyTreatment,
    )
    onto = ConstraintOntology.__new__(ConstraintOntology)
    onto.constraints = {}; onto.overrides = {}; onto.modes = {}
    onto.relations = {}; onto.conflict_scenarios = []
    onto._alias_to_id = {}; onto.cost_model_meta = {}
    onto.parent_to_default_causal_layer = {}
    onto.causes = {
        "cause:test:real": OntologyCause(
            cause_id="cause:test:real", label="test", category="capacity",
            causal_layer="policy", tier="T1", is_hard=True,
            problem_template_ko="real cause",
        )
    }
    onto._cause_alias_to_id = {"CAUSE:TEST:REAL": "cause:test:real"}
    onto.treatments = {
        "treatment:test:dangling": OntologyTreatment(
            treatment_id="treatment:test:dangling",
            label="t", action_type="force_soft_mode", target_family="X",
            config_key=None, direction="enable",
            rationale_ko="r", trade_off_ko="t",
            applies_to_causes=["cause:test:nonexistent"],  # ← dangling!
        )
    }
    findings = check_I3_treatment_applies_to_valid_causes(onto)
    assert findings, "I3 가 dangling reference 를 잡지 못함"
    assert any("nonexistent" in f.detail for f in findings)


def test_audit_catches_missing_problem_template():
    """problem_template_ko 비어있으면 I1 fail."""
    from services.semantics.ontology import ConstraintOntology, OntologyCause
    onto = ConstraintOntology.__new__(ConstraintOntology)
    onto.causes = {
        "cause:test:empty_template": OntologyCause(
            cause_id="cause:test:empty_template", label="x", category="capacity",
            causal_layer="policy", tier="T1", is_hard=True,
            problem_template_ko="",   # ← 비어있음
        )
    }
    findings = check_I1_cause_has_problem_template(onto)
    assert findings, "I1 가 빈 template 를 잡지 못함"


def test_audit_catches_unknown_category():
    """cause.category 가 알려진 set 에 없으면 I9 fail."""
    from services.semantics.ontology import ConstraintOntology, OntologyCause
    onto = ConstraintOntology.__new__(ConstraintOntology)
    onto.causes = {
        "cause:test:typo": OntologyCause(
            cause_id="cause:test:typo", label="x",
            category="capacti",   # ← 오타
            causal_layer="policy", tier="T1", is_hard=True,
            problem_template_ko="x",
        )
    }
    findings = check_I9_cause_category_is_known(onto)
    assert findings, "I9 가 unknown category(오타) 를 잡지 못함"


@pytest.mark.parametrize("check_name,check_fn", [
    ("I1 cause.problem_template_ko",         check_I1_cause_has_problem_template),
    ("I2 cause ≥1 treatment",                check_I2_cause_has_treatment),
    ("I3 treatment.applies_to_causes valid", check_I3_treatment_applies_to_valid_causes),
    ("I4 treatment rationale+trade_off",     check_I4_treatment_has_text),
    ("I5 config_key 친화 라벨",               check_I5_config_key_has_friendly_label),
    ("I6 direction 친화 라벨",                check_I6_direction_has_friendly_label),
    ("I7 family ↔ MUS token mapping",        check_I7_constraint_family_has_cause_mapping),
    ("I8 matrix factory cause_id 정합",       check_I8_matrix_factories_produce_valid_causes),
    ("I9 cause.category known set",          check_I9_cause_category_is_known),
])
def test_each_invariant_passes(check_name, check_fn):
    """각 invariant 별 독립 회귀 — 어느 check 가 fail 인지 명확히."""
    from services.semantics.ontology import get_default_ontology
    findings = check_fn(get_default_ontology())
    assert not findings, (
        f"{check_name} FAILED ({len(findings)} findings):\n" +
        "\n".join(f"  - {f.target}: {f.detail[:200]}" for f in findings[:5])
    )
