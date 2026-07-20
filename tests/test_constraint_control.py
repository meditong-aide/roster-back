"""Unit tests for constraint_impact.control adjustment apply layer.

Focus: apply_adjustments_to_config 가 ontology 매핑표대로 config_dict 를
정확히 변형하고, 잘못된 family/action 입력 시 ValueError 를 일으키는지.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for p in (str(ROOT), str(APP)):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.constraint_impact.control import (  # noqa: E402
    ConstraintAdjustment,
    apply_adjustments_to_config,
    supported_actions_for,
)


def _baseline_config() -> dict:
    return {
        "team_min_by_team": {"T1": {"D": 2, "N": 1}, "T2": {"D": 1}},
        "team_min_soft_fallback": False,
        "team_handoff_soft_fallback": False,
        "grade_config": {
            "constraints": {"D": {"1": 2}, "N": {"1": 1}},
            "constraints_json": {"D": {"1": 2}, "N": {"1": 1}},
            "allow_soft_fallback": False,
        },
        "ban_n_to_d": True,
        "ban_e_to_d": True,
        "ban_n_to_e": True,
        "max_consecutive_work_days": 5,
        "max_consecutive_nights": 3,
        "two_offs_after_two_nig": True,
        "two_offs_after_three_nig": True,
        "daily_shift_requirements": {"D": 5, "E": 3, "N": 2},
        "daily_shift_requirements_by_day": [
            {"D": 5, "E": 3, "N": 2},
            {"D": 5, "E": 3, "N": 2},
            {"D": 5, "E": 3, "N": 2},
        ],
    }


def test_team_min_disable_module_clears_team_min():
    cfg = _baseline_config()
    out = apply_adjustments_to_config(
        cfg,
        [ConstraintAdjustment(family="TeamMin", action="disable_module")],
    )
    assert out["team_min_by_team"] == {}
    assert cfg["team_min_by_team"]  # original untouched (deepcopy)
    assert out["_applied_constraint_adjustments"][0]["family"] == "TeamMin"
    assert out["_applied_constraint_adjustments"][0]["action"] == "disable_module"


def test_team_min_force_soft_mode_sets_flag():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="TeamMin", action="force_soft_mode")],
    )
    assert out["team_min_soft_fallback"] is True


def test_team_min_set_threshold_overrides_value():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="TeamMin",
                action="set_threshold",
                scope_filter={"team": "T1", "shift": "D"},
                target_value=1,
            )
        ],
    )
    assert out["team_min_by_team"]["T1"]["D"] == 1
    assert out["team_min_by_team"]["T1"]["N"] == 1
    assert out["team_min_by_team"]["T2"]["D"] == 1


def test_team_min_narrow_scope_drops_team():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="TeamMin", action="narrow_scope", scope_filter={"team": "T1"}
            )
        ],
    )
    assert "T1" not in out["team_min_by_team"]
    assert "T2" in out["team_min_by_team"]


def test_grade_max_force_soft_sets_force_flag():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="GradeMax", action="force_soft_mode")],
    )
    assert out["_force_grade_max_soft_fallback"] is True


def test_grade_min_force_soft_sets_grade_config_allow_soft():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="GradeMin", action="force_soft_mode")],
    )
    assert out["grade_config"]["allow_soft_fallback"] is True
    assert out["_force_grade_min_soft_fallback"] is True


def test_grade_disable_module_clears_constraints():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="GradeMin", action="disable_module")],
    )
    assert out["grade_config"]["constraints"] == {}
    assert out["grade_config"]["constraints_json"] == {}


def test_boundary_transition_disable_module_clears_bans():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="BoundaryTransitionBan", action="disable_module")],
    )
    assert out["ban_n_to_d"] is False
    assert out["ban_e_to_d"] is False
    assert out["ban_n_to_e"] is False


def test_consecutive_work_set_threshold():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="ConsecutiveWorkLimit", action="set_threshold", target_value=7
            )
        ],
    )
    assert out["max_consecutive_work_days"] == 7


def test_night_recovery_disable_module():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="NightRecovery", action="disable_module")],
    )
    assert out["two_offs_after_two_nig"] is False
    assert out["two_offs_after_three_nig"] is False


def test_coverage_min_set_threshold_per_day():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="CoverageMin",
                action="set_threshold",
                scope_filter={"day": 2, "shift": "N"},
                target_value=1,
            )
        ],
    )
    assert out["daily_shift_requirements_by_day"][1]["N"] == 1


def test_unknown_family_raises():
    with pytest.raises(ValueError, match="unknown ontology family"):
        apply_adjustments_to_config(
            _baseline_config(),
            [ConstraintAdjustment(family="ImaginaryFamily", action="disable_module")],
        )


def test_unsupported_action_raises():
    with pytest.raises(ValueError, match="unsupported action"):
        apply_adjustments_to_config(
            _baseline_config(),
            [ConstraintAdjustment(family="TeamMin", action="rocket_science")],
        )


def test_unsupported_family_action_pair_raises():
    with pytest.raises(ValueError, match="unsupported adjustment"):
        apply_adjustments_to_config(
            _baseline_config(),
            [ConstraintAdjustment(family="GradeMin", action="set_threshold", target_value=1)],
        )


def test_dict_input_normalizes():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [{"family": "TeamMin", "action": "disable_module", "reason": "test"}],
    )
    assert out["team_min_by_team"] == {}
    assert out["_applied_constraint_adjustments"][0]["reason"] == "test"


def test_alias_resolution_works():
    # team_min ontology aliases include TEAM_MIN_EXCEEDS_GLOBAL_NEED
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="TEAM_MIN_EXCEEDS_GLOBAL_NEED", action="disable_module"
            )
        ],
    )
    assert out["team_min_by_team"] == {}


def test_supported_actions_for_team_min_returns_all_four():
    actions = supported_actions_for("TeamMin")
    assert set(actions) == {"disable_module", "force_soft_mode", "set_threshold", "narrow_scope"}


def test_ban_night_before_fixed_off_disable_module_sets_flag():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="BanNightBeforeFixedOff", action="disable_module")],
    )
    assert out["ban_night_before_fixed_off"] is False


def test_weekend_off_only_disable_module_sets_flag():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="WeekendOffOnly", action="disable_module")],
    )
    assert out["weekend_off_only_enable"] is False


def test_monthly_night_cap_set_threshold_writes_max_night():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="MonthlyNightCap", action="set_threshold", target_value=8
            )
        ],
    )
    assert out["max_night_shifts_per_month"] == 8


def test_monthly_night_cap_alias_resolves_to_family():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="MONTHLY_NIGHT_CAPACITY_SHORTAGE",
                action="set_threshold",
                target_value=10,
            )
        ],
    )
    assert out["max_night_shifts_per_month"] == 10
    assert out["_applied_constraint_adjustments"][0]["family"] == "MonthlyNightCap"


def test_monthly_night_cap_rejects_negative_threshold():
    import pytest as _pytest
    with _pytest.raises(ValueError, match="non-negative int"):
        apply_adjustments_to_config(
            _baseline_config(),
            [
                ConstraintAdjustment(
                    family="MonthlyNightCap", action="set_threshold", target_value=-1
                )
            ],
        )


def test_multiple_adjustments_apply_in_order():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="TeamMin",
                action="set_threshold",
                scope_filter={"team": "T1", "shift": "D"},
                target_value=1,
            ),
            ConstraintAdjustment(family="TeamMin", action="disable_module"),
        ],
    )
    # Last write wins; disable_module clears entire map
    assert out["team_min_by_team"] == {}
    assert len(out["_applied_constraint_adjustments"]) == 2
