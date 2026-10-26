"""EI-12 BR-3 — 별칭 정본화로 '살아난' 처방·레버가 하드락을 완화하지 못한다(설계 §4.3 · 순서 EI-11 → EI-12).

살아난 것(정본화 전엔 엔진이 안 읽는 이름으로 쓰여 효과가 없던 것):
  · `treatment:threshold:monthly_night_cap` — 월 나이트 +1(하드락 #6 안 · `_infer_target_value` 가 15 에서 멈춘다)
  · control `ConsecutiveWorkLimit`·`MonthlyNightCap` 조정(분석 미리보기·처방 적용 경로)
  · `treatment:threshold:consecutive_work` — yaml 키가 정본(`max_conseq_work`, 12s 결정 B)이라 실제 설정에서 처음으로 +1 을 쓴다
    (상한 5 병동에서 6 → 진입 관문·카드 필터·적용 직후 재검이 막는다)
방어선은 셋이다 — ① 요청 가드(원래 이름으로 판정 후 정규화) ② 진입 관문(엄격 표본) ③ 적용 직후 재검(실제 설정 결과).
"""

from __future__ import annotations

import copy

import pytest
from fastapi import HTTPException
from services.constraint_impact.control import _FAMILY_ACTION_MATRIX, apply_adjustments_to_config
from services.cp_sat.undiagnosed_probe import treatments_to_resolution_options
from services.policy import card_filter as cf
from services.policy.hard_lock import BLOCK, override_violations
from services.precheck.treatment_enricher import enrich_treatment_recommendations
from services.semantics.ontology import get_default_ontology
from services.treatment_applicator import apply_treatments

_APPLIED = "_applied_constraint_adjustments"
_CONSEC = "treatment:threshold:consecutive_work"
_NIGHT = "treatment:threshold:monthly_night_cap"
_DEMAND = {"D": 3, "E": 3, "N": 2}


def _base(**kw) -> dict:
    return {"max_conseq_work": 4, "max_nig_per_month": 8, "three_seq_nig": True, "off_days": 8,
            "daily_shift_requirements": _DEMAND, **kw}


BASES = {"typical": _base(), "at_cap": _base(max_conseq_work=5, max_nig_per_month=15), "near_cap": _base(max_conseq_work=5, max_nig_per_month=14),
         "two_n": _base(max_conseq_work=3, max_nig_per_month=6, three_seq_nig=False)}


def test_forbidden_and_marked_treatment_sets_are_unchanged():
    assert sorted(cf.forbidden_treatments()) == ["treatment:disable:night_recovery", "treatment:disable:transition_ban", _CONSEC]
    assert sorted(cf.marked_treatments()) == ["treatment:disable:ban_night_before_fixed_off"]


@pytest.mark.parametrize("name", sorted(BASES))
def test_a_patch_that_breaks_a_hard_lock_comes_only_from_a_forbidden_treatment(name):
    """살아난 처방이 실제 설정(엄격 표본이 아닌)에서 BLOCK 값을 만들면 그 처방은 금지 목록에 있어야 한다(P-1)."""
    forbidden = cf.forbidden_treatments()
    base = BASES[name]
    for tid in sorted(get_default_ontology().treatments):
        try:
            patched = apply_treatments([tid], copy.deepcopy(base)).patched_config
        except ValueError:
            continue
        diff = {k: v for k, v in patched.items() if k != _APPLIED and base.get(k) != v}
        if any(v.kind == BLOCK for v in override_violations(diff)):
            assert tid in forbidden, (name, tid, diff)


def test_revived_night_cap_lever_stops_at_the_hard_lock():
    up = lambda cur: apply_treatments([_NIGHT], _base(max_nig_per_month=cur))  # noqa: E731
    assert up(8).patched_config["max_nig_per_month"] == 9
    assert up(14).patched_config["max_nig_per_month"] == 15
    assert up(15).manual_required == [_NIGHT]  # 15 에서는 올리지 않는다(수동으로 내림)


def test_consecutive_work_treatment_is_refused_at_the_gate_and_never_raises_the_real_cap():
    with pytest.raises(HTTPException) as gate:
        cf.assert_treatments_allowed([_CONSEC])
    assert gate.value.status_code == 400
    # 관문은 엄격 표본(상한 5)으로 늘 400 이다. 관문을 지난다고 가정해도 패치는 실제 설정에 6 을 쓰고 재검이 400 '하드락 #1' 로 끊는다.
    result = apply_treatments([_CONSEC], BASES["at_cap"])
    assert result.patched_config["max_conseq_work"] == 6 and result.manual_required == []
    with pytest.raises(HTTPException) as after:
        cf.assert_treatment_patch_allowed(result.patched_config)
    assert after.value.status_code == 400 and "하드락 #1" in after.value.detail


