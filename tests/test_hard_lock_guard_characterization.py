"""EI-7 — `assert_night_caps_within_hard_lock` 특성화 테스트(설계서 §4.8 · STATUS.md).

이 파일은 두 구역으로 나뉜다.

1. **#6(월 나이트 1인 월 15회) 특성화** — EI-7 이전부터 있던 동작을 고정한다. EI-7 은
   이 분기를 "한 글자도 바꾸지 않았다"(STATUS.md)고 주장하므로, 이 구역의 테스트는
   원본 코드에서도 그대로 통과해야 증명이 선다.
2. **월경계 하드락 스위치 거절(EI-7 신규)** — `override_violations` 가 추가되기 전에는
   없던 동작이다. 이 구역은 원본에서는 반드시 **실패**해야 "새 동작이 진짜로 생겼다"가
   증명된다.

원본 대조 방법(수동 — STATUS.md "다음에 이어서 할 순서" 1번):
  `.omc/aide_docs/v2/engine_input/ei7/scratch/head_copy/` 는 `app/`·`tests/` 를 그대로
  복사한 뒤 `roster_create_service.py`·`allowed_shift_types.py` 만 `git show HEAD:...`
  로 되돌리고 `app/services/policy/`(EI-7 신규 패키지)를 삭제한 사본이다. 이 파일을
  그 사본의 `tests/`에 복사해 돌리면 위 1번 구역은 전부 통과, 2번 구역은 전부 실패해야
  한다(실제로 그렇게 확인함 — 이 작업의 Bash 기록).

EI-11 11b(`.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md` §2.4) 뒤 — 가드는 그 밖의 하드락 8종 BLOCK 도
400 으로 막고(옛 'EI-7 은 다른 완화를 일부러 안 막는다' 고정 4개를 의도대로 뒤집었다), `max_consecutive_nights` 는
값과 무관하게 전용 문구로 400 이다(D13 ③). 방향 표 전수는 `tests/test_hard_lock_override_guard.py` 가 본다.
"""

from __future__ import annotations

import math

import pytest
from fastapi import HTTPException
from services.roster_create_service import assert_night_caps_within_hard_lock

# ── 1) #6(월 나이트 15회) 특성화 — head_copy 사본에서도 그대로 통과해야 한다 ─────────


def test_under_cap_passes_without_exception():
    assert_night_caps_within_hard_lock({"max_nig_per_month": 15}, None)


def test_over_cap_raises_400_with_full_message_text():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"max_nig_per_month": 16}, None)
    assert ei.value.status_code == 400
    assert ei.value.detail == "월 나이트는 1인 최대 15회(하드락)입니다: max_nig_per_month=16"


def test_alias_key_over_cap_raises_same_message_shape():
    """`max_night_shifts_per_month` 별칭도 정본과 같은 모양의 400 을 낸다."""
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"max_night_shifts_per_month": 20}, None)
    assert ei.value.status_code == 400
    assert ei.value.detail == "월 나이트는 1인 최대 15회(하드락)입니다: max_night_shifts_per_month=20"


def test_both_keys_over_cap_lists_both_in_detail():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock(
            {"max_nig_per_month": 16, "max_night_shifts_per_month": 17}, None
        )
    assert "max_nig_per_month=16" in ei.value.detail
    assert "max_night_shifts_per_month=17" in ei.value.detail


def test_non_numeric_config_delta_value_raises_400_not_500():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"max_nig_per_month": "abc"}, None)
    assert ei.value.status_code == 400
    assert ei.value.detail == "월 나이트 값이 숫자가 아닙니다: 'abc'"


def test_none_value_in_config_delta_is_not_a_violation():
    assert_night_caps_within_hard_lock({"max_nig_per_month": None}, None)


def test_monthly_limit_release_n_exact_over_cap_blocks():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock(
            None, [{"nurse_id": "n1", "field": "n_exact", "value": 16}]
        )
    assert ei.value.status_code == 400
    assert "n1:n_exact=16" in ei.value.detail


def test_monthly_limit_release_n_max_over_cap_blocks():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock(
            None, [{"nurse_id": "n2", "field": "n_max", "value": 30}]
        )
    assert ei.value.status_code == 400
    assert "n2:n_max=30" in ei.value.detail


def test_monthly_limit_release_n_min_over_cap_also_blocks():
    """`n_min`은 하한 값이지만 이 가드는 필드 구분 없이 수치만 본다(기존 동작)."""
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock(
            None, [{"nurse_id": "n3", "field": "n_min", "value": 16}]
        )
    assert ei.value.status_code == 400
    assert "n3:n_min=16" in ei.value.detail


