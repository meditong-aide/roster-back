"""EI-7 — `services.policy.hard_lock` 판정 함수 4종 정책 테스트(설계서 §4.8 · D17).

`config_change_violations`·`override_violations`·`person_attr_violations`·
`sequence_violations` 가 CLAUDE.md 하드락 표 #1~#8 을 레지스트리(`engine_contract
.settings.SETTINGS`·`.rules.RULES`·`.nurse_attrs.NURSE_ATTRS`)와 어긋나지 않게
판정하는지 검증한다.

★ 숫자·키·완화 방향은 **레지스트리에서 읽어 기대값을 만든다** — 정책을 테스트에 다시
적지 않는다(R3-06, '정책 두 벌 금지'). 이 파일에서 숫자를 그대로 적은 곳은 CLAUDE.md
표와 레지스트리가 아직 같은지 교차 확인하는 단언 1개(`test_registry_numbers_match_
claude_md_hard_lock_table`)뿐이다.
"""

from __future__ import annotations

import math

import pytest
from services.cp_sat.allowed_shift_types import HARD_MAX_NIGHTS_PER_MONTH
from services.engine_contract.rules import RULES
from services.engine_contract.settings import ADJACENT, AFFECTS, SETTINGS, HardLock
from services.engine_contract.settings import lookup as _lookup_setting
from services.policy.hard_lock import (
    BLOCK,
    RELAX_MARK,
    config_change_violations,
    override_violations,
    person_attr_violations,
    sequence_violations,
)

# ── 레지스트리에서 뽑은 테스트 전용 조회표(값을 다시 적지 않는다) ──────────────────

_DIRECT_SPECS = {
    s.key: s
    for s in SETTINGS
    if isinstance(s.hard_lock, HardLock) and s.hard_lock.kind in ("int_cap", "bool_on", "derived_cap")
}
_BOOL_SPECS = {k: s for k, s in _DIRECT_SPECS.items() if s.hard_lock.kind == "bool_on"}
_CAP_SPECS = {k: s for k, s in _DIRECT_SPECS.items() if s.hard_lock.kind == "int_cap"}
# 별칭 보유 spec 전부(ADJACENT·AFFECTS 포함 — `off_days` 는 ADJACENT 라 `_DIRECT_SPECS`
# 필터(HardLock 인스턴스만)에는 안 걸린다. STATUS.md 의 "별칭 4개" 가 바로 이 4종이다.
_ALIAS_SPECS = {s.key: s for s in SETTINGS if s.hard_lock is not None and s.aliases}
_BOUNDARY_SPECS = {
    s.key: s
    for s in SETTINGS
    if isinstance(s.hard_lock, HardLock) and s.hard_lock.kind == "boundary_switch"
}
_ADJACENT_OR_AFFECTS_KEYS = sorted(
    s.key for s in SETTINGS if s.hard_lock in (ADJACENT, AFFECTS)
)
_RULE6_CAP = _CAP_SPECS["max_nig_per_month"].hard_lock.cap

_SEQ_SPECS = {rid: spec for rid, spec in RULES.items() if spec.hard_lock_rule_nos}


def _only(violations):
    assert len(violations) == 1, violations
    return violations[0]


# ── int_cap(#1 연속근무·#6 월나이트) — 값 변형 전수 ─────────────────────────────


@pytest.mark.parametrize("key", sorted(_CAP_SPECS))
def test_int_cap_rule_value_variants(key):
    spec = _CAP_SPECS[key]
    cap = spec.hard_lock.cap
    rule_no = spec.hard_lock.rule_no

    assert config_change_violations({key: cap}) == []  # 정확히 상한 — 통과
    assert config_change_violations({key: None}) == []  # 값 없음 — 완화로 안 봄
    assert config_change_violations({key: 0}) == []  # falsy 지만 cap 이하라 통과
    assert config_change_violations({key: -1}) == []  # 음수도 cap 이하라 통과
    assert config_change_violations({key: True}) == []  # int(True)=1 <= cap
    assert config_change_violations({key: False}) == []  # int(False)=0 <= cap
    assert config_change_violations({key: str(cap)}) == []  # 문자열 숫자(상한) — 통과

    v = _only(config_change_violations({key: cap + 1}))  # 상한+1 — 차단
    assert v.kind == BLOCK and v.rule_no == rule_no and v.reason_code == "exceeds_cap"

    v = _only(config_change_violations({key: str(cap + 1)}))  # 문자열 숫자(상한+1) — 차단
    assert v.reason_code == "exceeds_cap"

    v = _only(config_change_violations({key: "abc"}))  # 비숫자 문자열 — 차단
    assert v.kind == BLOCK and v.rule_no == rule_no and v.reason_code == "non_numeric"


