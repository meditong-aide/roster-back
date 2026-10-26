"""`create_config_from_db` 키 표 고정 — EI-8 설계서 §4.2~§4.7, 부록 A~D.

63키(cfg 필드 1:1) × {부재·None·0·−1·대표값·False} 현행을 고정하고, `effective`·
`effective_hard_lock` 이 같은 값을 내는지 본다. 요구인원 파생(부록 B)·env(부록 C)·범위
밖 51키(§2.4)·레지스트리 교차(§4.5)·§7.3 대표 5행도 여기서 본다.

모든 기대값은 `.omc/aide_docs/v2/engine_input/ei8/scratch/` 의 실측 스크립트로 먼저
재현한 뒤 옮겼다(엔진 코드 변경 0, DB 0) — 손으로 베낀 표가 아니다.
"""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass, field

import pytest
from services.engine_contract import settings as S
from services.engine_contract.effective import (
    HARD_LOCK_CFG_KEYS,
    NotInConfig,
    cfg_field_name,
    effective,
    effective_hard_lock,
)

APP_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2] / "app"
BASE: dict[str, object] = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 1}}
_NA = object()  # bool 기본값 키가 아니면 False 변형을 두지 않는다


@dataclass(frozen=True)
class Raises:
    """값이 아니라 예외 타입을 기대한다는 표지. `type(ex) is Raises.exc_type` 로 비교한다."""

    exc_type: type[BaseException]


@dataclass(frozen=True)
class Row:
    key: str
    field: str
    absent: object
    none: object
    zero: object
    minus1: object
    rep_in: object
    rep_out: object
    false_out: object = _NA
    extra: tuple[tuple[object, object], ...] = ()


