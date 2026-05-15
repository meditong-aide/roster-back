"""Tests for ontology tier (T0/T1/T2/T3) + axis_registry + fix_plan tier-axis layer."""

from __future__ import annotations

import pytest

from services.precheck.fix_plan import build_fix_plan
from services.semantics.axis_registry import get_default_axis_registry
from services.semantics.ontology import get_default_ontology


# ---------- tier coverage ----------


def test_every_constraint_family_has_tier():
    onto = get_default_ontology()
    missing = [cid for cid, e in onto.constraints.items() if not e.tier]
    assert missing == [], f"families missing tier: {missing}"


def test_tier_values_are_valid():
    onto = get_default_ontology()
    valid = {"T0", "T1", "T2", "T3"}
    invalid = [(cid, e.tier) for cid, e in onto.constraints.items() if e.tier not in valid]
    assert invalid == [], f"families with invalid tier: {invalid}"


def test_tier_priority_consistency():
    """priority 5 → T1 or T0 (never T2/T3); priority 1~4 → T2 (never T1)."""
    onto = get_default_ontology()
    for cid, e in onto.constraints.items():
        if e.relaxation_priority == 5:
            assert e.tier in ("T0", "T1"), f"{cid}: priority 5 but tier={e.tier}"
        if e.relaxation_priority is not None and 1 <= e.relaxation_priority <= 4:
            assert e.tier == "T2", f"{cid}: priority {e.relaxation_priority} but tier={e.tier}"


def test_tier_partition_expected_anchor_families():
    onto = get_default_ontology()
    assert onto.get_tier("NightRecovery") == "T1"
    assert onto.get_tier("OffCap") == "T1"
    assert onto.get_tier("AssignmentWindow") == "T1"
    assert onto.get_tier("FixedWanted") == "T1"
    assert onto.get_tier("TeamMin") == "T2"
    assert onto.get_tier("GradeMin") == "T2"
    assert onto.get_tier("CoverageMin") == "T2"
    assert onto.get_tier("ConfigIntegrity") == "T0"


def test_families_by_tier_lookup():
    onto = get_default_ontology()
    assert "ConfigIntegrity" in onto.families_by_tier("T0")
    assert "NightRecovery" in onto.families_by_tier("T1")
    assert "TeamMin" in onto.families_by_tier("T2")


# ---------- axis_registry ----------


def test_axis_registry_has_expected_axes():
    r = get_default_axis_registry()
    ids = {a.axis_id for a in r.all_axes()}
    for expected in (
        "night_capacity",
        "day_capacity",
        "evening_capacity",
        "mid_capacity",
        "team_min",
        "grade_min",
        "grade_max",
        "team_grade_handoff",
        "monthly_n_cap",
        "off_cap",
        "allowed_shift_mask",
        "fixed_excess",
        "fixed_violates_allowed",
        "fixed_breaks_team_min",
        "carryover_transition",
    ):
        assert expected in ids, f"axis missing: {expected}"


def test_axis_inherits_tier_from_ontology():
    r = get_default_axis_registry()
    assert r.tier_of(r.get("team_min")) == "T2"
    assert r.tier_of(r.get("grade_min")) == "T2"
    assert r.tier_of(r.get("night_capacity")) == "T2"
    # AllowedShiftMask family is T1 → eligibility axes are T1 (protected)
    assert r.tier_of(r.get("allowed_shift_mask")) == "T1"
    # FixedWanted family is T1 → fixed_* axes are T1
    assert r.tier_of(r.get("fixed_excess")) == "T1"


def test_axes_for_reasons_matches_codes():
    r = get_default_axis_registry()
    matched = r.axes_for_reasons(reason_codes={"GRADE_MIN_SUM_EXCEEDS_NEED"})
    ids = {a.axis_id for a in matched}
    assert "grade_min" in ids


def test_axes_for_reasons_matches_patterns():
    r = get_default_axis_registry()
    matched = r.axes_for_reasons(patterns={"cpsat_mus:carryover_boundary"})
    ids = {a.axis_id for a in matched}
    assert "carryover_transition" in ids


def test_axes_sort_for_user_t2_before_t1():
    r = get_default_axis_registry()
    mixed = [r.get(x) for x in ["allowed_shift_mask", "team_min", "grade_min"]]
    sorted_axes = r.sort_for_user(mixed)
    # T2 (team_min, grade_min) should come before T1 (allowed_shift_mask)
    tier_seq = [r.tier_of(a) for a in sorted_axes]
    assert tier_seq.index("T2") < tier_seq.index("T1")


# ---------- fix_plan tier+axis integration ----------


