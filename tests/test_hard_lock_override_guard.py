"""EI-11 11b — 요청 가드 방향 표(`.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md` §2.4-5·§2.4-6).

`roster_create_service.assert_night_caps_within_hard_lock`(라우터 3곳 + 생성 진입 관문이 부르는 하나)이 요청의
`config_override` 를 순서 고정으로 판정한다 — ① #6(월 나이트 15)·숫자 검사 → ② 월경계 스위치·내부 키 → ③ 런타임 금지 별칭
(D13 ③ `max_consecutive_nights`) → ④ 그 밖의 하드락 8종 BLOCK. 인접·영향(relax_mark)은 통과한다.

키·별칭·상한·규칙 번호는 레지스트리(`engine_contract.settings.SETTINGS`)에서 읽어 표를 짓는다(R3-06 — 정책 두 벌 금지).
레지스트리에서 지은 행이 지시서 표와 같은지는 `test_direction_table_rows_match_spec` 한 곳에서만 글자로 대조한다.
판정은 기준값 없는 절대값이다(Q1) — 가드는 지금 설정을 모르므로 이미 꺼진 bool 하드락 키를 같은 값 False 로 보내도 400 이다.
"""

from __future__ import annotations

import math

import pytest
from fastapi import HTTPException
from services.engine_contract.names import INTERNAL_TO_EXTERNAL
from services.engine_contract.settings import ADJACENT, AFFECTS, RUNTIME_DATA_NOT_SETTINGS, SETTINGS, HardLock
from services.policy.hard_lock import _FORBIDDEN_INTERNAL_KEYS, rule_label
from services.roster_create_service import assert_night_caps_within_hard_lock as guard

_RULE6 = 6
_RELAX_PREFIX = "하드락 규칙은 요청으로 완화할 수 없습니다: "
_RUNTIME_MESSAGE = "max_consecutive_nights 는 요청으로 보낼 수 없는 키입니다(나이트 연속 상한은 설정의 나이트 3연속으로 정합니다)."


def _hard_lock_rows(kind: str) -> list[tuple[str, HardLock]]:
    """그 하드락 분류의 (키, HardLock) — 정본 키·별칭·바깥(옛) 이름(`spec.column` · NM)이 모두. 옛 이름 요청도 표 전체로 막는다."""
    return [(key, spec.hard_lock) for spec in SETTINGS
            if isinstance(spec.hard_lock, HardLock) and spec.hard_lock.kind == kind
            for key in (spec.key, *spec.aliases, *((spec.column,) if spec.column else ()))]


_CAP_ROWS = [(k, lock) for k, lock in _hard_lock_rows("int_cap") if lock.rule_no != _RULE6]     # #1
_NIGHT_ROWS = [(k, lock) for k, lock in _hard_lock_rows("int_cap") if lock.rule_no == _RULE6]   # #6
_BOOL_ROWS = _hard_lock_rows("bool_on")                                                          # #2·#4·#5·#7·#8
_DERIVED_CANON = [spec.key for spec in SETTINGS
                  if isinstance(spec.hard_lock, HardLock) and spec.hard_lock.kind == "derived_cap"]  # #3 정본
_RUNTIME_ALIASES = [alias for spec in SETTINGS for alias, info in (spec.derived_inputs or {}).items()
                    if info.get("runtime") == "forbidden"]                                       # D13 ③
_BOUNDARY_KEYS = [k for k, _lock in _hard_lock_rows("boundary_switch")]
_ADJACENT_KEYS = [key for spec in SETTINGS if spec.hard_lock in (ADJACENT, AFFECTS)
                  for key in (spec.key, *spec.aliases, *((spec.column,) if spec.column else ()))]   # 바깥(옛) 이름 포함(NM)


def _detail(delta: dict, release=None) -> str | None:
    """400 이면 detail, 통과면 None. 400 밖의 예외는 그대로 올린다."""
    try:
        guard(delta, release)
    except HTTPException as exc:
        assert exc.status_code == 400
        return exc.detail
    return None