# ── 부록 A. 키 표(63행, 현행 실측) ────────────────────────────────────────
KEY_TABLE: tuple[Row, ...] = (
    Row('max_conseq_work', 'max_consecutive_work_days', 5, Raises(TypeError), 0, -1, 4, 4, _NA, extra=((7, 7), ('6', '6'))),
    Row('banned_day_after_eve', 'banned_day_after_eve', True, None, 0, -1, False, False, False, extra=(('false', 'false'),)),
    Row('three_seq_nig', 'max_consecutive_nights', 3, 2, 2, 3, False, 2, 2, extra=((2, 3), (7, 3))),
    Row('two_offs_after_three_nig', 'two_offs_after_three_nig', True, None, 0, -1, False, False, False),
    Row('two_offs_after_two_nig', 'two_offs_after_two_nig', False, None, 0, -1, True, True, False),
    Row('max_nig_per_month', 'max_night_shifts_per_month', 15, 15, 15, 15, 4, 4, _NA, extra=((20, 20), (True, True), ('20', Raises(TypeError)))),
    Row('not_one_night', 'not_one_night', False, None, 0, -1, True, True, False),
    Row('use_mid', 'use_mid', False, False, False, True, True, True, False),
    Row('off_days', 'standard_personal_off_days', 8, None, 0, -1, 4, 4, _NA),
    Row('nod_noe', 'nod_noe', True, None, 0, -1, False, False, False),
    Row('preceptee_on', 'preceptee_on', True, False, False, True, False, False, False),
    Row('ban_night_before_fixed_wanted_off', 'ban_night_before_fixed_wanted_off', False, False, False, True, True, True, False),
    Row('min_exp_per_shift', 'min_experience_per_shift', 3, None, 0, -1, 4, 4, _NA),
    Row('req_exp_nurses', 'required_experienced_nurses', 1, None, 0, -1, 4, 4, _NA),
    Row('two_offs_per_week', 'enforce_two_offs_per_week', True, None, 0, -1, False, False, False),
    Row('max_conseq_off', 'max_conseq_off', 3, 3, 3, -1, 4, 4, _NA),
    Row('shift_priority', 'shift_requirement_priority', 0.7, Raises(TypeError), 0.05, 0.05, 0.5, 0.5, _NA),
    Row('sequential_offs', 'sequential_offs', True, None, 0, -1, False, False, False),
    Row('preceptee_shift_count', 'preceptee_shift_count', True, False, False, True, False, False, False),
    Row('off_first', 'off_first', False, False, False, True, True, True, False),
    Row('n2n_min_gap', 'n2n_min_gap', 0, 0, 0, -1, 4, 4, _NA),
    Row('ban_e_to_d', 'ban_e_to_d', True, False, False, True, False, False, False),
    Row('ban_n_to_d', 'ban_n_to_d', True, False, False, True, False, False, False),
    Row('ban_n_to_e', 'ban_n_to_e', True, False, False, True, False, False, False),
    Row('ban_d_to_n', 'ban_d_to_n', True, False, False, True, False, False, False),
    Row('ban_night_before_fixed_off', 'ban_night_before_fixed_off', True, False, False, True, False, False, False),
    Row('same_shift_hard_k', 'same_shift_hard_k', 3, 0, 0, -1, 4, 4, _NA),
    Row('isolated_work_hard', 'isolated_work_hard', True, False, False, True, False, False, False),
    Row('ban_night_minimize', 'ban_night_minimize', False, False, False, True, True, True, False),
    Row('enforce_4o_hard', 'enforce_4o_hard', False, False, False, True, True, True, False),
    Row('enforce_clustered_offs', 'enforce_clustered_offs', False, False, False, True, True, True, False),
    Row('isolated_off_slack_penalty', 'isolated_off_slack_penalty', 300000, 0, 0, -1, 4, 4, _NA),
    Row('shift_preference_weights', 'shift_preference_weights', {'D': 5.0, 'E': 5.0, 'N': 7.0, 'O': 10.0}, None, 0, -1, {'D': 1.0}, {'D': 1.0}, _NA),
    Row('pair_preference_weight', 'pair_preference_weight', 3.0, None, 0, -1, 0.5, 0.5, _NA),
    Row('preceptor_enable', 'preceptor_enable', True, None, 0, -1, False, False, False),
    Row('preceptor_strength_multiplier', 'preceptor_strength_multiplier', 1.0, None, 0, -1, 0.5, 0.5, _NA),
    Row('preceptor_top_days', 'preceptor_top_days', 12, None, 0, -1, 4, 4, _NA),
    Row('preceptor_min_pair_weight', 'preceptor_min_pair_weight', 5.0, None, 0, -1, 0.5, 0.5, _NA),
    Row('preceptor_focus_shifts', 'preceptor_focus_shifts', None, None, 0, -1, ['N'], ['N'], _NA),
    Row('max_extra_off_days', 'max_extra_off_days', 1, Raises(TypeError), 0, -1, 4, 4, _NA),
    Row('extra_off_penalty_weight', 'extra_off_penalty_weight', 80, 0, 0, -1, 4, 4, _NA),
    Row('soft_max_consecutive_work_days', 'soft_max_consecutive_work_days', 5, 5, 5, -1, 4, 4, _NA),
    Row('soft_consecutive_work_penalty_weight', 'soft_consecutive_work_penalty_weight', 180, 0, 0, -1, 4, 4, _NA),
    Row('max_conseq_off_penalty_weight', 'max_conseq_off_penalty_weight', 5000, 0, 0, -1, 4, 4, _NA),
    Row('max_same_shift_penalty_weight', 'max_same_shift_penalty_weight', 300, 0, 0, -1, 4, 4, _NA),
    Row('n_to_n_interval_penalty_weight', 'n_to_n_interval_penalty_weight', 300, 0, 0, -1, 4, 4, _NA),
    Row('n_to_n_interval_max_window', 'n_to_n_interval_max_window', 15, 0, 0, -1, 4, 4, _NA),
    Row('team_min_penalty_weight', 'team_min_penalty_weight', 80000, 0, 0, -1, 4, 4, _NA),
    Row('team_handoff_penalty_weight', 'team_handoff_penalty_weight', 80000, 0, 0, -1, 4, 4, _NA),
    Row('shadow_coverage_lookback_days', 'shadow_coverage_lookback_days', 6, 0, 0, -1, 4, 4, _NA),
    Row('shadow_coverage_penalty_weight', 'shadow_coverage_penalty_weight', 6, 0, 0, -1, 4, 4, _NA),
    Row('shadow_coverage_need_ratio', 'shadow_coverage_need_ratio', 0.6, 0.0, 0.0, -1.0, 0.5, 0.5, _NA, extra=(('4', 4.0),)),
    Row('n_to_n_interval_target', 'n_to_n_interval_target', 10, 0, 0, -1, 4, 4, _NA, extra=(('4', 4),)),
    Row('max_same_shift', 'max_same_shift', True, False, False, True, False, False, False),
    Row('weekend_off_only_enable', 'weekend_off_only_enable', True, False, False, True, False, False, False),
    Row('team_min_soft_fallback', 'team_min_soft_fallback', False, False, False, True, True, True, False),
    Row('team_handoff_soft_fallback', 'team_handoff_soft_fallback', True, False, False, True, False, False, False),
    Row('oversupply_equalize_enable', 'oversupply_equalize_enable', True, False, False, True, False, False, False),
    Row('oversupply_equalize_weight', 'oversupply_equalize_weight', 120, Raises(TypeError), 0, -1, 4, 4, _NA),
    Row('monthly_preference_weight', 'monthly_preference_weight', 60, 0, 0, -1, 4, 4, _NA),
    Row('distribution_mode', 'distribution_mode', 'hybrid', 'hybrid', 'hybrid', '-1', 'balanced', 'balanced', _NA),
    Row('team_min_by_team', 'team_min_by_team', {}, {}, {}, -1, {'1': {'D': 1}}, {'1': {'D': 1}}, _NA),
    Row('team_handoff_policy_by_team', 'team_handoff_policy_by_team', {}, {}, {}, -1, {'1': {'restrictions': []}}, {'1': {'restrictions': []}}, _NA),
)