@pytest.mark.parametrize("key", sorted(_CAP_SPECS))
def test_int_cap_rule_float_and_large_value_variants(key):
    """float·아주 큰 값(Codex 리뷰 LOW — §4.8 변형 표에 빠져 있었다).

    `int(5.9)==5` 처럼 float 는 절단된다(비숫자가 아니다) — ortools/range() 가 float 를
    거부해 엔진에서 조용히 통과되는 일은 없지만, 가드 판정 규칙 자체는 테스트로 고정한다."""
    spec = _CAP_SPECS[key]
    cap = spec.hard_lock.cap
    rule_no = spec.hard_lock.rule_no

    assert config_change_violations({key: float(cap)}) == []  # cap.0 — 절단해도 cap 이하

    v = _only(config_change_violations({key: float(cap + 1)}))  # (cap+1).0 — 차단
    assert v.kind == BLOCK and v.rule_no == rule_no and v.reason_code == "exceeds_cap"

    v = _only(config_change_violations({key: 99}))  # 아주 큰 값 — 차단
    assert v.kind == BLOCK and v.rule_no == rule_no and v.reason_code == "exceeds_cap"

    v = _only(config_change_violations({key: math.inf}))  # Infinity — 500 아니라 차단(Codex 리뷰 MEDIUM)
    assert v.kind == BLOCK and v.rule_no == rule_no and v.reason_code == "non_numeric"

    v = _only(config_change_violations({key: -math.inf}))
    assert v.reason_code == "non_numeric"


def test_int_cap_override_violations_same_as_config_change_for_non_boundary_keys():
    """월경계 스위치가 아닌 #1·#6 은 `override_violations` 도 `config_change_violations` 와 같다."""
    for key, spec in _CAP_SPECS.items():
        cap = spec.hard_lock.cap
        assert [v.kind for v in override_violations({key: cap + 1})] == [BLOCK]
        assert override_violations({key: cap}) == []


# ── derived_cap(#3 나이트 연속 3일) — 정본은 못 넘고, 별칭만 cap 판정 ────────────


def test_derived_cap_primary_bool_key_never_blocks_regardless_of_value():
    """정본 bool(`three_seq_nig`)은 `3 if x else 2`라 cap(3)을 못 넘는다 — 값과 무관하게 통과."""
    for v in (True, False, None, 0, 1, -1, "abc", ""):
        assert config_change_violations({"three_seq_nig": v}) == []


def test_derived_cap_alias_max_consecutive_nights_value_variants():
    spec = _DIRECT_SPECS["three_seq_nig"]
    cap = spec.hard_lock.cap
    rule_no = spec.hard_lock.rule_no
    alias = spec.aliases[0]
    assert alias == "max_consecutive_nights"

    assert config_change_violations({alias: cap}) == []
    assert config_change_violations({alias: None}) == []
    assert config_change_violations({alias: True}) == []  # int(True)=1 <= 3

    v = _only(config_change_violations({alias: cap + 1}))
    assert v.kind == BLOCK and v.rule_no == rule_no and v.reason_code == "exceeds_cap"


@pytest.mark.parametrize("bad_value", ["abc", [1], "3.5", math.inf, -math.inf])
def test_derived_cap_alias_non_numeric_is_boundary_switch_off_not_plain_non_numeric(bad_value):
    """Codex 리뷰 HIGH — `max_consecutive_nights` 에 숫자 아닌 값('x'·[1]·'3.5'·±inf)을 넣으면 옛 월경계 빌더
    (`L=int(...)`)가 깨져 월경계 하드락 #1·#2·#3·#4·#5·#7 이 통째로 풀렸다 — 그래서 설정 변경 판정은
    `boundary_switch_off`(옛 `non_numeric` 이 아니라)로 분류한다(그대로 둔다 — 설정 저장은 `RosterConfigCreate` 가
    이 키를 버린다). EI-11 11b(D13 ③) 뒤 요청 판정(`override_violations`)은 이 별칭을 값과 무관하게
    `runtime_key_forbidden`(#3) BLOCK 으로 막는다 — D13 ① 뒤 경계는 이 별칭을 읽지 않는다."""
    spec = _DIRECT_SPECS["three_seq_nig"]
    alias = spec.aliases[0]

    v = _only(config_change_violations({alias: bad_value}))
    assert v.kind == RELAX_MARK and v.reason_code == "boundary_switch_off"
    assert v.rule_nos == (1, 2, 3, 4, 5, 7)

    v = _only(override_violations({alias: bad_value}))
    assert v.kind == BLOCK and v.reason_code == "runtime_key_forbidden"
    assert v.rule_nos == (spec.hard_lock.rule_no,) == (3,)


