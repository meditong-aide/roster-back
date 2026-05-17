"""US-1 검증 — build_unrecoverable_payload 가 cause / observed_symptom / evidence 3 필드를 분리해 노출.

핵심 invariant:
  1. cause-bucket 과 symptom-bucket 의 reason_code 집합은 교집합 0건.
  2. EvidenceNode 안에 cause/reason_code/symptom 키가 절대 포함되지 않음.
  3. UNDIAGNOSED sentinel 은 cause-bucket 에 들어가되 confidence=low.
  4. core_size 는 conflict_cores members 의 합계.
  5. status=INFEASIBLE 이면 verified=False.
"""

from __future__ import annotations

import pytest

from services.precheck.payload import build_unrecoverable_payload
from services.precheck.cause_symptom_classifier import (
    classify,
    split_violations,
    SYMPTOM_CODES,
    PROBE_CODES,
    PARTIAL_CAUSE_CODES,
    UNDIAGNOSED_SENTINEL,
)
from services.precheck.evidence_builder import build_evidence_node


# ─────────────────────────────────────────────────────────────────────────
# Classifier — 단위
# ─────────────────────────────────────────────────────────────────────────
def test_classify_returns_expected_category_for_each_code_type():
    for code in SYMPTOM_CODES:
        assert classify(code) == "symptom", code
    for code in PROBE_CODES:
        assert classify(code) == "probe", code
    for code in PARTIAL_CAUSE_CODES:
        assert classify(code) == "partial_cause", code
    assert classify(UNDIAGNOSED_SENTINEL) == "undiagnosed"
    # cause family 예: 임의의 cause 코드 → cause
    assert classify("CAPACITY_TOTAL_SHORTAGE") == "cause"
    assert classify("GRADE_MAX_SUM_BELOW_NEED") == "cause"
    # 빈 코드 → cause 로 보수적 분류
    assert classify("") == "cause"
    assert classify(None) == "cause"


def test_split_violations_disjoint_buckets():
    """정책 (U-1, 2026-05-17 ralph): NO_ASSIGNMENT* 4 축 라벨도 symptom-bucket 으로 라우팅.

    cause-bucket: real cause + UNDIAGNOSED sentinel.
    symptom-bucket: NO_ASSIGNMENT / NO_ASSIGNMENT_X4 / DAY_ZERO_COVERAGE / NURSE_BLOCKED_DAYS / probes.
    """
    violations = [
        {"reason_code": "NO_ASSIGNMENT"},
        {"reason_code": "DAY_ZERO_COVERAGE"},
        {"reason_code": "NURSE_BLOCKED_DAYS"},
        {"reason_code": "GRADE_HARD_PROBE"},
        {"reason_code": "MAX_CAP_SHORTAGE"},
        {"reason_code": "CAPACITY_TOTAL_SHORTAGE"},
        {"reason_code": "GRADE_MAX_SUM_BELOW_NEED"},
        {"reason_code": "NO_ASSIGNMENT_CAPACITY"},
        {"reason_code": "NO_ASSIGNMENT_FIXED"},
        {"reason_code": "UNDIAGNOSED"},
    ]
    causes, symptoms, has_undiag = split_violations(violations)

    cause_codes = {c["reason_code"] for c in causes}
    sym_codes = {s["reason_code"] for s in symptoms}

    # 교집합 0
    assert cause_codes.isdisjoint(sym_codes), (cause_codes, sym_codes)
    # cause-bucket: real cause + UNDIAGNOSED only — NO_ASSIGNMENT* 절대 진입 금지
    assert {"CAPACITY_TOTAL_SHORTAGE", "GRADE_MAX_SUM_BELOW_NEED", "UNDIAGNOSED"} <= cause_codes
    for forbidden in ("NO_ASSIGNMENT", "NO_ASSIGNMENT_CAPACITY", "NO_ASSIGNMENT_FIXED",
                      "NO_ASSIGNMENT_ELIGIBILITY", "NO_ASSIGNMENT_CARRYOVER",
                      "DAY_ZERO_COVERAGE"):
        assert forbidden not in cause_codes, f"{forbidden} leaked into causes"
    # symptom-bucket: 모든 NO_ASSIGNMENT* + DAY_ZERO_COVERAGE + NURSE_BLOCKED_DAYS + probes
    assert {"NO_ASSIGNMENT", "DAY_ZERO_COVERAGE", "NURSE_BLOCKED_DAYS",
            "GRADE_HARD_PROBE", "MAX_CAP_SHORTAGE",
            "NO_ASSIGNMENT_CAPACITY", "NO_ASSIGNMENT_FIXED"} <= sym_codes
    assert has_undiag is True


def test_split_violations_undiagnosed_gets_low_confidence():
    causes, _, has_undiag = split_violations([{"reason_code": "UNDIAGNOSED"}])
    assert has_undiag is True
    assert len(causes) == 1
    assert causes[0]["confidence"] == "low"


# ─────────────────────────────────────────────────────────────────────────
# EvidenceBuilder — 단위
# ─────────────────────────────────────────────────────────────────────────
def test_evidence_node_does_not_leak_cause_keys():
    ev = build_evidence_node(
        applied_relaxations=["grade_hard_to_soft"],
        conflict_cores=[{"members": ["GradeMax:N:1", "GradeMax:N:2"]}],
        status="INFEASIBLE",
    )
    for k in ("reason_code", "cause", "causes", "cause_id", "symptom", "human_message_ko"):
        assert k not in ev