KEY_TABLE_BY_KEY: dict[str, Row] = {row.key: row for row in KEY_TABLE}
assert len(KEY_TABLE_BY_KEY) == 63 == len(KEY_TABLE)

REQ_KEYS: frozenset[str] = frozenset(
    {"daily_shift_requirements", "day_req", "eve_req", "nig_req",
     "daily_shift_requirements_by_day", "daily_shift_requirements_max_by_day"}
)

#: 범위 밖 51키 — §2.4. KEY_TABLE·REQ_KEYS 가 아닌 나머지는 전부 여기다(서로소를
#: ①에서 검증한다. 사유별 분류는 레지스트리 source/hard_lock 에서 직접 끌어온다).
#: EI-11 11f 가 DEAD 행 `_force_grade_min_soft_fallback` 을 지워 52 → 51 이다.
OUT_OF_SCOPE_KEYS: frozenset[str] = frozenset(S.SETTINGS_BY_KEY) - set(KEY_TABLE_BY_KEY) - REQ_KEYS


@pytest.fixture(autouse=True)
def _no_mcw_env(monkeypatch):
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)


@pytest.fixture(scope="module")
def _engine():
    from services.cp_sat_basic import CPSATBasicEngine

    return CPSATBasicEngine()


@pytest.fixture
def base_cfg(_engine):
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        return _engine.create_config_from_db(dict(BASE))


def _build(engine, config_data: dict) -> tuple[object, type[BaseException] | None]:
    """`create_config_from_db` 를 print 억제 하에 호출하고 (cfg, 예외타입) 을 돌려준다."""
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        try:
            return engine.create_config_from_db(config_data), None
        except Exception as ex:  # noqa: BLE001 — 예외 타입 자체가 기대값이다
            return None, type(ex)


def _run_variant(engine, key: str, value: object, *, present: bool) -> tuple[object, type[BaseException] | None]:
    cd = copy.deepcopy(BASE)
    if present:
        cd[key] = value
    snapshot = copy.deepcopy(cd)
    result = _build(engine, cd)
    assert cd == snapshot, f"{key}={value!r}: create_config_from_db 가 입력을 변형했다"
    return result


def _assert_matches(expected: object, cfg: object, err: type[BaseException] | None, field: str, label: str) -> None:
    """값·타입 둘 다 고정한다(스칼라만) — `==` 단독이면 `0==False`·`1==True` 같은 강제
    변환이 생겨도 통과해버린다(리뷰 반영, review/mutrun.py E4_max_nig_bool_becomes_int)."""
    if isinstance(expected, Raises):
        assert err is expected.exc_type, f"{label}: {err} 예외를 기대했는데 cfg={cfg!r}"
        return
    assert err is None, f"{label}: 예외 없이 {expected!r} 를 기대했는데 {err} 가 났다"
    actual = getattr(cfg, field)
    if isinstance(expected, (dict, list)):
        assert actual == expected, f"{label}: cfg.{field}={actual!r} != 기대값 {expected!r}"
        return
    assert (type(actual), actual) == (type(expected), expected), (
        f"{label}: cfg.{field}={actual!r}({type(actual).__name__}) != "
        f"기대값 {expected!r}({type(expected).__name__})"
    )


_VARIANTS = ("absent", "none", "zero", "minus1", "rep")


def _variant_cases():
    for row in KEY_TABLE:
        for variant in _VARIANTS:
            yield row, variant
        if row.false_out is not _NA:
            yield row, "false"


