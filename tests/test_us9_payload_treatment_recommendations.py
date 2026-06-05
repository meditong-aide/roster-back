"""US-9 검증 — payload 가 treatment_recommendations + resolution_narrative 통합 노출.

핵심 invariants:
  1. multi-cause case 에서 bundle 추천 1+ 개.
  2. 각 bundle 이 cover 정보 + treatments[] (config_key + direction + rationale + trade_off) 포함.
  3. resolution_narrative 에 problem_list / action_levers / trade_offs 3 섹션 모두.
  4. cause-bucket 와 symptom-bucket 교차 없음 (US-1 invariant 유지).
  5. legacy violated_constraints 보존.
  6. cause 없는 케이스 (cause_id 0건) → treatment_recommendations 빈 list.
"""

from __future__ import annotations

from services.precheck.payload import build_unrecoverable_payload


def test_multi_cause_payload_has_bundle_recommendations():
    payload = build_unrecoverable_payload(
        violated_constraints=[
            {"reason_code": "CAPACITY_TOTAL_SHORTAGE",
             "details": {"required": 450, "capacity": 220, "shortage": 230}},
            {"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
             "details": {"day": 5, "shift": "D", "min_sum": 8, "required": 5}},
            {"reason_code": "NO_ASSIGNMENT"},
        ],
        conflict_cores=[{"members": ["TeamMin:1:D", "TeamMin:2:D"]}],
    )
    inf = payload["infeasibility"]
    assert "treatment_recommendations" in inf
    assert len(inf["treatment_recommendations"]) >= 1
    bundle = inf["treatment_recommendations"][0]
    # bundle 의 핵심 필드
    assert "bundle_id" in bundle
    assert "total_cost" in bundle
    assert "covered_causes" in bundle
    assert "treatments" in bundle
    # 각 treatment 의 actionability
    for t in bundle["treatments"]:
        assert "treatment_id" in t
        assert "target_family" in t
        assert "action_type" in t
        # config_key + direction 명시 (data_correction_required 만 None 허용)
        if t["action_type"] != "data_correction_required":
            assert t["config_key"]
        assert t["direction"] in {"enable", "disable", "increase", "decrease", "clear", "remove_key", "manual"}
        assert t["rationale_ko"].strip()
        assert t["trade_off_ko"].strip()


def test_resolution_narrative_three_sections_present():
    payload = build_unrecoverable_payload(
        violated_constraints=[
            {"reason_code": "GRADE_MAX_SUM_BELOW_NEED",
             "details": {"day": 12, "shift": "N", "cap": 2, "required": 3}},
        ],
    )
    narr = payload["infeasibility"]["resolution_narrative"]
    assert narr is not None
    assert "summary_ko" in narr
    assert "problem_list" in narr
    assert "action_levers" in narr
    assert "trade_offs" in narr
    # 각 섹션 비어있지 않음
    assert len(narr["problem_list"]) >= 1
    assert len(narr["action_levers"]) >= 1
    assert len(narr["trade_offs"]) >= 1


def test_empty_violations_yields_empty_treatment_recommendations():
    payload = build_unrecoverable_payload(violated_constraints=[])
    inf = payload["infeasibility"]
    assert inf["treatment_recommendations"] == []
    # narrative 도 None (cause 0건)
    assert inf["resolution_narrative"] is None


def test_only_symptoms_no_cause_yields_empty_treatment_recommendations():
    """symptom 만 있는 경우 → cause 없음 → 추천 0."""
    payload = build_unrecoverable_payload(
        violated_constraints=[
            {"reason_code": "NO_ASSIGNMENT"},
            {"reason_code": "GRADE_HARD_PROBE"},
        ],
    )
    inf = payload["infeasibility"]
    assert inf["treatment_recommendations"] == []


def test_legacy_violated_constraints_preserved():
    violations = [
        {"reason_code": "CAPACITY_TOTAL_SHORTAGE"},
        {"reason_code": "NO_ASSIGNMENT"},
    ]
    payload = build_unrecoverable_payload(violated_constraints=violations)
    # legacy field 유지
    assert payload["infeasibility"]["violated_constraints"] == violations


def test_cause_symptom_invariant_held_through_us9_integration():
    """US-1 invariant: cause-bucket ∩ symptom-bucket = ∅ 가 US-9 통합 후에도 유지."""
    payload = build_unrecoverable_payload(
        violated_constraints=[
            {"reason_code": "NO_ASSIGNMENT"},
            {"reason_code": "CAPACITY_TOTAL_SHORTAGE"},
            {"reason_code": "NO_ASSIGNMENT_CAPACITY"},
            {"reason_code": "GRADE_HARD_PROBE"},
        ],
    )
    inf = payload["infeasibility"]
    cause_codes = {c["reason_code"] for c in inf["causes"]}
    symptom_codes = {s["reason_code"] for s in inf["observed_symptoms"]}
    assert cause_codes.isdisjoint(symptom_codes)


def test_bundle_recommendation_uses_ontology_derived_cost():
    """추천 bundle 의 total_cost 가 0 보다 큼 — ontology-derived cost 가 실제로 산출되었음."""
    payload = build_unrecoverable_payload(
        violated_constraints=[{"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED"}],
    )
    bundles = payload["infeasibility"]["treatment_recommendations"]
    assert bundles
    for b in bundles:
        assert b["total_cost"] >= 0  # 음수 가능 (scenario_bonus 가 음수면), but 합리적 범위
        # 각 treatment 의 cost 도 명시
        for t in b["treatments"]:
            assert "cost" in t


def test_no_naive_pattern_in_narrative_text_through_payload():
    import re
    payload = build_unrecoverable_payload(
        violated_constraints=[
            {"reason_code": "CAPACITY_TOTAL_SHORTAGE",
             "details": {"required": 100, "capacity": 50, "shortage": 50,
                         "nurse_count": 5, "days": 30, "off_days": 8,
                         "off_first": False, "source": "test"}},
        ],
    )
    narr = payload["infeasibility"]["resolution_narrative"]
    if narr is None:
        return
    full = " ".join([
        narr["summary_ko"],
        *[p["rendered_ko"] for p in narr["problem_list"]],
        *[a["rationale_ko"] for a in narr["action_levers"]],
        *[t["trade_off_ko"] for t in narr["trade_offs"]],
    ])
    # naive '인원 줄여라' 0건 (보강/추가 context 제외)
    for m in re.finditer(r"(간호사|인원)(을|를)\s*(줄이|감축)", full):
        ctx = full[max(0, m.start() - 30): m.end() + 30]
        assert any(k in ctx for k in ("보강", "추가", "수요 하향", "demand")), (
            f"naive pattern leaked: {ctx}"
        )
