"""US-6 검증 — Ontology v4 의 causes / treatments 카탈로그 무결성.

핵심 invariants:
  1. 모든 treatment.applies_to_causes 가 valid cause id 를 가리킨다.
  2. 모든 cause 가 적어도 1 개 treatment 에 의해 cover 된다.
  3. rationale_ko, trade_off_ko, problem_template_ko 가 비어있지 않다.
  4. alias 들이 unique (legacy reason_code 가 여러 cause 에 매핑되지 않음).
  5. cause-treatment edge 가 hitter 의 입력으로 동작 가능.
  6. naive 추천 패턴 ('인원을 줄이세요' / '간호사를 줄이세요') 카탈로그에 0건.
"""

from __future__ import annotations

import re
import pytest

from services.semantics.ontology import (
    ConstraintOntology,
    OntologyCause,
    OntologyTreatment,
    get_default_ontology,
)


# ─────────────────────────────────────────────────────────────────────────
# 카탈로그 size
# ─────────────────────────────────────────────────────────────────────────
def test_catalogue_has_enough_causes_and_treatments():
    onto = get_default_ontology()
    assert len(onto.causes) >= 15, f"cause 카탈로그 size {len(onto.causes)} — 최소 15 expected"
    assert len(onto.treatments) >= 12, f"treatment 카탈로그 size {len(onto.treatments)} — 최소 12 expected"


def test_undiagnosed_sentinel_in_cause_catalog():
    onto = get_default_ontology()
    c = onto.get_cause("UNDIAGNOSED")
    assert c is not None
    assert c.cause_id == "cause:undiagnosed"
    assert c.category == "undiagnosed"


# ─────────────────────────────────────────────────────────────────────────
# Invariant 1: 모든 treatment.applies_to_causes 가 valid cause
# ─────────────────────────────────────────────────────────────────────────
def test_every_treatment_applies_to_valid_causes():
    onto = get_default_ontology()
    invalid: list[tuple[str, str]] = []
    for tid, t in onto.treatments.items():
        for cid in t.applies_to_causes:
            if cid not in onto.causes:
                invalid.append((tid, cid))
    assert invalid == [], f"treatments 가 valid cause 를 가리키지 않음: {invalid[:5]}"


# ─────────────────────────────────────────────────────────────────────────
# Invariant 2: 모든 cause 가 적어도 1 treatment 에 의해 cover
# ─────────────────────────────────────────────────────────────────────────
def test_every_cause_has_at_least_one_treatment():
    onto = get_default_ontology()
    uncovered: list[str] = []
    for cid in onto.causes:
        treatments = onto.treatments_for_cause(cid)
        if not treatments:
            uncovered.append(cid)
    assert uncovered == [], f"treatment 없는 cause: {uncovered}"


# ─────────────────────────────────────────────────────────────────────────
# Invariant 3: 자연어 필드 비어있지 않음
# ─────────────────────────────────────────────────────────────────────────
def test_all_treatments_have_rationale_and_trade_off():
    onto = get_default_ontology()
    missing: list[str] = []
    for tid, t in onto.treatments.items():
        if not t.rationale_ko.strip():
            missing.append(f"{tid}.rationale_ko")
        if not t.trade_off_ko.strip():
            missing.append(f"{tid}.trade_off_ko")
    assert missing == [], f"비어있는 자연어 필드: {missing[:5]}"


def test_all_causes_have_problem_template():
    onto = get_default_ontology()
    missing = [cid for cid, c in onto.causes.items() if not c.problem_template_ko.strip()]
    assert missing == [], f"problem_template_ko 비어있는 cause: {missing[:5]}"


# ─────────────────────────────────────────────────────────────────────────
# Invariant 4: alias unique
# ─────────────────────────────────────────────────────────────────────────
def test_cause_aliases_are_unique_across_causes():
    onto = get_default_ontology()
    alias_to_cause: dict[str, str] = {}
    conflicts: list[tuple[str, str, str]] = []
    for cid, c in onto.causes.items():
        for alias in c.aliases:
            key = str(alias).upper()
            if key in alias_to_cause and alias_to_cause[key] != cid:
                conflicts.append((alias, alias_to_cause[key], cid))
            else:
                alias_to_cause[key] = cid
    assert conflicts == [], f"alias 충돌: {conflicts}"