@pytest.mark.parametrize(
    "row,variant", list(_variant_cases()), ids=[f"{r.key}-{v}" for r, v in _variant_cases()]
)
def test_key_table_variant(row: Row, variant: str, _engine):
    value_by_variant = {"absent": None, "none": None, "zero": 0, "minus1": -1, "rep": row.rep_in, "false": False}
    expected_by_variant = {
        "absent": row.absent, "none": row.none, "zero": row.zero,
        "minus1": row.minus1, "rep": row.rep_out, "false": row.false_out,
    }
    present = variant != "absent"
    cfg, err = _run_variant(_engine, row.key, value_by_variant[variant], present=present)
    expected = expected_by_variant[variant]
    label = f"{row.key}-{variant}"
    _assert_matches(expected, cfg, err, row.field, label)
    if err is not None:
        return
    # ② effective(cfg, 정본 키) 가 ①과 같다(값·타입). ③ 모든 별칭도 같다.
    _assert_effective_matches(expected, effective(cfg, row.key), label)
    for alias in S.SETTINGS_BY_KEY[row.key].aliases:
        _assert_effective_matches(expected, effective(cfg, alias), f"{label}-alias-{alias}")


def _assert_effective_matches(expected: object, actual: object, label: str) -> None:
    if isinstance(expected, (dict, list)):
        assert actual == expected, f"{label}: effective={actual!r} != 기대값 {expected!r}"
        return
    assert (type(actual), actual) == (type(expected), expected), (
        f"{label}: effective={actual!r}({type(actual).__name__}) != 기대값 {expected!r}({type(expected).__name__})"
    )


@pytest.mark.parametrize(
    "row",
    [row for row in KEY_TABLE if row.extra],
    ids=[row.key for row in KEY_TABLE if row.extra],
)
def test_key_table_extra_rows(row: Row, _engine):
    for extra_input, extra_expected in row.extra:
        cfg, err = _run_variant(_engine, row.key, extra_input, present=True)
        _assert_matches(extra_expected, cfg, err, row.field, f"{row.key}-extra-{extra_input!r}")


def test_banned_day_after_eve_extra_leaves_ban_e_to_d_true(_engine):
    """'false' 는 Python 에서 truthy 라 `if not x` 해제 판정을 통과하지 못한다(실효 완화 아님)."""
    cfg, err = _run_variant(_engine, "banned_day_after_eve", "false", present=True)
    assert err is None
    assert cfg.ban_e_to_d is True and cfg.ban_n_to_d is True and cfg.ban_n_to_e is True


@pytest.mark.parametrize("row", KEY_TABLE, ids=[row.key for row in KEY_TABLE])
def test_cfg_field_name_matches_key_table(row: Row, base_cfg):
    assert cfg_field_name(base_cfg, row.key) == row.field


# ── §4.7 대표 5행 재현(§7.3) ──────────────────────────────────────────────


def test_section73_max_nig_per_month(_engine):
    """표 상수끼리가 아니라 엔진을 직접 돌려 재현한다(리뷰 반영 — 기존판은 table 자기 비교였다)."""
    for value, present, expected in ((None, False, 15), (None, True, 15), (0, True, 15), (-1, True, 15)):
        cfg, err = _run_variant(_engine, "max_nig_per_month", value, present=present)
        assert err is None and cfg.max_night_shifts_per_month == expected


def test_section73_max_conseq_work(_engine):
    cfg, err = _run_variant(_engine, "max_conseq_work", None, present=False)
    assert err is None and cfg.max_consecutive_work_days == 5
    _, err_none = _run_variant(_engine, "max_conseq_work", None, present=True)
    assert err_none is TypeError
    for value, expected in ((0, 0), (-1, -1)):
        cfg, err = _run_variant(_engine, "max_conseq_work", value, present=True)
        assert err is None and cfg.max_consecutive_work_days == expected


def test_section73_banned_day_after_eve_and_ban_star(_engine):
    for value, present, expect_banned, expect_ban_star in (
        (None, False, True, True),  # 부재
        (None, True, None, False),
        (0, True, 0, False),
        (-1, True, -1, True),
    ):
        cfg, err = _run_variant(_engine, "banned_day_after_eve", value, present=present)
        assert err is None
        assert cfg.banned_day_after_eve == expect_banned
        for ban_field in ("ban_e_to_d", "ban_n_to_d", "ban_n_to_e", "ban_d_to_n"):
            assert getattr(cfg, ban_field) is expect_ban_star


def test_section73_n_to_n_interval_target(_engine):
    for value, present, expected in ((None, False, 10), (None, True, 0), (0, True, 0), (-1, True, -1)):
        cfg, err = _run_variant(_engine, "n_to_n_interval_target", value, present=present)
        assert err is None and cfg.n_to_n_interval_target == expected


