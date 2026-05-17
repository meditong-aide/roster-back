"""US-D 검증 — MUS conflict_core / member name → cause_id 자동 추론.

핵심 invariants:
  1. 19 MUS pattern 모두 cause_id 매핑
  2. pattern 누락 시 member name prefix fallback
  3. 알 수 없는 pattern → None (silent)
  4. dedup — 같은 cause_id 중복 안 됨
  5. 새 도메인 (carryover/transition/recovery/preceptee/consecutive) 매핑 검증
"""

from __future__ import annotations

import pytest

from services.precheck.cause_inferer import (
    _MEMBER_PREFIX_TO_CAUSE,
    _PATTERN_TO_CAUSE,
    infer_cause_from_conflict_core,
    infer_causes_from_cores,
)
from services.semantics.ontology import get_default_ontology


# ─────────────────────────────────────────────────────────────────────────
# pattern → cause 매핑 — 19개 전부
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pattern,expected_cause_id", list(_PATTERN_TO_CAUSE.items()))
def test_pattern_to_cause_mapping_resolves_to_valid_ontology(pattern, expected_cause_id):
    onto = get_default_ontology()
    cause = onto.get_cause(expected_cause_id)
    assert cause is not None, f"매핑 대상 cause_id {expected_cause_id} 가 ontology 에 없음"


def test_infer_from_cpsat_mus_pattern_transition_ban():
    core = {"pattern": "cpsat_mus:transition_ban", "members": ["TransitionBanN2D:nurse_3:day_5"]}
    c = infer_cause_from_conflict_core(core)
    assert c is not None
    assert c["node_id"] == "cause:transition:nod_pattern_forces_infeasibility"


def test_infer_from_pattern_recovery_2n2off():
    core = {"pattern": "cpsat_mus:recovery_2n2off", "members": ["Recovery2N2OFF:nurse_5"]}
    c = infer_cause_from_conflict_core(core)
    assert c is not None
    assert c["node_id"] == "cause:recovery:two_n_two_off_blocks_demand"


def test_infer_from_pattern_recovery_3n2off():
    core = {"pattern": "cpsat_mus:recovery_3n2off", "members": ["Recovery3N2OFF:nurse_7"]}
    c = infer_cause_from_conflict_core(core)
    assert c["node_id"] == "cause:recovery:three_n_two_off_blocks_demand"


def test_infer_from_pattern_carryover_boundary():
    core = {"pattern": "cpsat_mus:carryover_boundary", "members": ["CarryoverRecovery3N2OFFGuard:nurse_2:day_0"]}
    c = infer_cause_from_conflict_core(core)
    assert c["node_id"] == "cause:carryover:prev_month_n_tail_blocks_start"


def test_infer_from_pattern_not_one_night():
    core = {"pattern": "cpsat_mus:not_one_night", "members": ["NotOneNight:nurse_4"]}
    c = infer_cause_from_conflict_core(core)
    assert c["node_id"] == "cause:carryover:fixed_n_isolated_by_off_neighbors"


def test_infer_from_pattern_consecutive_work():
    core = {"pattern": "cpsat_mus:max_consecutive_work",
            "members": ["MaxConsecutiveWorkWindow:nurse_1:start_3:k_5"]}
    c = infer_cause_from_conflict_core(core)
    assert c["node_id"] == "cause:consecutive:work_limit_blocks_coverage"


# ─────────────────────────────────────────────────────────────────────────
# member prefix fallback
# ─────────────────────────────────────────────────────────────────────────
def test_infer_fallback_to_member_prefix_when_no_pattern():
    core = {"members": ["TransitionBanE2D:nurse_2:day_8"]}
    c = infer_cause_from_conflict_core(core)
    assert c is not None
    assert c["node_id"] == "cause:transition:nod_pattern_forces_infeasibility"


def test_infer_fallback_longest_prefix_wins():
    """ConsecutiveNightCap 가 ConsecutiveNightCapEdge 같은 더 긴 이름의 prefix 가 됨.
    가장 긴 prefix match 가 정확하게 매칭하는지 확인."""
    core = {"members": ["ConsecutiveNightCapEdge:nurse_4:w_2"]}
    c = infer_cause_from_conflict_core(core)
    # ConsecutiveNightCap prefix 가 그것 자체를 cover
    assert c is not None
    assert c["node_id"] == "cause:capacity:monthly_night_shortage"


def test_infer_member_prefix_fixedcell():
    core = {"members": ["FixedCell:nurse_2:day_5"]}
    c = infer_cause_from_conflict_core(core)
    assert c["node_id"] == "cause:fixed:over_demand"