# ── bool_on(#2·#4·#5·#7·#8 + 내부 ban_* 파생 키) — 값 변형 전수 ─────────────────


@pytest.mark.parametrize("key", sorted(_BOOL_SPECS))
def test_bool_on_rule_off_values_block(key):
    spec = _BOOL_SPECS[key]
    rule_no = spec.hard_lock.rule_no
    for off_value in (False, None, 0, ""):
        v = _only(config_change_violations({key: off_value}))
        assert v.kind == BLOCK and v.rule_no == rule_no and v.reason_code == "turned_off"


@pytest.mark.parametrize("key", sorted(_BOOL_SPECS))
def test_bool_on_rule_truthy_values_never_block(key):
    """Python truthy 판정만 본다 — 음수·`'false'` 문자열·`'0'` 문자열도 켜진 것으로 본다."""
    for on_value in (True, 1, -1, "false", "0", "anything"):
        assert config_change_violations({key: on_value}) == []


# ── 인접(ADJACENT)·영향(AFFECTS) — 방향 무관 relax_mark 만(D17 ①, 차단 아님) ─────


@pytest.mark.parametrize("key", _ADJACENT_OR_AFFECTS_KEYS)
def test_adjacent_and_affects_keys_are_relax_mark_never_block(key):
    for value in (0, 1, True, False, None, "x"):
        v = _only(config_change_violations({key: value}))
        assert v.kind == RELAX_MARK
        assert v.reason_code == "adjacent_changed"

    # override_violations 도 같다 — 인접/영향은 차단 승격 대상이 아니다
    for value in (0, 1, True, False, None, "x"):
        v = _only(override_violations({key: value}))
        assert v.kind == RELAX_MARK


# ── 월경계 하드락 스위치 — config_change 는 relax_mark, override 는 block(D17 추가 결정) ──


@pytest.mark.parametrize(
    "key,off_value",
    [("cross_month_hard_rules_enable", False), ("cross_month_lookback_days", 0)],
)
def test_boundary_switch_relax_mark_in_config_change_but_block_in_override(key, off_value):
    assert key in _BOUNDARY_SPECS

    v = _only(config_change_violations({key: off_value}))
    assert v.kind == RELAX_MARK and v.reason_code == "boundary_switch_off"

    v = _only(override_violations({key: off_value}))
    assert v.kind == BLOCK and v.reason_code == "boundary_switch_off"


def test_boundary_switch_normal_values_no_violation_in_either_function():
    for fn in (config_change_violations, override_violations):
        assert fn({"cross_month_hard_rules_enable": True}) == []
        assert fn({"cross_month_lookback_days": 6}) == []


def test_boundary_switch_lookback_negative_is_also_off():
    v = _only(override_violations({"cross_month_lookback_days": -1}))
    assert v.kind == BLOCK and v.reason_code == "boundary_switch_off"


@pytest.mark.parametrize("enable_off_value", [False, None, 0, "", [], {}])
def test_boundary_switch_enable_every_falsy_variant_is_off(enable_off_value):
    """Codex 리뷰 MEDIUM(테스트 공백) — 기존 테스트는 `False` 한 값만 봤다. `not bool(x)`
    판정이라 falsy 는 전부 꺼짐이어야 한다(None·0·''·[]·{} 포함) — 변이 테스트에서 이 값들이
    빠진 채로도 92개가 전부 초록이었다(R2·R3 생존)."""
    v = _only(override_violations({"cross_month_hard_rules_enable": enable_off_value}))
    assert v.kind == BLOCK and v.reason_code == "boundary_switch_off"


@pytest.mark.parametrize("enable_on_value", [True, 1, "false", "0"])
def test_boundary_switch_enable_truthy_variant_passes(enable_on_value):
    assert override_violations({"cross_month_hard_rules_enable": enable_on_value}) == []