def test_section73_off_days_three_columns(_engine):
    """cfg(`standard_personal_off_days`) · 로그(`resolve_effective_off_days(config_data)`) ·
    lex(`resolve_effective_off_days(cfg)`) 세 칸이 서로 다르다(BR-2)."""
    from services.cp_sat.off_policy import resolve_effective_off_days

    cases = (
        (False, None, 8, 8, 8),
        (True, None, None, 8, 0),
        (True, 0, 0, 0, 0),
        (True, -1, -1, 0, 0),
    )
    for present, value, expect_cfg, expect_log, expect_lex in cases:
        cd = copy.deepcopy(BASE)
        if present:
            cd["off_days"] = value
        cfg, err = _build(_engine, cd)
        assert err is None
        assert cfg.standard_personal_off_days == expect_cfg
        assert resolve_effective_off_days(cd)[0] == expect_log
        assert resolve_effective_off_days(cfg)[0] == expect_lex


# ── §4.9③ — effective_hard_lock vs 직접 cfg(극단값) ─────────────────────────
#: 기존 테스트는 {}·None·별칭·골든 14건만 넣어, 래퍼가 상한을 몰래 깎거나(W3) bool 을
#: 몰래 접어도(W2) 못 잡았다(리뷰 반영, review/mutrun.py W2_bool_fold_non_none·
#: W3_clamp_int_caps 둘 다 기존 545 건을 통과했다). 상한 초과·0·−1·False·문자열을 추가한다.
_HARDLOCK_EXTREME_VALUES: dict[str, tuple[object, ...]] = {
    "max_conseq_work": (None, 0, -1, 7, "6"),
    "banned_day_after_eve": (None, 0, -1, False, "false"),
    "three_seq_nig": (None, 2, 7, False),
    "two_offs_after_three_nig": (None, 0, -1, False),
    "two_offs_after_two_nig": (None, 0, -1, True),
    "max_nig_per_month": (None, 0, -1, 20, True, "20"),
    "not_one_night": (None, 0, -1, True),
    "use_mid": (None, 0, -1, True),
}


@pytest.mark.parametrize(
    "key,value",
    [(k, v) for k, vs in _HARDLOCK_EXTREME_VALUES.items() for v in vs],
    ids=[f"{k}={v!r}" for k, vs in _HARDLOCK_EXTREME_VALUES.items() for v in vs],
)
def test_effective_hard_lock_matches_direct_cfg_for_extreme_values(key, value, _engine):
    """§4.9③ 구현 — 극단값에서도 래퍼와 직접 cfg 가 같은 값(또는 같은 예외)을 낸다."""
    import contextlib
    import io

    cd = {**BASE, key: value}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            wrapped = effective_hard_lock(cd)
        wrapped_err = None
    except TypeError:
        wrapped, wrapped_err = None, TypeError
    cfg, direct_err = _build(_engine, dict(cd))
    assert wrapped_err == direct_err
    if wrapped_err is None:
        direct = {h: effective(cfg, h) for h in HARD_LOCK_CFG_KEYS}
        assert wrapped == direct


def test_effective_hard_lock_extreme_values_locked_literal():
    """대표 극단값 3개를 리터럴로도 고정한다(상한 초과가 깎이지 않음을 못 박는다)."""
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        assert effective_hard_lock({**BASE, "max_conseq_work": 7})["max_conseq_work"] == 7
        assert effective_hard_lock({**BASE, "max_nig_per_month": 20})["max_nig_per_month"] == 20
        r = effective_hard_lock({**BASE, "banned_day_after_eve": "false"})
    assert r["banned_day_after_eve"] == "false" and r["ban_e_to_d"] is True


# ── 부록 B. 요구인원 파생 표(REQ_TABLE) ───────────────────────────────────

_NOT_SET = object()
_ABSENT_ATTR = object()


@dataclass(frozen=True)
class ReqRow:
    name: str
    config_data: dict = field(default_factory=dict)
    expected: object = _NOT_SET  # dsr dict 또는 Raises
    by_day: object = _NOT_SET  # _ABSENT_ATTR·list·_NOT_SET(검사 안 함)
    max_by_day: object = _NOT_SET