def test_monthly_limit_release_non_numeric_value_raises_400():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock(
            None, [{"nurse_id": "n4", "field": "n_exact", "value": "abc"}]
        )
    assert ei.value.status_code == 400
    assert ei.value.detail == "월 나이트 값이 숫자가 아닙니다: 'abc'"


def test_monthly_limit_release_non_dict_items_are_ignored():
    assert_night_caps_within_hard_lock(None, ["n1", 123, None, 4.5])


def test_monthly_limit_release_other_field_name_is_ignored():
    assert_night_caps_within_hard_lock(
        None, [{"nurse_id": "n5", "field": "d_max", "value": 999}]
    )


def test_monthly_limit_release_missing_value_key_is_ignored():
    assert_night_caps_within_hard_lock(
        None, [{"nurse_id": "n6", "field": "n_exact"}]
    )


def test_empty_or_none_inputs_pass():
    assert_night_caps_within_hard_lock(None, None)
    assert_night_caps_within_hard_lock({}, [])


# ── 2) 월경계 하드락 스위치 거절(EI-7 신규) — head_copy 사본에서는 실패해야 한다 ─────


def test_boundary_switch_enable_false_blocks_with_400():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"cross_month_hard_rules_enable": False}, None)
    assert ei.value.status_code == 400
    assert "월경계 하드락은 요청으로 끌 수 없습니다" in ei.value.detail
    assert "cross_month_hard_rules_enable=False" in ei.value.detail


def test_boundary_switch_lookback_zero_blocks_with_400():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"cross_month_lookback_days": 0}, None)
    assert ei.value.status_code == 400
    assert "cross_month_lookback_days=0" in ei.value.detail


def test_boundary_switch_lookback_negative_also_blocks():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"cross_month_lookback_days": -3}, None)
    assert ei.value.status_code == 400


def test_boundary_switch_normal_values_pass():
    assert_night_caps_within_hard_lock({"cross_month_hard_rules_enable": True}, None)
    assert_night_caps_within_hard_lock({"cross_month_lookback_days": 6}, None)


def test_boundary_switch_block_takes_priority_message_over_unrelated_pass():
    """월경계 거절과 무관한(#6 통과) 값이 섞여도 월경계 위반이 있으면 400 이 난다."""
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock(
            {"max_nig_per_month": 10, "cross_month_hard_rules_enable": False}, None
        )
    assert "cross_month_hard_rules_enable=False" in ei.value.detail


def test_unrelated_hard_lock_relax_is_rejected_with_400():
    """EI-11 11b — 월경계 외 하드락 완화(#2 off · #8 off 등)도 400 이다(옛 이름 `…_is_not_rejected_by_this_guard` —
    EI-7 은 카드 적용 흐름 보호로 일부러 통과시켰다. 11a 뒤 카드는 같은 판정을 통과한 것만 나와 이제 막는다)."""
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"banned_day_after_eve": False}, None)
    assert ei.value.status_code == 400
    assert ei.value.detail == ("하드락 규칙은 요청으로 완화할 수 없습니다: 하드락 #2(ND·ED·NE 전이 금지)"
                               " — banned_day_after_eve=False")
    # 파이썬 이름(새 이름)으로 와도 같은 400·같은 문구 — 문구의 키 표기는 바깥(옛) 이름이라 화면 문구가 바뀌지 않는다(NM X6)
    with pytest.raises(HTTPException) as ei_new:
        assert_night_caps_within_hard_lock({"ban_shift_transitions": False}, None)
    assert ei_new.value.status_code == 400
    assert ei_new.value.detail == ei.value.detail
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"use_mid": False}, None)
    assert ei.value.status_code == 400 and "하드락 #8" in ei.value.detail and "use_mid=False" in ei.value.detail


# (EI-11 11b) 옛 `test_relax_catalog_producer_values_pass_through_the_guard_unrejected` 는 지웠다 — 기준 dict 에
# `max_consecutive_nights` 가 있어 D13 뒤 400 이 맞고, 카탈로그 `apply` delta 의 가드 통과는 C5
# (`tests/engine_contract/test_card_hard_lock_contract.py` ①·⑦)가 본다.


# ── 월경계 빌더를 깨뜨려 같은 효과를 내는 다른 통로(Codex 리뷰 HIGH) — 전부 400 ──────