def test_infer_member_prefix_allowed_shift_mask():
    core = {"members": ["AllowedShiftMaskBanD:nurse_3:day_5"]}
    c = infer_cause_from_conflict_core(core)
    assert c["node_id"] == "cause:eligibility:nurse_isolated"


# ─────────────────────────────────────────────────────────────────────────
# 알 수 없는 → None
# ─────────────────────────────────────────────────────────────────────────
def test_infer_unknown_pattern_returns_none():
    core = {"pattern": "cpsat_mus:unknown_xyz", "members": ["UnknownLiteral:nurse_1"]}
    assert infer_cause_from_conflict_core(core) is None


def test_infer_empty_core_returns_none():
    assert infer_cause_from_conflict_core({}) is None
    assert infer_cause_from_conflict_core(None) is None


def test_infer_no_pattern_no_members_returns_none():
    core = {"some_other_field": "x"}
    assert infer_cause_from_conflict_core(core) is None


# ─────────────────────────────────────────────────────────────────────────
# infer_causes_from_cores — dedup
# ─────────────────────────────────────────────────────────────────────────
def test_dedup_same_cause_id_across_multiple_cores():
    cores = [
        {"pattern": "cpsat_mus:transition_ban", "members": ["TransitionBanN2D:nurse_1:day_5"]},
        {"pattern": "cpsat_mus:transition_ban", "members": ["TransitionBanE2D:nurse_2:day_8"]},
        {"pattern": "cpsat_mus:transition_ban", "members": ["TransitionBanN2E:nurse_3:day_10"]},
    ]
    causes = infer_causes_from_cores(cores)
    assert len(causes) == 1  # 모두 같은 cause_id 로 dedup
    assert causes[0]["node_id"] == "cause:transition:nod_pattern_forces_infeasibility"


def test_multiple_different_causes_preserved():
    cores = [
        {"pattern": "cpsat_mus:transition_ban", "members": ["TransitionBanN2D:n_1:d_5"]},
        {"pattern": "cpsat_mus:recovery_2n2off", "members": ["Recovery2N2OFF:n_2"]},
        {"pattern": "cpsat_mus:max_consecutive_work", "members": ["MaxConsecutiveWorkWindow:n_3"]},
    ]
    causes = infer_causes_from_cores(cores)
    node_ids = {c["node_id"] for c in causes}
    assert node_ids == {
        "cause:transition:nod_pattern_forces_infeasibility",
        "cause:recovery:two_n_two_off_blocks_demand",
        "cause:consecutive:work_limit_blocks_coverage",
    }


def test_silent_skip_when_some_cores_unknown():
    cores = [
        {"pattern": "cpsat_mus:transition_ban", "members": ["TransitionBanN2D:n_1:d_5"]},
        {"pattern": "cpsat_mus:unknown_xyz", "members": ["WeirdLiteral:x"]},
        {"pattern": "cpsat_mus:recovery_2n2off", "members": ["Recovery2N2OFF:n_2"]},
    ]
    causes = infer_causes_from_cores(cores)
    assert len(causes) == 2  # unknown 한 개 skip
    node_ids = {c["node_id"] for c in causes}
    assert "cause:transition:nod_pattern_forces_infeasibility" in node_ids
    assert "cause:recovery:two_n_two_off_blocks_demand" in node_ids


def test_reason_code_uses_first_alias_for_legacy_compat():
    core = {"pattern": "cpsat_mus:transition_ban", "members": ["TransitionBanN2D:n_1"]}
    c = infer_cause_from_conflict_core(core)
    # reason_code 가 cause 의 첫 alias (legacy reason_code 호환)
    onto = get_default_ontology()
    cause = onto.get_cause("cause:transition:nod_pattern_forces_infeasibility")
    expected_first_alias = cause.aliases[0]
    assert c["reason_code"] == expected_first_alias


# ─────────────────────────────────────────────────────────────────────────
# 모든 member prefix 가 valid cause 가리킴
# ─────────────────────────────────────────────────────────────────────────
def test_all_member_prefixes_map_to_valid_cause():
    onto = get_default_ontology()
    invalid: list[tuple[str, str]] = []
    for prefix, cause_id in _MEMBER_PREFIX_TO_CAUSE.items():
        if onto.get_cause(cause_id) is None:
            invalid.append((prefix, cause_id))
    assert invalid == [], f"member prefix 매핑이 invalid cause 를 가리킴: {invalid}"
