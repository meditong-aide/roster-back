"""apply_hint 검증 — 사용자 동의 후 재실행 호출 정보.

사용자 정책 (2026-05-17):
  team_min 같이 자동 soften 안 하는 lever 는 결과 payload 에 노출 → 사용자
  클릭 → 재실행. apply_hint 가 그 클릭이 호출할 API 정보 (method/url/overrides).
"""

from __future__ import annotations

import pytest

from services.cause_treatment_hitter import propose_bundles
from services.precheck.payload import build_unrecoverable_payload
from services.semantics.ontology import (
    OntologyTreatment,
    build_apply_hint,
)


def test_force_soft_mode_apply_hint_sets_config_to_true():
    t = OntologyTreatment(
        treatment_id="treatment:soft:team_min",
        label="팀 최소 인원을 soft fallback",
        action_type="force_soft_mode",
        target_family="TeamMin",
        config_key="team_min_soft_fallback",
        direction="enable",
        rationale_ko="r",
        trade_off_ko="t",
    )
    h = build_apply_hint(t)
    assert h["method"] == "POST"
    assert h["url"] == "/roster_create/generate"
    assert h["config_overrides"] == {"team_min_soft_fallback": True}
    assert h["user_consent_required"] is True
    assert "팀 최소" in h["human_message_ko"]


def test_disable_module_apply_hint_sets_config_to_false():
    t = OntologyTreatment(
        treatment_id="treatment:disable:night_recovery",
        label="야간 회복 OFF 강제 비활성화",
        action_type="disable_module",
        target_family="NightRecovery",
        config_key="two_offs_after_two_nig",
        direction="disable",
        rationale_ko="r",
        trade_off_ko="t",
    )
    h = build_apply_hint(t)
    assert h["config_overrides"] == {"two_offs_after_two_nig": False}


def test_set_threshold_apply_hint_uses_adjust_dict():
    t = OntologyTreatment(
        treatment_id="treatment:threshold:monthly_night_cap",
        label="월 N cap 상향",
        action_type="set_threshold",
        target_family="MonthlyNightCap",
        config_key="max_night_shifts_per_month",
        direction="increase",
        rationale_ko="r",
        trade_off_ko="t",
    )
    h = build_apply_hint(t)
    assert h["config_overrides"] == {"max_night_shifts_per_month": {"adjust": "increase"}}


def test_data_correction_required_returns_none():
    t = OntologyTreatment(
        treatment_id="treatment:data:fix_config",
        label="Config 수정 (manual)",
        action_type="data_correction_required",
        target_family="ConfigIntegrity",
        config_key=None,
        direction="manual",
        rationale_ko="r",
        trade_off_ko="t",
    )
    assert build_apply_hint(t) is None


def test_manual_investigation_meta_treatment_returns_none():
    """meta:manual_investigation_required 같은 manual lever 는 apply_hint 없음."""
    t = OntologyTreatment(
        treatment_id="treatment:meta:manual_investigation_required",
        label="수동 분석",
        action_type="data_correction_required",
        target_family="HardCase",
        config_key=None,
        direction="manual",
        rationale_ko="r",
        trade_off_ko="t",
    )
    assert build_apply_hint(t) is None


def test_payload_treatments_include_apply_hint():
    violated = [
        {"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
         "node_id": "cause:team:min_over_need",
         "details": {"day": 5, "shift": "D", "min_sum": 6, "required": 4}},
    ]
    payload = build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason="apply_hint test",
        violated_constraints=violated,
        conflict_cores=[],
        pool_snapshot={},
    )
    trs = payload["infeasibility"]["treatment_recommendations"]
    assert trs
    has_apply_hint_treatment = False
    for bundle in trs:
        for t in bundle["treatments"]:
            assert "apply_hint" in t   # 필드 존재
            if t["apply_hint"] is not None:
                has_apply_hint_treatment = True
                ah = t["apply_hint"]
                assert ah["method"] == "POST"
                assert ah["url"] == "/roster_create/generate"
                assert isinstance(ah["config_overrides"], dict)
                assert ah["user_consent_required"] is True
    assert has_apply_hint_treatment, "최소 1 treatment 가 apply_hint 보유해야 함"


def test_propose_bundles_treatments_have_apply_hint():
    bundles = propose_bundles(
        active_causes=["cause:team:min_over_need"],
        max_alternatives=1,
    )
    assert bundles
    for t in bundles[0].treatments:
        # data_correction_required 면 None, 아니면 dict
        if t.action_type == "data_correction_required":
            assert t.apply_hint is None
        else:
            assert t.apply_hint is not None
            assert t.apply_hint["url"] == "/roster_create/generate"