def test_direction_table_rows_match_spec():
    """레지스트리에서 지은 표가 지시서 §2.4-6 의 키 행과 같다(표본 무의미 방지 — 행이 비면 아래 테스트가 헛돈다)."""
    assert {k for k, _ in _CAP_ROWS} == {"max_conseq_work", "max_consecutive_work_days", "max_consecutive_work"}
    assert {k for k, _ in _NIGHT_ROWS} == {"max_nig_per_month", "max_night_shifts_per_month"}
    assert {k: lock.rule_no for k, lock in _BOOL_ROWS} == {
        "ban_shift_transitions": 2, "ban_e_to_d": 2, "ban_n_to_d": 2, "ban_n_to_e": 2,
        "two_offs_after_three_nig": 4, "two_offs_after_two_nig": 5, "ban_single_night": 7, "use_mid": 8,
        "banned_day_after_eve": 2, "not_one_night": 7}  # 끝의 둘은 바깥(옛) 이름 — 같은 행으로 막는다(NM)
    assert _DERIVED_CANON == ["allow_three_consecutive_nights"] and _RUNTIME_ALIASES == ["max_consecutive_nights"]
    assert set(_BOUNDARY_KEYS) == {"cross_month_hard_rules_enable", "cross_month_lookback_days"}
    assert {"off_days", "avoid_nod_noe_eod", "ban_night_before_fixed_off", "ban_night_before_fixed_wanted_off", "preceptee_follows_preceptor",
            "apply_confirmed_wanted", "n2n_min_gap"} <= set(_ADJACENT_KEYS)


# ── #1(정수 상한) — > 상한 · 숫자 아님 → 400 / ≤ 상한 · None → 통과 ────────────────────────────


@pytest.mark.parametrize(("key", "lock"), _CAP_ROWS, ids=[k for k, _ in _CAP_ROWS])
def test_int_cap_relax_is_400_with_rule_label_and_key(key, lock):
    for value in (lock.cap + 1, 99, str(lock.cap + 1), "abc", "", math.inf, -math.inf, math.nan, [1]):
        assert _detail({key: value}) == f"{_RELAX_PREFIX}{rule_label(lock.rule_no)} — {key}={value!r}", value


@pytest.mark.parametrize(("key", "lock"), _CAP_ROWS, ids=[k for k, _ in _CAP_ROWS])
def test_int_cap_within_cap_or_none_passes(key, lock):
    for value in (lock.cap, lock.cap - 1, 0, -1, None, True, False, str(lock.cap), float(lock.cap)):
        assert _detail({key: value}) is None, value


@pytest.mark.parametrize(("key", "lock"), _CAP_ROWS + _NIGHT_ROWS, ids=[k for k, _ in _CAP_ROWS + _NIGHT_ROWS])
def test_passing_string_or_float_cap_is_normalized_to_the_judged_int(key, lock):
    """EI-11 11r(리뷰 LOW) — 가드가 `int()` 로 읽어 통과시킨 문자열·실수는 생성 서비스가 관문 뒤에서 그 정수로 바꿔 엔진에 넘긴다
    (예전엔 원값 그대로 가 엔진 TypeError → 500 · 워커 재시도·DLQ). 판정은 그대로(가드 400 ⇔ 카드 BLOCK — C5 ⑦).
    None(D3 미결)·bool(int 하위형)·다른 키는 그대로이고, 바꿀 것이 없으면 받은 dict 그대로다."""
    from services.roster_create_service import _normalize_int_cap_override as normalize

    for value, want in ((str(lock.cap), lock.cap), (f" {lock.cap} ", lock.cap), (float(lock.cap), lock.cap),
                        (lock.cap - 0.5, lock.cap - 1), ("0", 0)):
        assert _detail({key: value}) is None, value
        out = normalize({key: value, "off_days": "6", "two_offs_after_two_nig": True})
        assert out == {key: want, "off_days": "6", "two_offs_after_two_nig": True} and type(out[key]) is int, value
    for same in ({key: lock.cap}, {key: None}, {key: True}, {"off_days": "6"}, {}):
        assert normalize(same) is same
    assert normalize(None) is None


# ── #6(월 나이트) — 기존 문구 글자 그대로 ───────────────────────────────────────────────


