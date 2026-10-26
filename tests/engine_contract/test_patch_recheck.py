"""EI-12 12b — 처방 적용 직후 재검(`card_filter.assert_treatment_patch_allowed` · 설계 §4.3 BR-3 마지막 방어선).

진입 관문은 엄격 표본으로 미리 거르고, 이 재검은 **실제 설정에 적용한 결과**의 적용 기록을 다시 본다. 12b 시험은 지금 이미 살아 있는
금지 처방(`night_recovery`·`transition_ban`)으로 쓴다 — 별칭 정본화(12c) 없이도 성립한다.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from services.policy import card_filter as cf
from services.treatment_applicator import apply_treatments

_APPLIED = "_applied_constraint_adjustments"
BASE = {"max_conseq_work": 4, "max_nig_per_month": 8, "allow_three_consecutive_nights": True, "off_days": 8, "ban_shift_transitions": True,
        "two_offs_after_two_nig": True, "two_offs_after_three_nig": True, "daily_shift_requirements": {"D": 3, "E": 3, "N": 2}}


@pytest.mark.parametrize("tid,rule", [("treatment:disable:night_recovery", "하드락 #4"), ("treatment:disable:transition_ban", "하드락 #2")])
def test_recheck_refuses_a_hard_lock_breaking_patch(tid, rule):
    patched = apply_treatments([tid], dict(BASE)).patched_config
    with pytest.raises(HTTPException) as err:
        cf.assert_treatment_patch_allowed(patched)
    assert err.value.status_code == 400 and rule in err.value.detail


@pytest.mark.parametrize("tid", ["treatment:disable:ban_night_before_fixed_off", "treatment:soft:grade_max",
                                 "treatment:threshold:monthly_night_cap"])
def test_recheck_passes_marked_and_unrelated_patches(tid):
    cf.assert_treatment_patch_allowed(apply_treatments([tid], dict(BASE)).patched_config)


def test_recheck_ignores_missing_or_unrelated_records():
    for given in (None, {}, {_APPLIED: []}, {_APPLIED: ["x", 1]}):
        cf.assert_treatment_patch_allowed(given)
    cf.assert_treatment_patch_allowed({_APPLIED: [{"family": "TeamMin", "applicable": False, "hard_lock_check_error": True}]})


def test_recheck_is_fail_closed_for_hard_lock_families_when_the_judge_failed():
    record = {"family": "MonthlyNightCap", "applicable": False, "hard_lock_check_error": True}
    with pytest.raises(HTTPException):
        cf.assert_treatment_patch_allowed({_APPLIED: [record]})