@pytest.mark.parametrize(
    "lookback_off_value",
    [None, "abc", "", False, math.nan, 0, -1, 1, 2, 3, 4, 5, True, 2.9],
)
def test_boundary_switch_lookback_every_off_variant_is_blocked(lookback_off_value):
    """Codex 리뷰 HIGH+MEDIUM — `cross_month_lookback_days` 는 ≤0 뿐 아니라 레지스트리
    `current_default`(6) 미만이면 전부 꺼짐이다(1~5·`True`(=1)·2.9(=2) 포함, 전월 꼬리가
    그 길이로 잘려 #1·#3·#4·#5·#7 월경계분이 부분 해제된다 — 실측 재현). `False`(=0)도 같다."""
    v = _only(override_violations({"cross_month_lookback_days": lookback_off_value}))
    assert v.kind == BLOCK and v.reason_code == "boundary_switch_off"


@pytest.mark.parametrize("lookback_on_value", [6, 7, 100])
def test_boundary_switch_lookback_at_or_above_default_passes(lookback_on_value):
    assert override_violations({"cross_month_lookback_days": lookback_on_value}) == []


def test_boundary_switch_rule_nos_exposed_on_violation():
    """Codex 리뷰 LOW — `rule_no` 하나만으론 영향받는 규칙이 #1 로만 집계된다. `rule_nos`
    에 전체(#1·#2·#3·#4·#5·#7)가 담겨야 소비자가 제대로 센다."""
    v = _only(override_violations({"cross_month_hard_rules_enable": False}))
    assert v.rule_nos == (1, 2, 3, 4, 5, 7)


# ── 엔진 내부 전용 키 — 설정·요청 어느 경로로도 값과 무관하게 차단(Codex 리뷰 HIGH) ──


@pytest.mark.parametrize("bad_value", ["x", {}, {"n1": "g1"}, 123, None])
def test_internal_inbound_source_map_key_always_blocked(bad_value):
    """`_inbound_source_map` 은 엔진이 인바운드 배정에서 스스로 계산해 주입하는 캐리오버라
    REST `config_override`·설정 변경 어느 경로로도 정당한 생산자가 없다. 인바운드 배정이
    없는 달엔 주입이 덮어쓰지 않아(roster_create_service.py:5753-5757) 값이 그대로 월경계
    빌더까지 가서 `dict` 가 아니면 깨지고(TypeError), 그 예외를 호출부가 삼켜 월경계
    하드락이 통째로 풀린다 — 값과 무관하게(설령 올바른 dict 라도) 차단한다."""
    for fn in (config_change_violations, override_violations):
        v = _only(fn({"_inbound_source_map": bad_value}))
        assert v.kind == BLOCK and v.reason_code == "boundary_switch_off"
        assert v.rule_nos == (1, 2, 3, 4, 5, 7)


def test_internal_key_violation_coexists_with_other_changes():
    violations = override_violations({"_inbound_source_map": "x", "max_nig_per_month": 10})
    hit = [v for v in violations if v.key == "_inbound_source_map"]
    assert hit and hit[0].kind == BLOCK


# ── 탐색 카탈로그(RELAX_CATALOG) delta 는 월경계 스위치로 오분류되지 않는다(Codex 리뷰 MEDIUM) ───────


def test_relax_catalog_producer_deltas_never_classified_as_boundary_switch_off():
    """Codex 리뷰 MEDIUM — `RELAX_CATALOG`(EI-11 11a-1 뒤 7항목)의 `apply()` delta 를 실제로 넣어,
    `override_violations` 가 이들을 '월경계 스위치를 끄는 방향'(`boundary_switch_off`)으로 오분류하지 않는지만 본다.
    ★ EI-11 11b 뒤 가드는 BLOCK 을 모두 400 으로 막는다(옛 문구 '이것만 가드가 차단한다'·'카드 14종'은 낡았다).
    카탈로그 delta 의 BLOCK 0·가드 통과 본체는 C5(`tests/engine_contract/test_card_hard_lock_contract.py` ①·⑦)다 —
    이 테스트는 delta 만 넣어 계속 통과하므로 BLOCK 회귀를 실패로 알려 주지 않는 보조 검사다."""
    from services.cp_sat.undiagnosed_probe import RELAX_CATALOG

    bases = [
        {},
        {"max_nig_per_month": 10, "max_conseq_work": 4, "max_consecutive_nights": 2,
         "three_seq_nig": False, "two_offs_after_two_nig": True,
         "two_offs_after_three_nig": True, "off_days": 9},
        {"max_nig_per_month": HARD_MAX_NIGHTS_PER_MONTH, "max_conseq_work": 5,
         "max_consecutive_nights": 3, "three_seq_nig": True},
    ]
    for base in bases:
        for item in RELAX_CATALOG:
            delta = item["apply"](base)
            if not delta:
                continue
            boundary_hits = [v for v in override_violations(delta) if v.reason_code == "boundary_switch_off"]
            assert boundary_hits == [], (item["id"], delta, boundary_hits)