REQ_TABLE: tuple[ReqRow, ...] = (
    ReqRow("dsr 부재", {}, Raises(ValueError)),
    ReqRow("dsr None", {"daily_shift_requirements": None}, Raises(ValueError)),
    ReqRow("dsr 0", {"daily_shift_requirements": 0}, Raises(ValueError)),
    ReqRow("dsr -1", {"daily_shift_requirements": -1}, Raises(ValueError)),
    ReqRow(
        "혼합 대소문자·모르는 키",
        {"daily_shift_requirements": {"d": "2", "E": 1, "n": None, "X": 5}},
        {"D": 2, "E": 1, "N": 0},
    ),
    ReqRow("use_mid=True", {**BASE, "use_mid": True}, {"D": 1, "E": 1, "N": 1, "M": 0}),
    ReqRow("W 키", {"daily_shift_requirements": {"D": 1, "W": 2}}, {"D": 1, "E": 0, "N": 0, "W": 2}),
    ReqRow("day_req=2(dsr 부재 — 구 필드 폴백 삭제, EI-13d)", {"day_req": 2}, Raises(ValueError)),
    ReqRow("day_req=None(dsr 부재)", {"day_req": None}, Raises(ValueError)),
    ReqRow("day_req=9 + dsr 있음(폴백 무시)", {**BASE, "day_req": 9}, {"D": 1, "E": 1, "N": 1}),
    ReqRow("by_day 부재", dict(BASE), {"D": 1, "E": 1, "N": 1}, by_day=_ABSENT_ATTR),
    ReqRow("by_day None", {**BASE, "daily_shift_requirements_by_day": None}, {"D": 1, "E": 1, "N": 1}, by_day=_ABSENT_ATTR),
    ReqRow("by_day []", {**BASE, "daily_shift_requirements_by_day": []}, {"D": 1, "E": 1, "N": 1}, by_day=_ABSENT_ATTR),
    ReqRow(
        "by_day [{'D':3}](D/E/N 만 — 보완 분기가 채울 추가 키 없음)",
        {**BASE, "daily_shift_requirements_by_day": [{"D": 3}]},
        {"D": 1, "E": 1, "N": 1},
        by_day=[{"D": 3, "E": 0, "N": 0}],
    ),
    ReqRow(
        "by_day [{'D':3}] + dsr 에 W(보완 분기가 살아 있다 — R-2 재정정)",
        {"daily_shift_requirements": {"D": 1, "W": 2}, "daily_shift_requirements_by_day": [{"D": 3}]},
        {"D": 1, "E": 0, "N": 0, "W": 2},
        by_day=[{"D": 3, "E": 0, "N": 0, "W": 2}],
    ),
    ReqRow(
        "by_day [{'D':3}] + dsr 에 M(use_mid 꺼짐, 보완 분기가 살아 있다 — R-2 재정정)",
        {
            "daily_shift_requirements": {"D": 1, "E": 1, "N": 1, "M": 1},
            "daily_shift_requirements_by_day": [{"D": 3}],
        },
        {"D": 1, "E": 1, "N": 1, "M": 1},
        by_day=[{"D": 3, "E": 0, "N": 0, "M": 1}],
    ),
    ReqRow(
        "by_day ['x'](비dict 칸은 기본 요구치)",
        {**BASE, "daily_shift_requirements_by_day": ["x"]},
        {"D": 1, "E": 1, "N": 1},
        by_day=[{"D": 1, "E": 1, "N": 1}],
    ),
    ReqRow(
        "max_by_day [{'D':2}]",
        {**BASE, "daily_shift_requirements_max_by_day": [{"D": 2}]},
        {"D": 1, "E": 1, "N": 1},
        max_by_day=[{"D": 2, "E": 0, "N": 0}],
    ),
    ReqRow(
        "max_by_day [{'D':None}]",
        {**BASE, "daily_shift_requirements_max_by_day": [{"D": None}]},
        {"D": 1, "E": 1, "N": 1},
        max_by_day=[{"D": 0, "E": 0, "N": 0}],
    ),
)


@pytest.mark.parametrize("row", REQ_TABLE, ids=[r.name for r in REQ_TABLE])
def test_req_table(row: ReqRow, _engine):
    cfg, err = _build(_engine, dict(row.config_data))
    if isinstance(row.expected, Raises):
        assert err is row.expected.exc_type, f"{row.name}: {err} != {row.expected.exc_type}"
        return
    assert err is None, f"{row.name}: 예외 없이 끝나야 하는데 {err} 가 났다"
    assert cfg.daily_shift_requirements == row.expected
    if row.by_day is not _NOT_SET:
        actual = vars(cfg).get("daily_shift_requirements_by_day", _ABSENT_ATTR)
        assert actual == row.by_day
    if row.max_by_day is not _NOT_SET:
        actual = vars(cfg).get("daily_shift_requirements_max_by_day", _ABSENT_ATTR)
        assert actual == row.max_by_day


def test_req_table_day_req_not_in_config(base_cfg):
    """`day_req`·`eve_req`·`nig_req` 는 cfg 필드가 아니다 — 실효값은
    `effective(cfg,'daily_shift_requirements')['D']` 로 본다."""
    with pytest.raises(NotInConfig):
        effective(base_cfg, "day_req")
    assert effective(base_cfg, "daily_shift_requirements")["D"] == 1