def test_recheck_refuses_a_control_write_above_the_consecutive_cap():
    """처방 키가 정본이 되는 날을 대비한 고정 — control 이 상한 5 를 넘겨 쓰면 기록에 BLOCK 표지가 달리고 재검이 400 으로 끊는다."""
    adj = {"family": "ConsecutiveWorkLimit", "action": "set_threshold", "target_value": 6}
    patched = apply_adjustments_to_config(copy.deepcopy(BASES["at_cap"]), [adj])
    assert patched["max_conseq_work"] == 6
    with pytest.raises(HTTPException) as after:
        cf.assert_treatment_patch_allowed(patched)
    assert after.value.status_code == 400 and "하드락 #1" in after.value.detail


def test_patch_recheck_passes_within_the_lock_and_ignores_unrelated_records():
    cf.assert_treatment_patch_allowed(apply_treatments([_NIGHT], BASES["near_cap"]).patched_config)
    cf.assert_treatment_patch_allowed(apply_treatments([_CONSEC], BASES["typical"]).patched_config)
    cf.assert_treatment_patch_allowed(None)
    cf.assert_treatment_patch_allowed({})
    cf.assert_treatment_patch_allowed({_APPLIED: [{"family": "TeamMin", "hard_lock_check_error": True}]})  # 하드락 family 아님 → 통과


def test_recheck_is_fail_closed_for_hard_lock_families_when_the_judge_failed():
    record = {"family": "MonthlyNightCap", "applicable": False, "hard_lock_check_error": True}
    with pytest.raises(HTTPException):
        cf.assert_treatment_patch_allowed({_APPLIED: [record]})


def test_every_blocking_control_adjustment_is_marked_and_refused_after_apply():
    scope = {"team": "T1", "shift": "D", "day": 1}
    base = {**BASES["typical"], "team_min_by_team": {"T1": {"D": 1}}, "grade_config": {"constraints": {"D": {"1": 1}}}}
    refused = 0
    for family, actions in sorted(_FAMILY_ACTION_MATRIX.items()):
        for action in sorted(actions):
            for target in ((0, 3, 5, 6, 15, 16, 10**6) if action == "set_threshold" else (None,)):
                adj = {"family": family, "action": action, "target_value": target, "scope_filter": scope}
                after = apply_adjustments_to_config(copy.deepcopy(base), [adj])
                diff = {k: v for k, v in after.items() if k != _APPLIED and base.get(k) != v}
                blocks = [v for v in override_violations(diff) if v.kind == BLOCK]
                record = after[_APPLIED][-1]
                assert bool(blocks) == (record.get("applicable") is False) or family == "ConsecutiveNightLimit", (family, action, target)
                if record.get("applicable") is False:
                    with pytest.raises(HTTPException):
                        cf.assert_treatment_patch_allowed(after)
                    refused += 1
    assert refused >= 8


def test_consecutive_night_limit_stays_always_not_applicable():
    """D13 — 연속 나이트 상한은 런타임 조정 금지다. 설정은 안 쓰고 '적용 불가'만 기록한다."""
    assert cf.family_action_policy()[("ConsecutiveNightLimit", "set_threshold")] == {"mode": "blocked", "rule_nos": [3]}
    base = BASES["typical"]
    for target in (0, 2, 3, 4, 10**6):
        adj = {"family": "ConsecutiveNightLimit", "action": "set_threshold", "target_value": target}
        after = apply_adjustments_to_config(copy.deepcopy(base), [adj])
        assert {k: v for k, v in after.items() if k != _APPLIED} == base
        record = after[_APPLIED][-1]
        assert record["applicable"] is False and record["blocked_by_hard_lock"][0]["rule_nos"] == [3]


def _night_bundle(sized: bool) -> list[dict]:
    treatment = {"treatment_id": _NIGHT, "config_key": "max_nig_per_month", "config_key_label_ko": "월 야간 한도(개인)",
                 "direction": "상향", "direction_label_ko": "상향", "action_type": "set_threshold",
                 "covers": ["cause:capacity:monthly_night_shortage"], "rationale_ko": "개인당 월 최대 야간 +1", "trade_off_ko": "야간 부담 증가"}
    trec = [{"bundle_id": f"bundle:{_NIGHT}", "treatments": [treatment]}]
    if sized:
        details = {"n_required": 180, "night_capable_nurses": [{"nurse_id": str(i), "capacity_days": 21} for i in range(23)]}
        enrich_treatment_recommendations(trec, [{"node_id": None, "reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE", "details": details}])
        assert trec[0]["treatments"][0]["suggested_value"] == 8
    return trec


def test_sized_treatment_is_expressed_by_apply_only_so_it_is_not_applied_twice():
    """프론트는 카드의 `apply`(config_override)와 `treatment_ids` 를 함께 보낸다. 정본화 뒤 처방이 실제로 +1 을 쓰므로, 크기 계산 값이 있는
    처방은 `apply` 로만 표현한다(그러지 않으면 계산값 8 위에 처방 +1 이 얹혀 9)."""
    sized = treatments_to_resolution_options(_night_bundle(sized=True))
    assert [(o["apply"], o["treatment_ids"]) for o in sized] == [({"max_nig_per_month": 8}, [])]
    unsized = treatments_to_resolution_options(_night_bundle(sized=False))
    assert [(o["apply"], o["treatment_ids"]) for o in unsized] == [({}, [_NIGHT])]