def _plan(*, preflight_issues=None, violated=None, cores=None, shortages=None, evidence=None):
    violated = list(violated or [{"reason_code": "NO_ASSIGNMENT"}])
    if evidence:
        # attach evidence to first violation
        if violated and isinstance(violated[0], dict):
            violated[0].setdefault("details", {})["validator_evidence"] = evidence
    return build_fix_plan(
        structural_diagnosis={"mode": "relaxation_candidate"},
        preflight_issues=list(preflight_issues or []),
        violated_constraints=violated,
        conflict_cores=list(cores or []),
        pool_snapshot={"shortages": list(shortages or [])},
    )


def test_fix_plan_tier_summary_present():
    p = _plan(shortages=[{"pool_id": "team_pool:1:N", "shortage": 3}])
    assert "tier_summary" in p
    assert set(p["tier_summary"].keys()) == {"T0", "T1", "T2", "T3"}


def test_fix_plan_axis_actions_cap_five():
    cells = [{"day": d, "shift": s, "shortage": 1} for d in range(1, 8) for s in ("D", "E", "N")]
    p = _plan(
        violated=[{"reason_code": "NO_ASSIGNMENT_CAPACITY"}],
        evidence={"top_failed_cells": cells},
    )
    assert len(p["axis_actions"]) <= 5
    assert p["axis_actions_cap"] == 5


def test_fix_plan_protected_axes_t1_only():
    p = _plan(
        preflight_issues=[{"reason_code": "ALLOWED_SHIFTS_ISOLATES_NURSE"}],
    )
    # protected_axes should only contain T1 entries
    for entry in p["protected_axes"]:
        assert entry["tier"] == "T1"


def test_fix_plan_data_correction_when_t0_present():
    # ConfigIntegrity is T0 and its aliases include MID_REQUIRED_MISSING
    p = _plan(
        violated=[
            {"reason_code": "NO_ASSIGNMENT"},
            {"reason_code": "MID_REQUIRED_MISSING"},
        ],
    )
    assert p["data_correction_required"] is True
    assert "data_correction_message_ko" in p


def test_fix_plan_no_data_correction_when_only_t2():
    p = _plan(
        shortages=[{"pool_id": "team_pool:1:N", "shortage": 3}],
    )
    assert p["data_correction_required"] is False


def test_fix_plan_axis_action_human_message_includes_label():
    p = _plan(
        violated=[{"reason_code": "NO_ASSIGNMENT_CAPACITY"}],
        evidence={
            "top_failed_cells": [
                {"day": 20, "shift": "N", "shortage": 2},
                {"day": 22, "shift": "N", "shortage": 1},
            ]
        },
    )
    axis_actions = p["axis_actions"]
    night = next((a for a in axis_actions if a["axis_id"] == "night_capacity"), None)
    assert night is not None
    assert "N" in night["human_message_ko"]
    assert "20" in night["human_message_ko"] or "22" in night["human_message_ko"]


def test_fix_plan_axis_action_has_required_fields():
    p = _plan(
        shortages=[{"pool_id": "team_pool:1:N", "shortage": 3}],
    )
    for a in p["axis_actions"]:
        assert "axis_id" in a
        assert "family" in a
        assert "tier" in a
        assert "lock_type" in a
        assert "label_ko" in a
        assert "human_message_ko" in a
        assert "config_lever" in a
        assert "targets" in a


def test_fix_plan_legacy_actions_preserved():
    """Existing actions[] (adjust_coverage_or_supply etc.) must stay for backward-compat."""
    p = _plan(
        shortages=[{"pool_id": "team_pool:1:D", "shortage": 2}],
    )
    legacy_ids = [a["action_id"] for a in p["actions"]]
    assert "adjust_coverage_or_supply" in legacy_ids


def test_fix_plan_multi_axis_composite_recommendation():
    """사용자 예: 'N 필요인원 줄이고 Grade 최소 줄여보세요'."""
    p = _plan(
        violated=[
            {"reason_code": "NO_ASSIGNMENT_CAPACITY"},
            {"reason_code": "GRADE_MIN_SUM_EXCEEDS_NEED"},
        ],
        evidence={
            "top_failed_cells": [
                {"day": 20, "shift": "N", "shortage": 2},
            ]
        },
    )
    axis_ids = {a["axis_id"] for a in p["axis_actions"]}
    # both night_capacity and grade_min should appear as separate axis actions
    assert "night_capacity" in axis_ids
    assert "grade_min" in axis_ids