# ── #6(월 나이트) 와 월경계 스위치가 함께 걸리면 순서가 고정돼야 한다(Codex 리뷰 LOW) ──


def test_both_number6_and_boundary_switch_violations_are_both_reported_by_policy_layer():
    """정책 계층(`override_violations`)은 둘 다 돌려준다 — 어느 걸 먼저 보여줄지는
    `assert_night_caps_within_hard_lock`(가드)가 #6 을 먼저 체크해 정한다(특성화 테스트
    `test_hard_lock_guard_characterization.py` 가 그 우선순위를 고정한다)."""
    violations = override_violations(
        {"max_nig_per_month": HARD_MAX_NIGHTS_PER_MONTH + 1, "cross_month_hard_rules_enable": False}
    )
    reason_codes = {v.reason_code for v in violations}
    assert {"exceeds_cap", "boundary_switch_off"} <= reason_codes


# ── 정본+별칭 동시 전송(Codex 게이트 사례) — 어느 쪽도 누락되면 안 된다 ──────────


def test_codex_regression_alias_violation_caught_even_when_canonical_looks_fine():
    """Codex 지적 원 사례: `three_seq_nig=True`(정본, 멀쩡해 보임) + `max_consecutive_nights=4`
    (별칭, 위반) — 정본만 보고 끝내면 이 위반을 놓친다."""
    spec = _DIRECT_SPECS["three_seq_nig"]
    changes = {"three_seq_nig": True, "max_consecutive_nights": spec.hard_lock.cap + 1}
    violations = config_change_violations(changes)
    hit = [v for v in violations if v.key == "max_consecutive_nights"]
    assert hit and hit[0].rule_no == spec.hard_lock.rule_no


@pytest.mark.parametrize("key", sorted(_ALIAS_SPECS))
def test_canonical_and_alias_are_each_scanned_independently(key):
    """정본·별칭 중 하나만 위반이어도(둘 다 보낸 채로) 잡혀야 한다 — 어느 쪽도 스킵 금지."""
    spec = _ALIAS_SPECS[key]
    alias = spec.aliases[0]
    lock = spec.hard_lock

    if lock in (ADJACENT, AFFECTS):
        violations = config_change_violations({key: 1, alias: 2})
        hit_keys = {v.key for v in violations}
        assert {key, alias} <= hit_keys
        return

    if lock.kind == "int_cap":
        cap = lock.cap
        only_canonical_bad = config_change_violations({key: cap + 1, alias: cap})
        assert any(v.key == key for v in only_canonical_bad)
        only_alias_bad = config_change_violations({key: cap, alias: cap + 1})
        assert any(v.key == alias for v in only_alias_bad)
    elif lock.kind == "derived_cap":
        cap = lock.cap
        # 정본(bool)은 못 넘으니 별칭만으로 위반을 재현한다(Codex 사례와 같은 모양).
        only_alias_bad = config_change_violations({key: True, alias: cap + 1})
        assert any(v.key == alias for v in only_alias_bad)
    else:  # pragma: no cover - 현재 별칭 보유 키는 위 두 kind 뿐
        pytest.fail(f"예상 못한 hard_lock.kind: {lock.kind}")


# ── person_attr_violations — #6(DIRECT)만 수치 판정, 나머지는 계약만(빈 리스트) ──


def test_person_attr_n_max_over_cap_blocks_with_nurse_id_in_message():
    v = _only(person_attr_violations("n_max", _RULE6_CAP + 1, nurse_id="n7"))
    assert v.rule_no == 6 and v.reason_code == "exceeds_cap"
    assert "n7" in v.message_ko


def test_person_attr_n_exact_over_cap_blocks():
    v = _only(person_attr_violations("n_exact", _RULE6_CAP + 5))
    assert v.rule_no == 6


@pytest.mark.parametrize("attr_key", ["n_max", "n_exact"])
def test_person_attr_direct_key_at_or_under_cap_passes(attr_key):
    assert person_attr_violations(attr_key, _RULE6_CAP) == []
    assert person_attr_violations(attr_key, 1) == []  # 1N 면제 경로 — cap 이하라 결과는 같음(통과)