def test_evidence_node_required_fields():
    ev = build_evidence_node(
        applied_relaxations=["grade_hard_to_soft"],
        conflict_cores=[{"members": ["GradeMax:N:1", "GradeMax:N:2", "TeamMin:1:D"]}],
        status="INFEASIBLE",
    )
    assert ev["status"] == "INFEASIBLE"
    assert ev["applied_treatments"] == ["grade_hard_to_soft"]
    assert ev["proof_type"] == "cp_sat_unsat_core_heuristic"
    assert ev["is_minimal"] is False  # heuristic core 는 minimal 보장 없음
    assert ev["core_size"] == 3
    assert ev["witness_schedule_id"] is None
    assert ev["verified"] is False


def test_evidence_node_arithmetic_certificate_is_minimal():
    ev = build_evidence_node(
        applied_relaxations=[],
        conflict_cores=[],
        status="INFEASIBLE",
        proof_type="arithmetic_certificate",
    )
    assert ev["is_minimal"] is True


def test_evidence_node_re_solve_witness_verified_true_when_witness():
    ev = build_evidence_node(
        applied_relaxations=["grade_hard_to_soft"],
        conflict_cores=[],
        status="FEASIBLE",
        proof_type="re_solve_witness",
        witness_schedule_id="sched_42",
    )
    assert ev["verified"] is True


def test_evidence_node_verified_false_if_violation_delta_nonzero_after():
    ev = build_evidence_node(
        applied_relaxations=["grade_hard_to_soft"],
        status="FEASIBLE",
        proof_type="re_solve_witness",
        witness_schedule_id="sched_43",
        violation_delta={"cause:grade:max_sum_below_need": {"before": 1, "after": 1}},
    )
    # cause 가 풀리지 않았으므로 verified=False
    assert ev["verified"] is False


# ─────────────────────────────────────────────────────────────────────────
# Payload — build_unrecoverable_payload 통합
# ─────────────────────────────────────────────────────────────────────────
def test_unrecoverable_payload_separates_cause_symptom_evidence():
    payload = build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=["grade_hard_to_soft"],
        last_error_reason="infeasible",
        violated_constraints=[
            {"reason_code": "NO_ASSIGNMENT"},
            {"reason_code": "CAPACITY_TOTAL_SHORTAGE"},
            {"reason_code": "GRADE_HARD_PROBE", "details": "day=12"},
            {"reason_code": "NO_ASSIGNMENT_CAPACITY"},
        ],
        conflict_cores=[{"members": ["TeamMin:1:D", "TeamMin:2:D"]}],
    )
    infeasibility = payload["infeasibility"]
    assert "causes" in infeasibility
    assert "observed_symptoms" in infeasibility
    assert "evidence" in infeasibility

    cause_codes = {c["reason_code"] for c in infeasibility["causes"]}
    symptom_codes = {s["reason_code"] for s in infeasibility["observed_symptoms"]}

    assert "CAPACITY_TOTAL_SHORTAGE" in cause_codes
    # 새 정책 (U-1): NO_ASSIGNMENT* 는 symptom-bucket 으로 라우팅
    assert "NO_ASSIGNMENT_CAPACITY" not in cause_codes
    assert "NO_ASSIGNMENT_CAPACITY" in symptom_codes
    assert "NO_ASSIGNMENT" in symptom_codes
    assert "GRADE_HARD_PROBE" in symptom_codes

    # 핵심 invariant: cause-bucket ∩ symptom-bucket = ∅
    assert cause_codes.isdisjoint(symptom_codes)

    ev = infeasibility["evidence"]
    assert ev["status"] == "INFEASIBLE"
    assert ev["proof_type"] == "cp_sat_unsat_core_heuristic"
    assert ev["is_minimal"] is False
    assert ev["core_size"] == 2
    assert ev["applied_treatments"] == ["grade_hard_to_soft"]
    assert ev["verified"] is False
    # evidence 안에 cause/symptom 키 누출 방지
    for k in ("reason_code", "cause", "causes", "cause_id", "symptom"):
        assert k not in ev


def test_unrecoverable_payload_with_undiagnosed_sentinel():
    payload = build_unrecoverable_payload(
        violated_constraints=[
            {"reason_code": "UNDIAGNOSED",
             "human_message_ko": "day-zero trigger but no root cause identified"},
            {"reason_code": "NO_ASSIGNMENT"},
        ],
    )
    causes = payload["infeasibility"]["causes"]
    symptoms = payload["infeasibility"]["observed_symptoms"]
    # UNDIAGNOSED 는 cause-bucket
    assert any(c["reason_code"] == "UNDIAGNOSED" for c in causes)
    # NO_ASSIGNMENT 는 symptom-bucket
    assert any(s["reason_code"] == "NO_ASSIGNMENT" for s in symptoms)
    # UNDIAGNOSED 는 confidence=low 라벨
    undiag = next(c for c in causes if c["reason_code"] == "UNDIAGNOSED")
    assert undiag["confidence"] == "low"


def test_unrecoverable_payload_preserves_legacy_violated_constraints():
    """기존 violated_constraints 필드는 deprecated 라벨이지만 보존 — 1릴리즈."""
    violations = [{"reason_code": "NO_ASSIGNMENT"}, {"reason_code": "CAPACITY_TOTAL_SHORTAGE"}]
    payload = build_unrecoverable_payload(violated_constraints=violations)
    assert payload["infeasibility"]["violated_constraints"] == violations


def test_unrecoverable_payload_empty_violations_yields_empty_buckets():
    payload = build_unrecoverable_payload(violated_constraints=[])
    inf = payload["infeasibility"]
    assert inf["causes"] == []
    assert inf["observed_symptoms"] == []
    assert inf["evidence"]["status"] == "INFEASIBLE"
    assert inf["evidence"]["core_size"] == 0