def test_fix_plan_failure_stage_s0_precheck():
    """ConfigIntegrity 신호 → S0 precheck."""
    p = _plan(
        violated=[
            {"reason_code": "NO_ASSIGNMENT"},
            {"reason_code": "MID_REQUIRED_MISSING"},
        ],
    )
    assert p["failure_stage"] == "S0_precheck"
    assert "S0" in p["failure_stage_label_ko"]


def test_fix_plan_failure_stage_s1_night():
    """N capacity 코드 → S1 night skeleton."""
    p = _plan(
        violated=[
            {"reason_code": "NO_ASSIGNMENT"},
            {"reason_code": "N_CAPACITY_SHORTAGE"},
        ],
    )
    assert p["failure_stage"] == "S1_night_skeleton"


def test_fix_plan_failure_stage_s2_carryover():
    """carryover pattern → S2 recovery/offwindow."""
    p = _plan(
        violated=[{"reason_code": "NO_ASSIGNMENT"}],
        cores=[{"pattern": "cpsat_mus:carryover_boundary"}],
    )
    assert p["failure_stage"] == "S2_recovery_offwindow"


def test_fix_plan_failure_stage_s3_dayeve():
    """grade/team min shortage → S3 day/eve coverage."""
    p = _plan(
        violated=[
            {"reason_code": "NO_ASSIGNMENT"},
            {"reason_code": "GRADE_MIN_SUM_EXCEEDS_NEED"},
        ],
    )
    assert p["failure_stage"] == "S3_day_eve_coverage"


def test_fix_plan_stage_scope_filters_irrelevant_axes():
    """N-only signal 인 S1 stage 에서, recommend 측면 무관 axis 를 자동 제외.

    아래 시나리오는 N 실패만 있는데 reason_code 가 GRADE_MAX 도 함께
    들어와 grade_max axis 가 매칭됐을 때, stage 가 S1 로 좁혀져
    grade_max(주로 D/E 영역) 를 자동으로 actions 에서 빼는 케이스.
    """
    p = _plan(
        violated=[
            {"reason_code": "NO_ASSIGNMENT"},
            {"reason_code": "N_CAPACITY_SHORTAGE"},
            {"reason_code": "GRADE_MAX_SUM_BELOW_NEED"},  # would normally match grade_max axis
        ],
        evidence={
            "top_failed_cells": [
                {"day": 20, "shift": "N", "shortage": 2, "required": 3, "assigned": 1, "eligible": 1,
                 "blocking_axes": ["night_capacity"], "blocking_detail": {"primary_axis": "night_capacity"}},
            ],
        },
    )
    # N_CAPACITY_SHORTAGE → S1, N cell → S1. Both same → S1.
    # GRADE_MAX_SUM_BELOW_NEED matches T2 grade_max but is out of S1 scope.
    assert p["failure_stage"] == "S1_night_skeleton"
    axis_ids = {a["axis_id"] for a in p["axis_actions"]}
    assert "night_capacity" in axis_ids
    assert "grade_max" not in axis_ids  # filtered by S1 scope


def test_fix_plan_uses_blocking_axes_from_cell_evidence():
    """S3: validator_evidence cells with explicit `blocking_axes` should be respected."""
    p = _plan(
        violated=[{"reason_code": "NO_ASSIGNMENT_CAPACITY"}],
        evidence={
            "top_failed_cells": [
                {
                    "day": 20, "shift": "N", "shortage": 2,
                    "blocking_axes": ["night_capacity", "allowed_shift_mask"],
                    "blocking_detail": {"primary_axis": "night_capacity", "eligibility_gap": 1, "assignment_gap": 2, "shift": "N"},
                },
            ]
        },
    )
    axis_ids = {a["axis_id"] for a in p["axis_actions"]}
    assert "night_capacity" in axis_ids
    # allowed_shift_mask is T1 → must go to protected_axes, not actions
    protected_ids = {x["axis_id"] for x in p["protected_axes"]}
    assert "allowed_shift_mask" in protected_ids


def test_fix_plan_axis_sorted_by_priority_asc():
    """T2 axes sorted by relaxation_priority ascending (easier-to-relax first)."""
    p = _plan(
        violated=[
            {"reason_code": "NO_ASSIGNMENT_CAPACITY"},
            {"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED"},  # team_min prio=2
            {"reason_code": "GRADE_MIN_SUM_EXCEEDS_NEED"},   # grade_min prio=3
            {"reason_code": "TEAM_GRADE_INTERSECT_SHORTAGE"}, # team_grade_handoff prio=1
        ],
    )
    priorities = [a["relaxation_priority"] for a in p["axis_actions"]]
    # filter out Nones
    priorities = [p for p in priorities if p is not None]
    assert priorities == sorted(priorities), f"axis_actions not sorted: {priorities}"