@pytest.mark.parametrize(("key", "lock"), _NIGHT_ROWS, ids=[k for k, _ in _NIGHT_ROWS])
def test_monthly_night_cap_keeps_messages_verbatim(key, lock):
    assert _detail({key: lock.cap + 1}) == f"월 나이트는 1인 최대 {lock.cap}회(하드락)입니다: {key}={lock.cap + 1}"
    assert _detail({key: "abc"}) == "월 나이트 값이 숫자가 아닙니다: 'abc'"
    assert _detail({key: math.inf}) == f"월 나이트 값이 숫자가 아닙니다: {math.inf!r}"
    for value in (lock.cap, 0, None, True, str(lock.cap)):
        assert _detail({key: value}) is None, value


# ── bool 하드락(#2·#4·#5·#7·#8) — 끄는 값은 같은 값이어도 400(Q1) / truthy 는 통과 ──────────────────


@pytest.mark.parametrize(("key", "lock"), _BOOL_ROWS, ids=[k for k, _ in _BOOL_ROWS])
def test_bool_hard_lock_off_value_is_400_even_if_already_off(key, lock):
    """끄는 값(False·None·0·"")은 400 — 가드는 기준 설정을 모르므로 이미 꺼진 병동이 같은 값 False 를 보내도 400 이다
    (Q1 의 대가 · 정상 생산자 0 — 수동 API 만)."""
    shown = INTERNAL_TO_EXTERNAL.get(key, key)  # 400 문구의 키 표기는 바깥(옛) 이름이다 — 지금 화면 문구가 바뀌지 않는다(NM X6)
    for value in (False, None, 0, ""):
        assert _detail({key: value}) == f"{_RELAX_PREFIX}{rule_label(lock.rule_no)} — {shown}={value!r}", value


@pytest.mark.parametrize(("key", "lock"), _BOOL_ROWS, ids=[k for k, _ in _BOOL_ROWS])
def test_bool_hard_lock_truthy_value_passes(key, lock):
    """truthy 는 켜진 것으로 본다 — 문자열 'false'·'0' 도 truthy 라 통과(엔진도 truthiness 로 읽는다)."""
    for value in (True, 1, -1, "false", "0", "on"):
        assert _detail({key: value}) is None, value


# ── #3 — 정본은 모든 값 통과(3 if x else 2) · 별칭은 값과 무관하게 400(D13 ③) ─────────────────────


@pytest.mark.parametrize("key", _DERIVED_CANON)
def test_canonical_allow_three_consecutive_nights_passes_every_value(key):
    """정본 `allow_three_consecutive_nights` 는 `3 if x else 2` 라 상한(3)을 못 넘는다 — 2N→3N(True)도 통과(Q12: 절대 상한 3 안)."""
    for value in (True, False, None, 0, 1, 7, -1, "x", ""):
        assert _detail({key: value}) is None, value


@pytest.mark.parametrize("alias", _RUNTIME_ALIASES)
def test_runtime_forbidden_alias_is_400_for_every_value(alias):
    for value in (1, 2, 3, 4, 7, 0, -1, "x", "3", None, True, False, [1], {}, math.inf):
        assert _detail({alias: value}) == _RUNTIME_MESSAGE, value


# ── 인접·영향 — 모든 값 통과(요청 경로엔 표시 칸 없음) ───────────────────────────────────────


@pytest.mark.parametrize("key", _ADJACENT_KEYS)
def test_adjacent_and_affects_keys_pass_every_value(key):
    for value in (0, 1, -5, 99, True, False, None, "x"):
        assert _detail({key: value}) is None, value


# ── 월경계 스위치·내부 키 — 기존 문구 글자 그대로 ───────────────────────────────────────────


def test_boundary_switch_and_internal_key_messages_verbatim():
    prefix = "월경계 하드락은 요청으로 끌 수 없습니다(하드락): "
    assert _detail({"cross_month_hard_rules_enable": False}) == f"{prefix}cross_month_hard_rules_enable=False"
    assert _detail({"cross_month_lookback_days": 0}) == f"{prefix}cross_month_lookback_days=0"
    for key in _FORBIDDEN_INTERNAL_KEYS:
        assert _detail({key: "x"}) == f"{prefix}{key}='x'"
    assert _detail({"cross_month_hard_rules_enable": True, "cross_month_lookback_days": 6}) is None


# ── 엔진 내부 값(RUNTIME_DATA_NOT_SETTINGS) — 값과 무관하게 400(EI-11 11r · 리뷰 HIGH) ─────────────────────

