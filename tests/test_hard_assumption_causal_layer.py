"""Causal layer 분류 단위 테스트 (hard_assumption.py)."""

from __future__ import annotations

from services.cp_sat.hard_assumption import (
    LAYER_PRIORITY,
    TYPE_TO_LAYER,
    classify_member_type,
    derive_core_layer,
    per_layer_counts,
)


def test_layer_priority_order():
    # policy 가 가장 root, structural / unknown 이 가장 cascade
    assert LAYER_PRIORITY[0] == "policy"
    assert LAYER_PRIORITY[-1] == "unknown"
    assert LAYER_PRIORITY.index("policy") < LAYER_PRIORITY.index("data")
    assert LAYER_PRIORITY.index("data") < LAYER_PRIORITY.index("personal")
    assert LAYER_PRIORITY.index("personal") < LAYER_PRIORITY.index("structural")


def test_team_grade_coverage_are_policy():
    for t in ("TeamMinNode", "GradeMinNode", "GradeMaxNode", "CoverageMinNode"):
        assert classify_member_type(t) == "policy", f"{t} 가 policy 가 아님"


def test_per_nurse_caps_are_personal():
    for t in ("OffCapNode", "NightCapNode", "MonthlyNExactNode", "FixedWantedNode"):
        assert classify_member_type(t) == "personal", f"{t} 가 personal 이 아님"


def test_model_rules_are_structural():
    for t in ("ConsecutiveWorkNode", "TransitionBanNode", "RecoveryOffNode", "NotOneNightNode"):
        assert classify_member_type(t) == "structural", f"{t} 가 structural 이 아님"


def test_forbidden_cell_is_data():
    assert classify_member_type("ForbiddenCellNode") == "data"
    assert classify_member_type("AllowedShiftMaskNode") == "data"


def test_unknown_type_returns_unknown():
    assert classify_member_type("RandomMadeUpNode") == "unknown"
    assert classify_member_type(None) == "unknown"
    assert classify_member_type("") == "unknown"


def test_derive_core_layer_picks_root_most():
    # policy 와 structural 섞여 있으면 policy 가 winning layer
    members = [
        {"type": "TeamMinNode"},
        {"type": "ConsecutiveWorkNode"},
        {"type": "TransitionBanNode"},
    ]
    assert derive_core_layer(members) == "policy"


def test_derive_core_layer_all_structural():
    members = [
        {"type": "ConsecutiveWorkNode"},
        {"type": "ConsecutiveNightCapNode"},
        {"type": "TransitionBanNode"},
    ]
    assert derive_core_layer(members) == "structural"


def test_derive_core_layer_empty_returns_unknown():
    assert derive_core_layer([]) == "unknown"


def test_per_layer_counts_breakdown():
    members = [
        {"type": "TeamMinNode"},
        {"type": "GradeMinNode"},
        {"type": "OffCapNode"},
        {"type": "ConsecutiveWorkNode"},
        {"type": "ConsecutiveWorkNode"},
        {"type": "ForbiddenCellNode"},
    ]
    counts = per_layer_counts(members)
    assert counts == {"policy": 2, "data": 1, "personal": 1, "structural": 2}


def test_per_layer_counts_omits_zero():
    counts = per_layer_counts([{"type": "TeamMinNode"}])
    # 0 짜리는 출력 dict 에 없어야 함
    assert "structural" not in counts
    assert counts == {"policy": 1}


def test_data_and_personal_only_picks_data():
    # data 가 personal 보다 root 다움
    members = [{"type": "ForbiddenCellNode"}, {"type": "OffCapNode"}]
    assert derive_core_layer(members) == "data"