@pytest.mark.parametrize("bad_value", ["x", [1], "3.5", math.inf, -math.inf])
def test_non_numeric_max_consecutive_nights_blocks_with_400(bad_value):
    """`max_consecutive_nights` 비숫자 값은 400 이다. 옛 근거는 월경계 빌더의 `L=int(...)` 가 깨져 월경계 하드락이
    통째로 풀리는 것이었다(Codex 리뷰 HIGH — 월경계 문구). D13(EI-11 11b) 뒤 경계는 이 별칭을 읽지 않고, 이 키는
    값과 무관하게 요청으로 보낼 수 없는 키라 전용 문구(`runtime_key_forbidden`)로 400 이다."""
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"max_consecutive_nights": bad_value}, None)
    assert ei.value.status_code == 400
    assert ei.value.detail == ("max_consecutive_nights 는 요청으로 보낼 수 없는 키입니다"
                               "(나이트 연속 상한은 설정의 나이트 3연속으로 정합니다).")


@pytest.mark.parametrize("bad_value", ["x", {}, {"n1": "g1"}])
def test_inbound_source_map_internal_key_blocks_with_400(bad_value):
    """`_inbound_source_map` 은 엔진이 스스로 계산해 주입하는 내부 키라 REST `config_override`
    로 보내면 안 된다 — 인바운드 배정이 없는 달엔 주입이 안 덮어써 그대로 월경계 빌더까지
    가서 깨지고, 월경계 하드락이 통째로 풀린다(Codex 리뷰 HIGH, roster_create_service.py:
    2404·5753-5757·3570-3575 실측)."""
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"_inbound_source_map": bad_value}, None)
    assert ei.value.status_code == 400


@pytest.mark.parametrize("lookback_value", [1, 2, 3, 4, 5, True, 2.9])
def test_lookback_values_below_registry_default_block_with_400(lookback_value):
    """`cross_month_lookback_days` 는 ≤0 뿐 아니라 레지스트리 기본(6) 미만이면 전부 '꺼짐'이다
    — 전월 꼬리가 그 길이로 잘려 월경계 하드락이 부분 해제된다(Codex 리뷰 HIGH, 1~5·True(=1)·
    2.9(=2) 실측 재현 — 이전에는 ≤0 만 걸러 전부 통과했다)."""
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"cross_month_lookback_days": lookback_value}, None)
    assert ei.value.status_code == 400


@pytest.mark.parametrize("lookback_value", [6, 7, 100])
def test_lookback_values_at_or_above_registry_default_pass(lookback_value):
    assert_night_caps_within_hard_lock({"cross_month_lookback_days": lookback_value}, None)


# ── FastAPI 가 받아들이는 JSON Infinity — 500 이 아니라 400/통과여야 한다(Codex 리뷰 MEDIUM) ──


def test_infinity_max_nig_per_month_blocks_with_400_not_500():
    """기존 `_num` 은 OverflowError 를 안 잡아 `max_nig_per_month=Infinity` 가 500 이었다."""
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"max_nig_per_month": math.inf}, None)
    assert ei.value.status_code == 400


def test_infinity_cross_month_lookback_days_blocks_with_400_not_500():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"cross_month_lookback_days": math.inf}, None)
    assert ei.value.status_code == 400


def test_infinity_max_consecutive_nights_blocks_with_400_not_500():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"max_consecutive_nights": math.inf}, None)
    assert ei.value.status_code == 400


def test_infinity_max_conseq_work_no_longer_raises_500():
    """#1 키의 Infinity 는 크래시(500)하지 않는다 — `_coerce_int` 가 OverflowError 를 흡수해 non_numeric(BLOCK)으로
    분류한다. EI-11 11b 뒤 가드는 그 BLOCK 을 400 으로 막는다(옛 동작: 거절하지 않고 통과 — D17 ③ 범위 밖)."""
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock({"max_conseq_work": math.inf}, None)
    assert ei.value.status_code == 400
    assert "하드락 #1" in ei.value.detail and "max_conseq_work=inf" in ei.value.detail


# ── #6 과 월경계 스위치가 동시에 걸리면 #6 메시지가 먼저(순서 고정, Codex 리뷰 LOW) ──


def test_number6_and_boundary_switch_together_keeps_number6_message_verbatim():
    with pytest.raises(HTTPException) as ei:
        assert_night_caps_within_hard_lock(
            {"max_nig_per_month": 16, "cross_month_hard_rules_enable": False}, None
        )
    assert ei.value.status_code == 400
    assert ei.value.detail == "월 나이트는 1인 최대 15회(하드락)입니다: max_nig_per_month=16"
