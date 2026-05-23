"""QA §B1 정책 enforcement — max_nig_per_month 등 정책-고정 field 변경 차단.

QA 시나리오 docs/AGENT_QA_SCENARIOS_2026-05-18.md §B1 회귀 가드.
회귀 시 critical (운영 정책 위반 사고).
"""

from __future__ import annotations

from agents_v2.skills.update_constraint import (
    update_constraint,
    _POLICY_LOCKED_FIELDS,
    _reject_policy_locked,
)


def test_max_nig_per_month_rejected_flat_params():
    """flat params (field/value) 로 max_nig 변경 시도 → policy_locked 거절."""
    result = update_constraint(
        db=None,  # 거절은 DB 미접근
        params={
            "group_id": "GRP001",
            "field": "max_nig_per_month",
            "value": 7,
        },
    )
    assert isinstance(result, dict)
    assert result.get("error") == "policy_locked"
    assert result.get("field") == "max_nig_per_month"
    assert "야간 최대" in result.get("field_label", "")
    assert "update_monthly_limit" in result.get("alternative", "")


def test_max_nig_per_month_rejected_updates_dict():
    """updates dict 로 max_nig 변경 시도 → 거절."""
    result = update_constraint(
        db=None,
        params={
            "group_id": "GRP001",
            "updates": {"max_nig_per_month": 7},
        },
    )
    assert result.get("error") == "policy_locked"


def test_mixed_updates_with_locked_field_all_rejected():
    """updates 에 정책-고정 + 일반 field 가 섞이면 전체 거절 (정책 우선)."""
    result = update_constraint(
        db=None,
        params={
            "group_id": "GRP001",
            "updates": {
                "max_nig_per_month": 7,
                "day_req": 5,  # 정상 field 지만 함께 reject
            },
        },
    )
    assert result.get("error") == "policy_locked"


def test_preview_only_also_rejected():
    """preview_only=True 도 거절 — preview 단계에서 차단되어야 사용자 confirm 흐름 진입 X."""
    result = update_constraint(
        db=None,
        params={
            "group_id": "GRP001",
            "field": "max_nig_per_month",
            "value": 7,
            "preview_only": True,
        },
    )
    assert result.get("error") == "policy_locked"


def test_nested_mutation_format_rejected():
    """LLM grounding 의 nested mutation 포맷도 차단."""
    result = update_constraint(
        db=None,
        params={
            "group_id": "GRP001",
            "mutation": {
                "target_field": "max_nig_per_month",
                "target_value": 7,
            },
        },
    )
    assert result.get("error") == "policy_locked"


def test_normal_field_passes_through():
    """정책-고정 외 field 는 정상 흐름 — 거절되지 않음.

    실제 DB 호출은 None db 로 실패하지만 거절 단계는 통과한다는 점만 확인.
    """
    # _reject_policy_locked 가 None 을 반환해야 함
    assert _reject_policy_locked({"day_req": 5}) is None
    assert _reject_policy_locked({"eve_req": 3}) is None
    assert _reject_policy_locked({}) is None


def test_policy_locked_set_documented():
    """정책-고정 field 목록이 명시적으로 정의되어 있고 max_nig_per_month 포함."""
    assert "max_nig_per_month" in _POLICY_LOCKED_FIELDS
    meta = _POLICY_LOCKED_FIELDS["max_nig_per_month"]
    assert "야간" in meta["label_ko"]
    assert "update_monthly_limit" in meta["alternative"]