@pytest.mark.parametrize("attr_key", ["n_max", "n_exact"])
def test_person_attr_direct_key_non_numeric_does_not_crash(attr_key):
    assert person_attr_violations(attr_key, "abc") == []


def test_person_attr_non_direct_key_returns_empty_contract_only():
    """#6·#7 과 관련 있어도 DIRECT 가 아니면(AFFECTS) 현재는 빈 리스트 — 계약만, 실판정 없음."""
    assert person_attr_violations("resignation_date", "2026-01-01") == []
    assert person_attr_violations("fixed_shift", "N") == []


def test_person_attr_unknown_key_returns_empty():
    assert person_attr_violations("no_such_attr_xyz", 999) == []


# ── sequence_violations — 이름표가 끄는 하드락 번호만큼 block, 최소 계약 ────────


@pytest.mark.parametrize("rule_id", sorted(_SEQ_SPECS))
def test_sequence_violations_disabled_true_emits_one_per_hard_lock_number(rule_id):
    spec = _SEQ_SPECS[rule_id]
    violations = sequence_violations(rule_id, disabled=True)
    assert {v.rule_no for v in violations} == set(spec.hard_lock_rule_nos)
    assert all(v.kind == BLOCK and v.reason_code == "rule_disabled" for v in violations)
    assert all(v.key == rule_id for v in violations)


@pytest.mark.parametrize("rule_id", sorted(_SEQ_SPECS))
def test_sequence_violations_message_label_fills_cap_from_registry(rule_id):
    """상한 없이 라벨을 불러도 레지스트리 cap 으로 채운다 — '연속근무 None일 이하' 회귀 방지."""
    for v in sequence_violations(rule_id, disabled=True):
        assert "None" not in v.message_ko
        assert "{cap}" not in v.message_ko


@pytest.mark.parametrize("rule_id", sorted(_SEQ_SPECS))
def test_sequence_violations_disabled_false_is_empty(rule_id):
    assert sequence_violations(rule_id, disabled=False) == []


def test_sequence_violations_rule_without_hard_lock_number_is_always_empty():
    rule_id = next(rid for rid, spec in RULES.items() if not spec.hard_lock_rule_nos)
    assert sequence_violations(rule_id, disabled=True) == []


def test_sequence_violations_unknown_rule_id_is_empty():
    assert sequence_violations("no_such_rule_xyz", disabled=True) == []


def test_sequence_violations_dynamic_prefix_family_not_exact_matched():
    """`RULE_PREFIXES` 동적 규칙(`monthly_limit_*`)은 `RULES.get()` 직접조회 계약이라
    접두 인스턴스 이름(`monthly_limit_n_exact`)으로는 안 걸린다 — 최소 계약 범위(STATUS.md)."""
    assert sequence_violations("monthly_limit_n_exact", disabled=True) == []


# ── SSOT 재수출·CLAUDE.md 교차 확인 ─────────────────────────────────────────────


def test_hard_max_nights_per_month_reexports_registry_cap():
    assert HARD_MAX_NIGHTS_PER_MONTH == _RULE6_CAP


def test_hard_max_nights_per_month_registry_entry_actually_has_a_cap():
    """Codex 리뷰 LOW — 재수출 테스트가 '레지스트리에서 읽는다'를 증명 못 했다(하드코딩
    `HARD_MAX_NIGHTS_PER_MONTH = 15` 로 바꿔도 값이 같아 위 단언이 그대로 통과했다). 레지스트리
    spec 자체에 cap 이 실제로 있는지 직접 확인해 '폴백(15)이 아니라 레지스트리를 읽었다'는
    전제를 따로 고정한다."""
    spec = _lookup_setting("max_nig_per_month")
    assert spec is not None
    assert isinstance(spec.hard_lock, HardLock)
    assert spec.hard_lock.cap == 15


def test_registry_numbers_match_claude_md_hard_lock_table():
    """CLAUDE.md 하드락 표 숫자(#1=5일·#3=3일·#6=15회)가 레지스트리와 같은지 교차 확인.

    이 모듈에서 유일하게 숫자를 그대로 적은 단언이다(R3-06 예외 — '정책 두 벌 비교'가
    아니라 정본 문서 대조용)."""
    assert _CAP_SPECS["max_conseq_work"].hard_lock.cap == 5
    assert _DIRECT_SPECS["three_seq_nig"].hard_lock.cap == 3
    assert _CAP_SPECS["max_nig_per_month"].hard_lock.cap == 15
