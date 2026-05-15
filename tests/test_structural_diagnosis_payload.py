from services.precheck.payload import build_unrecoverable_payload
from services.precheck.structural_diagnosis import build_structural_diagnosis


def test_structural_diagnosis_hard_block_from_no_assignment_and_shortage():
    d = build_structural_diagnosis(
        preflight_issues=[],
        violated_constraints=[{"reason_code": "NO_ASSIGNMENT"}],
        conflict_cores=[{"pattern": "cpsat_mus:allowed_shift_mask"}],
        pool_snapshot={"shortages": [{"pool": "team_pool:1:D"}]},
        applied_relaxations=["soft_fallback"],
    )
    assert d["mode"] == "hard_block_structural"
    assert "capacity_structural" in d["primary_causes"]
    assert d["signals"]["shortage_count"] == 1
    assert any("RULE_2_MATCH" in x for x in d["decision_trace"])


def test_structural_diagnosis_relaxation_candidate_when_no_signals():
    d = build_structural_diagnosis(
        preflight_issues=[],
        violated_constraints=[],
        conflict_cores=[],
        pool_snapshot={},
        applied_relaxations=[],
    )
    assert d["mode"] == "relaxation_candidate"
    assert d["primary_causes"] == []
    assert any("RULE_4_MATCH" in x for x in d["decision_trace"])


def test_structural_diagnosis_hard_impossible_code_boosts_mode():
    d = build_structural_diagnosis(
        preflight_issues=[{"reason_code": "GRADE_MIN_SUM_EXCEEDS_NEED"}],
        violated_constraints=[],
        conflict_cores=[],
        pool_snapshot={},
        applied_relaxations=[],
    )
    assert d["mode"] == "hard_block_structural"
    assert "capacity_structural" in d["primary_causes"]
    assert "GRADE_MIN_SUM_EXCEEDS_NEED" in d["signals"]["reason_codes"]
    assert any("RULE_1_MATCH" in x for x in d["decision_trace"])


def test_structural_diagnosis_mixed_for_role_or_fixed_without_shortage():
    d = build_structural_diagnosis(
        preflight_issues=[{"reason_code": "ALLOWED_SHIFTS_ISOLATES_NURSE"}],
        violated_constraints=[],
        conflict_cores=[],
        pool_snapshot={},
        applied_relaxations=[],
    )
    assert d["mode"] == "mixed_relaxation_needed"
    assert "role_isolation" in d["primary_causes"]
    assert any("RULE_3_MATCH" in x for x in d["decision_trace"])


def test_unrecoverable_payload_contains_structural_diagnosis():
    payload = build_unrecoverable_payload(
        precheck_result={"issues": [{"reason_code": "ALLOWED_SHIFTS_ISOLATES_NURSE"}]},
        applied_relaxations=["grade_max_soft_fallback"],
        last_error_reason="infeasible",
        violated_constraints=[{"reason_code": "NO_ASSIGNMENT"}],
        conflict_cores=[{"pattern": "cpsat_mus:initial_forbidden"}],
        pool_snapshot={"shortages": [{"pool": "grade_pool:0:D"}]},
    )
    inf = payload["infeasibility"]
    assert "structural_diagnosis" in inf
    assert "fix_plan" in inf
    assert isinstance(inf["fix_plan"].get("actions"), list)
    assert inf["structural_diagnosis"]["mode"] in {
        "hard_block_structural",
        "mixed_relaxation_needed",
    }


def test_unrecoverable_payload_fix_plan_prioritizes_shortage_action():
    payload = build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason="infeasible",
        violated_constraints=[{"reason_code": "NO_ASSIGNMENT"}],
        conflict_cores=[],
        pool_snapshot={
            "shortages": [
                {"pool_id": "team_pool:team_1:D", "shortage": 6},
                {"pool_id": "team_pool:team_2:N", "shortage": 2},
            ]
        },
    )
    fp = payload["infeasibility"]["fix_plan"]
    actions = fp["actions"]
    assert fp["plan_mode"] == "hypothesis_checks"
    assert "capacity_shortage" in fp["no_assignment_breakdown"]
    assert actions
    assert actions[0]["action_id"] == "adjust_coverage_or_supply"
    assert any(t["pool_id"] == "team_pool:team_1:D" for t in actions[0]["targets"])


def test_unrecoverable_payload_fix_plan_no_assignment_breakdown_role_and_carryover():
    payload = build_unrecoverable_payload(
        precheck_result={"issues": [{"reason_code": "ALLOWED_SHIFTS_ISOLATES_NURSE"}]},
        applied_relaxations=[],
        last_error_reason="infeasible",
        violated_constraints=[{"reason_code": "NO_ASSIGNMENT"}, {"reason_code": "PREV_MONTH_TRANSITION"}],
        conflict_cores=[{"pattern": "cpsat_mus:carryover_boundary"}],
        pool_snapshot={"shortages": []},
    )
    fp = payload["infeasibility"]["fix_plan"]
    assert "eligibility_lock" in fp["no_assignment_breakdown"]
    assert "carryover_lock" in fp["no_assignment_breakdown"]


def test_unrecoverable_payload_includes_validator_evidence_summary():
    payload = build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason="infeasible",
        violated_constraints=[
            {
                "reason_code": "NO_ASSIGNMENT_ELIGIBILITY",
                "details": {
                    "source": "no_assignment_direct_rule",
                    "validator_evidence": {
                        "total_failed_cells": 7,
                        "eligible_zero_cells": 5,
                        "required_minus_assigned_total": 12,
                        "top_failed_cells": [
                            {"day": 1, "shift": "D", "required": 3, "assigned": 0, "eligible": 0, "shortage": 3, "eligible_gap": 3}
                        ],
                    },
                },
            }
        ],
        conflict_cores=[],
        pool_snapshot={},
    )
    ev = payload["infeasibility"]["validator_evidence_summary"]
    assert ev["total_failed_cells"] == 7
    assert ev["eligible_zero_cells"] == 5
    assert ev["required_minus_assigned_total"] == 12
    assert ev["fixed_forbidden_count"] == 0
    assert ev["carryover_artifact_count"] == 0
    assert ev["top_failed_cells"][0]["shift"] == "D"


def test_unrecoverable_payload_capacity_direct_generates_concrete_daily_targets():
    payload = build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason="infeasible",
        violated_constraints=[
            {
                "reason_code": "NO_ASSIGNMENT_CAPACITY",
                "details": {
                    "validator_evidence": {
                        "total_failed_cells": 9,
                        "top_failed_cells": [
                            {"day": 4, "shift": "D", "shortage": 2},
                            {"day": 5, "shift": "E", "shortage": 1},
                        ],
                    }
                },
            }
        ],
        conflict_cores=[],
        pool_snapshot={},
    )
    fp = payload["infeasibility"]["fix_plan"]
    actions = fp.get("actions") or []
    assert actions
    assert actions[0]["action_id"] == "adjust_daily_requirement_stepwise"
    targets = actions[0].get("targets") or []
    assert targets and targets[0]["target_type"] == "daily_requirement"
    assert targets[0]["day"] == 4 and targets[0]["shift"] == "D"