def test_req_table_by_day_default_none_when_unset(base_cfg):
    with pytest.raises(NotInConfig):
        effective(base_cfg, "daily_shift_requirements_by_day")
    assert effective(base_cfg, "daily_shift_requirements_by_day", default=None) is None


# ── 부록 C. env `AIDE_MAX_CONSEC_WORK` 행 ─────────────────────────────────

ENV_TABLE: tuple[tuple[str, object, bool, int, int], ...] = (
    # (env 값, max_conseq_work 입력, present, 기대 hard, 기대 soft)
    ("4", None, False, 4, 4),
    ("4", None, True, 4, 4),
    ("4", 0, True, 4, 4),
    ("4", -1, True, -1, -1),
    ("4", 7, True, 4, 4),
    ("6", None, False, 5, 5),
    ("6", None, True, 5, 5),
    ("6", 0, True, 5, 5),
    ("6", -1, True, -1, -1),
    ("6", 7, True, 6, 6),  # cap 5 를 넘긴다 — `cp_sat_basic.py:457-458` 주석과 일치(리뷰로
                           # R-6 재검토: 어긋남이 아니다. env=min() 은 DB 값을 "못 올리게"
                           # 막을 뿐, cap(5) 자체를 강제하지는 않는다)
)


@pytest.mark.parametrize(
    "env_value,value,present,expect_hard,expect_soft",
    ENV_TABLE,
    ids=[f"env{e}-{'absent' if not p else v}" for e, v, p, *_ in ENV_TABLE],
)
def test_env_table(env_value, value, present, expect_hard, expect_soft, monkeypatch, _engine):
    monkeypatch.setenv("AIDE_MAX_CONSEC_WORK", env_value)
    cd = copy.deepcopy(BASE)
    if present:
        cd["max_conseq_work"] = value
    cfg, err = _build(_engine, cd)
    assert err is None
    assert cfg.max_consecutive_work_days == expect_hard
    assert cfg.soft_max_consecutive_work_days == expect_soft
    assert effective(cfg, "max_conseq_work") == expect_hard


# ── §4.4 범위 표 — 새 설정·새 읽기를 빨갛게 ────────────────────────────────


def test_scope_partition_covers_all_settings_disjointly():
    key_table_keys = set(KEY_TABLE_BY_KEY)
    assert key_table_keys.isdisjoint(REQ_KEYS)
    assert key_table_keys.isdisjoint(OUT_OF_SCOPE_KEYS)
    assert REQ_KEYS.isdisjoint(OUT_OF_SCOPE_KEYS)
    assert key_table_keys | REQ_KEYS | OUT_OF_SCOPE_KEYS == set(S.SETTINGS_BY_KEY)
    assert len(OUT_OF_SCOPE_KEYS) == 51


@pytest.mark.parametrize("key", sorted(OUT_OF_SCOPE_KEYS))
def test_out_of_scope_keys_are_not_in_config(key, base_cfg):
    with pytest.raises(NotInConfig):
        effective(base_cfg, key)


def test_create_config_from_db_reads_only_known_keys():
    """AST — `config_data.get("X")` 의 X 전부가 알려진 집합 안에 있다. 엔진이 새 키를
    읽기 시작하면 이 테스트가 빨개진다(허용 목록은 레지스트리에서 파생한다)."""
    src = (APP_ROOT / "services" / "cp_sat_basic.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "create_config_from_db"
    )
    read_keys: set[str] = set()
    for node in ast.walk(target):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "config_data"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            read_keys.add(node.args[0].value)

    allowed = set(KEY_TABLE_BY_KEY) | REQ_KEYS
    for row in KEY_TABLE:
        allowed |= set(S.SETTINGS_BY_KEY[row.key].aliases)
    allowed |= {c.key for c in S.CODE_CONSTS}
    allowed |= {k for k, _ in S.RUNTIME_DATA_NOT_SETTINGS}
    unknown = read_keys - allowed
    assert not unknown, f"create_config_from_db 가 레지스트리 밖 키를 읽는다: {sorted(unknown)}"


#: `config_data` 를 인자로 받아도 되는 헬퍼 — 이 밖으로 위임하면 AST 가드가 못 본다.
_ALLOWED_CONFIG_DATA_HELPERS: frozenset[str] = frozenset({"resolve_effective_off_days"})


def _variable_key_get_lines(target: ast.AST) -> list[int]:
    """`config_data.get(<상수 아님>)` — 변수 키 경유는 기존 AST 가드의 사각지대다."""
    lines = []
    for node in ast.walk(target):
        is_get = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "config_data"
        )
        if not is_get:
            continue
        arg = node.args[0] if node.args else None
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            lines.append(node.lineno)
    return lines