#: 레지스트리에서 짓는다 — `_inbound_source_map`(위 월경계 문구)을 뺀 나머지 전부.
_RUNTIME_DATA_KEYS = [key for key, _part in RUNTIME_DATA_NOT_SETTINGS if key not in _FORBIDDEN_INTERNAL_KEYS]


def test_runtime_data_rows_cover_review_reproductions():
    """표본 무의미 방지 — 리뷰가 재현한 키(월중 경계를 지운 둘 · 고정 칸 우회 · 서버 값이 덮는 꼬리·주휴·차단일)가 행에 있다."""
    assert {"initial_constraints", "off_window_constraints", "fixed_cells", "blocked_by_nurse_id", "weekly_off_map",
            "prev_month_last_main", "prev_month_n_tail", "prev_month_n_offs_after", "prev_month_last_n_k",
            "prev_month_off_tail"} <= set(_RUNTIME_DATA_KEYS)


@pytest.mark.parametrize("key", _RUNTIME_DATA_KEYS)
def test_runtime_data_key_is_400_for_every_value(key):
    """값과 무관하게 ③ 문구로 400 — 리뷰 재현값([1]·{'N07': 'x'}·{'forced_off': 'x'})과 정상 모양·빈 값 모두."""
    for value in ([1], {"N07": "x"}, {"forced_off": "x"}, {"X01": [[19, 19]]}, {}, [], None, "x", 0, True):
        detail = _detail({key: value})
        assert detail is not None and detail.startswith(f"{key} 는 서버가 스스로 계산해 엔진에 넣는 내부 값이라 "), (key, value)


def test_runtime_data_key_order_after_boundary_with_runtime_alias():
    """순서 — ② 월경계가 먼저, ③ 안에서는 런타임 금지 별칭 문구 뒤에 내부 값 문구가 붙는다."""
    assert _detail({"cross_month_hard_rules_enable": False, "initial_constraints": [1]}).startswith(
        "월경계 하드락은 요청으로 끌 수 없습니다")
    detail = _detail({"max_consecutive_nights": 3, "off_window_constraints": {}, "two_offs_after_two_nig": False})
    assert detail.startswith(_RUNTIME_MESSAGE + " off_window_constraints 는 서버가 스스로 계산해")


# ── 순서 고정 — 앞 단계에서 걸리면 그 문구로 끝난다 ────────────────────────────────────────────


def test_order_number6_then_boundary_then_runtime_then_rest():
    over6 = {"max_nig_per_month": 16}
    boundary = {"cross_month_hard_rules_enable": False}
    alias = {"max_consecutive_nights": 3}
    relax = {"two_offs_after_two_nig": False}
    assert _detail({**over6, **boundary}).startswith("월 나이트는 1인 최대 ")
    assert _detail({**over6, **alias}).startswith("월 나이트는 1인 최대 ")
    assert _detail({**boundary, **alias}).startswith("월경계 하드락은 요청으로 끌 수 없습니다")
    assert _detail({**boundary, **relax}).startswith("월경계 하드락은 요청으로 끌 수 없습니다")
    assert _detail({**alias, **relax}) == _RUNTIME_MESSAGE
    release = [{"nurse_id": "N1", "field": "n_max", "value": 16}]
    assert _detail({**alias, **relax}, release).startswith("월 나이트는 1인 최대 "), "release #6 도 ① 이다"


def test_rest_lists_only_block_violations_even_with_relax_marks():
    """④ — 인접·영향 표지(relax_mark)가 섞여도 BLOCK 만 나열한다."""
    detail = _detail({"off_days": 3, "preceptee_on": False, "two_offs_after_two_nig": False, "max_conseq_work": 9})
    assert detail.startswith(_RELAX_PREFIX)
    items = set(detail.removeprefix(_RELAX_PREFIX).split(", "))
    assert items == {f"{rule_label(5)} — two_offs_after_two_nig=False", f"{rule_label(1)} — max_conseq_work=9"}
    assert "off_days" not in detail and "preceptee_on" not in detail


def test_empty_inputs_pass():
    for delta in (None, {}):
        assert _detail(delta) is None
    assert _detail({}, []) is None