# ─────────────────────────────────────────────────────────────────────────
# Invariant 5: legacy reason_code → cause 해석 가능
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("legacy_code,expected_category", [
    ("CAPACITY_TOTAL_SHORTAGE", "capacity"),
    ("N_CAPACITY_SHORTAGE", "capacity"),
    ("MONTHLY_NIGHT_CAPACITY_SHORTAGE", "capacity"),
    ("ALLOWED_SHIFTS_ISOLATES_NURSE", "eligibility"),
    ("GRADE_MAX_SUM_BELOW_NEED", "grade"),
    ("GRADE_MIN_EXCEEDS_MAX", "config"),
    ("TEAM_MIN_EXCEEDS_GLOBAL_NEED", "team"),
    ("FIXED_ASSIGN_EXCEEDS_NEED", "fixed"),
    ("FIXED_OFF_EXCEEDS_SPAN", "fixed"),
    ("MID_REQUIRED_MISSING", "config"),
    ("UNDIAGNOSED", "undiagnosed"),
    ("DAILY_DEMAND_EXCEEDS_NURSE_COUNT", "capacity"),
    ("OFF_BUDGET_EXCEEDS_NUM_DAYS", "fixed"),
    ("MONTHLY_LIMIT_MIN_EXCEEDS_MAX", "config"),
    ("TEAM_MIN_EXCEEDS_TEAM_SIZE", "team"),
])
def test_legacy_reason_code_resolves_to_cause_with_expected_category(legacy_code, expected_category):
    onto = get_default_ontology()
    c = onto.get_cause(legacy_code)
    assert c is not None, f"{legacy_code} 가 cause 로 해석되지 않음"
    assert c.category == expected_category, (
        f"{legacy_code} → {c.cause_id} (category={c.category}) — expected {expected_category}"
    )


# ─────────────────────────────────────────────────────────────────────────
# Invariant 6: naive 추천 패턴 금지 — '인원을 줄이세요' 류 0건
# ─────────────────────────────────────────────────────────────────────────
def test_no_naive_headcount_reduction_in_treatments():
    onto = get_default_ontology()
    # naive 패턴: 단순히 "줄이세요" 만 있고 어떤 설정 어떻게 조정인지 명시 없음
    # 단, "감소" / "줄여" 자체는 OK (구체 config_key 와 함께라면)
    naive_pattern = re.compile(r"(간호사|인원)(을|를)\s*(줄이|감축)")
    violations: list[tuple[str, str]] = []
    for tid, t in onto.treatments.items():
        if naive_pattern.search(t.rationale_ko):
            # config_key 가 None (= manual) 인 경우에만 OK 인지 추가 점검
            # treatment:data:add_nurse 처럼 manual + 명시적 trade-off 면 OK
            # 일단 모든 naive 패턴 점검 — 발견 시 review 필요
            violations.append((tid, t.rationale_ko[:80]))
    # 인력 본질 부족 케이스(treatment:data:add_nurse) 는 "인원 보강 또는 demand 하향" 양방향 명시
    # 그 외 naive '인원 감축' 추천 없어야
    naive_only = [
        (tid, msg) for tid, msg in violations
        if "보강" not in msg and "추가" not in msg and "demand" not in msg.lower() and "수요" not in msg
    ]
    assert naive_only == [], f"naive headcount reduction 추천 발견: {naive_only}"


# ─────────────────────────────────────────────────────────────────────────
# 동작 검증 — treatments_for_cause API
# ─────────────────────────────────────────────────────────────────────────
def test_treatments_for_cause_returns_expected_set_for_team_min_over_need():
    onto = get_default_ontology()
    treatments = onto.treatments_for_cause("cause:team:min_over_need")
    ids = {t.treatment_id for t in treatments}
    # 적어도 다음 3개는 매핑되어야
    assert "treatment:soft:team_min" in ids
    assert "treatment:threshold:team_min" in ids
    assert "treatment:scope:team_min_remove" in ids


def test_treatments_for_cause_via_legacy_alias():
    """legacy reason_code 로도 매핑 lookup 가능."""
    onto = get_default_ontology()
    treatments = onto.treatments_for_cause("GRADE_MAX_SUM_BELOW_NEED")
    ids = {t.treatment_id for t in treatments}
    assert "treatment:soft:grade_max" in ids


def test_undiagnosed_has_only_manual_treatment():
    onto = get_default_ontology()
    treatments = onto.treatments_for_cause("cause:undiagnosed")
    assert len(treatments) >= 1
    # UNDIAGNOSED 는 자동 treatment 가 아니라 manual 만 (data_correction_required)
    assert all(t.action_type == "data_correction_required" for t in treatments)


# ─────────────────────────────────────────────────────────────────────────
# Treatment 의 config_key 가 명시적 (단순 '인원 조절' 추상 추천 금지)
# ─────────────────────────────────────────────────────────────────────────
def test_non_manual_treatments_have_config_key():
    """data_correction_required 가 아닌 treatment 는 반드시 config_key 명시."""
    onto = get_default_ontology()
    missing: list[str] = []
    for tid, t in onto.treatments.items():
        if t.action_type == "data_correction_required":
            continue  # manual 은 config_key 없을 수 있음
        if not t.config_key:
            missing.append(tid)
    assert missing == [], f"action_type={t.action_type} 인데 config_key 없는 treatment: {missing}"


def test_non_manual_treatments_have_explicit_direction():
    """direction 이 명시적 — manual/enable/disable/increase/decrease/clear/remove_key 중 하나."""
    onto = get_default_ontology()
    valid_directions = {"enable", "disable", "increase", "decrease", "clear", "remove_key", "manual"}
    invalid: list[tuple[str, str]] = []
    for tid, t in onto.treatments.items():
        if t.direction not in valid_directions:
            invalid.append((tid, t.direction))
    assert invalid == [], f"invalid direction: {invalid}"