def _config_data_subscript_lines(target: ast.AST) -> list[int]:
    """`config_data[...]` — `.get` 만 보는 기존 AST 가드가 놓치는 또 다른 경로."""
    return [
        node.lineno
        for node in ast.walk(target)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "config_data"
    ]


def _config_data_unknown_helper_lines(target: ast.AST) -> list[int]:
    """`config_data` 를 허용 목록 밖 함수로 위임 — 위임된 쪽의 읽기는 이 파일 AST 에 안 잡힌다."""
    lines = []
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        passed = any(isinstance(a, ast.Name) and a.id == "config_data" for a in node.args)
        if not passed:
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        if name not in _ALLOWED_CONFIG_DATA_HELPERS:
            lines.append(node.lineno)
    return lines


def test_create_config_from_db_config_data_access_is_fully_scanned():
    """AST 가드 확장 — 상수 `.get` 만 보던 기존 가드의 사각지대 3곳을 추가로 막는다
    (리뷰 반영, review/probe_ast_blindspot.py: 변수 키·subscript·헬퍼 위임은 unknown=[]
    로 전부 샜다)."""
    src = (APP_ROOT / "services" / "cp_sat_basic.py").read_text(encoding="utf-8")
    target = next(
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.FunctionDef) and node.name == "create_config_from_db"
    )
    bad = (
        _variable_key_get_lines(target)
        + _config_data_subscript_lines(target)
        + _config_data_unknown_helper_lines(target)
    )
    assert bad == [], f"config_data 접근 중 AST 가드 밖 경로 — 줄 {sorted(bad)}"


def test_alias_index_has_no_duplicate_owners():
    owners: dict[str, list[str]] = {}
    for spec in S.SETTINGS:
        for alias in spec.aliases:
            owners.setdefault(alias, []).append(spec.key)
    dup = {alias: ks for alias, ks in owners.items() if len(ks) > 1}
    assert dup == {}


# ── §4.5 레지스트리 교차 단언 ──────────────────────────────────────────────

#: `current_default` 와 '부재' 결과가 실제로 어긋나는 키(값 영역 차이·기호 기본값).
_DEFAULT_CROSS_SKIP = frozenset({"three_seq_nig", "soft_max_consecutive_work_days"})

#: None 결과의 자동 토큰(`f"None→{v!r}"`/`"TypeError"`)이 `null_behavior` 서술과 다른 4키 —
#: 서술 형식이 다를 뿐, 전부 레지스트리 원문의 부분 문자열이다(§4.5).
_NULL_TOKEN_OVERRIDE: dict[str, str] = {
    "banned_day_after_eve": "None→cfg.banned_day_after_eve=None",
    "off_days": "cfg.standard_personal_off_days=None",
    "shift_preference_weights": "None 이면 None",
    "soft_max_consecutive_work_days": "None·0→max_conseq_work",
}


def _default_mismatches() -> set[str]:
    mismatches = set()
    for row in KEY_TABLE:
        spec = S.SETTINGS_BY_KEY[row.key]
        try:
            default_val = ast.literal_eval(spec.current_default)
        except Exception:  # noqa: BLE001 — 기호 기본값(파싱 불가)도 '비교 불가'로 취급
            mismatches.add(row.key)
            continue
        if default_val != row.absent:
            mismatches.add(row.key)
    return mismatches


def test_current_default_cross_check_skip_set_is_exact():
    assert _default_mismatches() == _DEFAULT_CROSS_SKIP


def _none_token(row: Row) -> str:
    return "TypeError" if isinstance(row.none, Raises) else f"None→{row.none!r}"


def _token_present(text: str, token: str) -> bool:
    """`token` 이 `text` 안에 '다른 토큰의 접두사'가 아니라 독립 토큰으로 있는지(리뷰 반영
    — 단순 `in` 부분 문자열 비교는 우연 접두 일치를 통과시킬 수 있다, review/probe_token_prefix.py)."""
    for m in re.finditer(re.escape(token), text):
        end = m.end()
        if end == len(text) or not (text[end].isalnum() or text[end] in "._"):
            return True
    return False


def test_null_behavior_cross_check_override_set_is_exact():
    needs_override = {
        row.key for row in KEY_TABLE if not _token_present(S.SETTINGS_BY_KEY[row.key].null_behavior, _none_token(row))
    }
    assert needs_override == set(_NULL_TOKEN_OVERRIDE)
    for key, override_substr in _NULL_TOKEN_OVERRIDE.items():
        assert override_substr in S.SETTINGS_BY_KEY[key].null_behavior, key
