"""C8' — precheck 와 엔진이 하드락 8종·인접 키에 같은 값을 본다(설계서 §2.6 C8' · EI-10c · 결정 D8).

엔진 값은 **엔진 `create_config_from_db` 가 만든 `NurseRosterConfig` 속성**(`vars(...)`)에서 이 파일 안에 따로 적은 속성
이름 표(`ENGINE_ATTR`)로 읽는다 — EI-8 `effective_hard_lock`·레지스트리 키 대응을 거치지 않는다(리뷰 지적: 같은 함수가 양쪽
기준이면 키 대응과 읽기 배관만 보는 동어반복이라, `ban_e_to_d` ↔ `ban_n_to_e` 를 뒤바꿔도 빨갛지 않았다). precheck 값은
생성 경로가 쓰는 `prepare_precheck_input` 결과에서 **검사기가 실제로 읽는 형태**로 꺼낸다 — 개인 시퀀스 DP 는
`precheck_sequence_params`, 용량 검사는 `roster_config` 의 `max_conseq_work`·`max_nig_per_month`·`use_mid` 를 읽는다.
같은 값이 나와야 한다. 실제 엔진 모델(lex stage1)의 가능/불가능 판정과의 대조는
`tests/engine_input/test_precheck_vs_engine_stage1.py` 가 맡는다.

비교에서 의도적으로 빠지는 것(레지스트리 하드락·인접·영향 키 중 precheck 가 값으로 쓰지 않는 13키)은
`hard_lock_view.NOT_CONSUMED` 에 이유와 함께 적혀 있고, 이 파일이 '전부 분류됐는가'를 지킨다.
특히 `off_days` 는 엔진이 off_first=False 인 병동의 N 전담·주말휴무가 아닌 간호사에게만 OFF 하드 하한(min_off — 휴가 제외·구조
OFF 계산)을 거는데 precheck 는 그 계산을 재현하지 않아 하드 용량에서 일부러 뺀다(느슨한 쪽, 놓칠 수는 있어도 오차단은 없다).
같은 값을 요구하면 오히려 오차단이다.

알려진 차이(값은 같지만 쓰는 방식이 다른 곳 — 레지스트리상 비정상 값이라 이번 범위 밖):
  - `max_conseq_work` ≤ 0 은 엔진에선 창 크기가 0 이하라 모델이 비거나 불가능해지지만, precheck 용량
    검사는 상한 없음으로 본다(느슨한 쪽 — `_working_capacity`·DP 모두 1 이상일 때만 상한으로 쓴다).
"""

from __future__ import annotations

import contextlib
import copy
import io
import itertools

import pytest
from services.cp_sat_basic import CPSATBasicEngine
from services.engine_contract.effective import HARD_LOCK_CFG_KEYS, effective_hard_lock
from services.engine_contract.settings import ADJACENT, AFFECTS, SETTINGS, HardLock
from services.precheck.hard_lock_view import (
    ENGINE_TO_PRECHECK_KEYS,
    NOT_CONSUMED,
    attach_engine_hard_lock,
)
from services.precheck.runtime_bridge import build_precheck_input, prepare_precheck_input
from services.precheck.team_grade_precheck import precheck_sequence_params


class _Absent:
    """'키가 없다'를 값으로 쓰기 위한 표지(None 과 구별한다)."""

    def __repr__(self) -> str:
        return "absent"


_ABSENT = _Absent()
BASE = {
    "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "max_conseq_work": 5,
    "max_nig_per_month": 15, "banned_day_after_eve": True, "three_seq_nig": True,
    "two_offs_after_three_nig": True, "two_offs_after_two_nig": False, "not_one_night": False,
    "use_mid": False, "off_days": 8, "shift_priority": 0.7,
}
VALUES = (_ABSENT, None, 0, -1, True, False, "false", 2, 3, 4, 6, 16)
BOOL_KEYS = frozenset({
    "banned_day_after_eve", "two_offs_after_three_nig", "two_offs_after_two_nig", "not_one_night",
    "use_mid", "ban_e_to_d", "ban_n_to_d", "ban_n_to_e",
})
#: 이 단계 전에는 precheck 가 받지 못하던 키 — 엔진이 못 읽는 설정이면 하나도 실리면 안 된다.
NEWLY_CARRIED = frozenset(ENGINE_TO_PRECHECK_KEYS.values()) - {"max_conseq_work", "max_nig_per_month", "use_mid"}
ENGINE_FAILURES = (TypeError, ValueError, OverflowError)


def _with(key, value):
    cfg = dict(BASE)
    if value is _ABSENT:
        cfg.pop(key, None)
    else:
        cfg[key] = value
    return cfg


def _roster_config(cfg) -> dict:
    """생성 경로가 쓰는 입구(`prepare_precheck_input`)가 만든 precheck `roster_config`."""
    inp = prepare_precheck_input(
        nurses_dict=[{"nurse_id": "n0"}], config_dict=cfg, grade_config=None,
        fixed_cells=None, year=2026, month=11)
    return inp.roster_config


def _precheck_view(roster_config) -> dict:
    """검사기가 실제로 읽는 형태의 하드락 11키 값."""
    seq = precheck_sequence_params(roster_config)
    return {
        "max_conseq_work": roster_config.get("max_conseq_work"),
        "banned_day_after_eve": roster_config.get("banned_day_after_eve"),
        "three_seq_nig": seq["max_consecutive_nights"],
        "two_offs_after_three_nig": seq["two_offs_after_three_nig"],
        "two_offs_after_two_nig": seq["two_offs_after_two_nig"],
        "max_nig_per_month": roster_config.get("max_nig_per_month"),
        "not_one_night": seq["not_one_night"],
        "use_mid": roster_config.get("use_mid"),
        "ban_e_to_d": seq["ban_e_to_d"],
        "ban_n_to_d": seq["ban_n_to_d"],
        "ban_n_to_e": seq["ban_n_to_e"],
    }


#: 엔진 모델이 실제로 읽는 `NurseRosterConfig` 속성(하드락 키 → 속성 이름). 레지스트리·`effective` 와 **따로** 적은 표다 —
#: 키 대응이 뒤바뀌거나 별칭 정규화가 잘못 들어가면 이 표와 어긋나 빨개진다.
ENGINE_ATTR = {
    "max_conseq_work": "max_consecutive_work_days",
    "banned_day_after_eve": "banned_day_after_eve",
    "three_seq_nig": "max_consecutive_nights",
    "two_offs_after_three_nig": "two_offs_after_three_nig",
    "two_offs_after_two_nig": "two_offs_after_two_nig",
    "max_nig_per_month": "max_night_shifts_per_month",
    "not_one_night": "not_one_night",
    "use_mid": "use_mid",
    "ban_e_to_d": "ban_e_to_d",
    "ban_n_to_d": "ban_n_to_d",
    "ban_n_to_e": "ban_n_to_e",
}


def _engine_value(cfg) -> dict | None:
    """엔진이 이 설정에서 모델에 거는 값(`create_config_from_db` 결과의 속성). 엔진도 못 읽는 설정이면 None.

    못 읽는 설정은 뒤에서 엔진이 같은 예외로 실패한다. print 는 엔진 해석의 7줄이라 버린다.
    """
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            engine_cfg = CPSATBasicEngine().create_config_from_db(dict(cfg))
    except ENGINE_FAILURES:
        return None
    attrs = vars(engine_cfg)
    return {key: attrs[attr] for key, attr in ENGINE_ATTR.items()}


def _set_env(monkeypatch, env) -> None:
    if env is None:
        monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    else:
        monkeypatch.setenv("AIDE_MAX_CONSEC_WORK", env)


@pytest.mark.parametrize("env", (None, "4"), ids=("env_none", "env_4"))
@pytest.mark.parametrize("value", VALUES, ids=repr)
@pytest.mark.parametrize("key", HARD_LOCK_CFG_KEYS)
def test_c8_precheck_hard_lock_equals_engine(key, value, env, monkeypatch):
    """11키 × 값 12종 × env 2 = 264 조합 — 엔진 모델이 읽는 속성값과 같다. 엔진이 못 읽으면 하나도 싣지 않는다."""
    _set_env(monkeypatch, env)
    cfg = _with(key, value)
    engine = _engine_value(cfg)
    roster_config = _roster_config(cfg)
    if engine is None:
        assert not (NEWLY_CARRIED & set(roster_config)), sorted(NEWLY_CARRIED & set(roster_config))
        return
    view = _precheck_view(roster_config)
    for name in HARD_LOCK_CFG_KEYS:
        if name in BOOL_KEYS:
            assert bool(view[name]) == bool(engine[name]), (name, view[name], engine[name])
        else:
            assert view[name] == engine[name], (name, view[name], engine[name])


def test_c8_engine_unreadable_config_falls_back_to_pre_10c_keys():
    """(가드) `shift_priority=None` 은 엔진 `create_config_from_db` 가 TypeError — 값을 싣지 않는다(10c 이전과 같다)."""
    cfg = _with("shift_priority", None)
    with pytest.raises(TypeError):
        effective_hard_lock(cfg)
    roster_config = _roster_config(cfg)
    assert not (NEWLY_CARRIED & set(roster_config))
    assert attach_engine_hard_lock({}, cfg) is False


def test_c8_every_hard_lock_and_adjacent_key_is_classified():
    """레지스트리의 하드락·인접·영향 키는 전부 '싣는다' 아니면 '쓰지 않는다(이유)' 로 분류돼 있다.

    새 하드락·인접 키가 생기면 분류하기 전까지 빨갛다(A2 와 같은 장치).
    """
    registry = {s.key for s in SETTINGS if isinstance(s.hard_lock, HardLock) or s.hard_lock in (ADJACENT, AFFECTS)}
    assert set(ENGINE_TO_PRECHECK_KEYS) == set(HARD_LOCK_CFG_KEYS)
    assert registry == set(ENGINE_TO_PRECHECK_KEYS) | set(NOT_CONSUMED)
    assert not set(ENGINE_TO_PRECHECK_KEYS) & set(NOT_CONSUMED)
    assert all(reason.startswith("없음:") for reason in NOT_CONSUMED.values())
    # off_days: 엔진은 소프트가 아니라 조건부 하드 하한(min_off)을 건다 — 사유가 그 사실을 적어야 한다(리뷰 MEDIUM).
    # EI-14r3 이후: 하한값은 하드락 값 전달 경로가 아니라 bridge 의 `min_off_required` 로 따로 실린다 — 사유가 그 소비 경로를 적어야 한다.
    assert "min_off" in NOT_CONSUMED["off_days"] and "소프트" not in NOT_CONSUMED["off_days"]
    assert "min_off_required" in NOT_CONSUMED["off_days"] and "_hard_off_floor" in NOT_CONSUMED["off_days"]
    assert "재현하지 않아" not in NOT_CONSUMED["off_days"]


def test_c8_attach_does_not_touch_config_dict_or_nurses():
    """(가드) 엔진 해석을 한 번 더 부르지만 입력 config_dict 는 바뀌지 않는다(중첩 값 포함)."""
    cfg = _with("max_conseq_work", 5)
    cfg["team_min_by_team"] = {"T1": {"D": 1}}
    before = copy.deepcopy(cfg)
    _roster_config(cfg)
    assert cfg == before


def test_c8_build_precheck_input_alone_carries_no_new_keys():
    """(가드) `build_precheck_input` 은 그대로다 — PoolGraph(ontology_pool)가 이 함수를 직접 부르므로
    하드락 값 연결은 `prepare_precheck_input` 한 곳에만 둔다(거기까지 엔진 출력 7줄을 늘리지 않는다)."""
    inp = build_precheck_input(
        nurses_dict=[{"nurse_id": "n0"}], config_dict=_with("max_conseq_work", 5), grade_config=None,
        fixed_cells=None, year=2026, month=11)
    assert not (NEWLY_CARRIED & set(inp.roster_config))


def test_c8_attach_maps_three_seq_nig_to_integer_cap():
    """(가드) 엔진의 `three_seq_nig` 는 bool 이 아니라 연속 N 상한 정수(3/2) — 이름을 바꿔 정수로 싣는다.

    bool 로 접어 싣으면 `2`(참)가 L=3 으로 읽혀 연속 N 상한이 틀어진다.
    """
    for raw, cap in ((True, 3), (False, 2), (None, 2), (0, 2)):
        roster_config = _roster_config(_with("three_seq_nig", raw))
        assert roster_config["max_consecutive_nights"] == cap
        assert "three_seq_nig" not in roster_config


def test_c8_sequence_params_keep_legacy_defaults_for_direct_callers():
    """(가드) bridge 를 거치지 않는 직접 호출부(라우터 `POST /groups/{id}/roster/precheck`·테스트)는 10c 전과 같은 값을 본다.

    키가 없으면 전이 금지 3종 켬 · 연속 N 상한 2(`three_seq_nig` 참이면 3) · 회복·1N 끔 · 연속근무 상한 없음.
    bridge 가 실은 엔진 값(`ban_e_to_d`·`max_consecutive_nights`)은 옛 키(`banned_day_after_eve`·`three_seq_nig`)보다 이긴다.
    """
    legacy = precheck_sequence_params({})
    assert legacy == {
        "max_consecutive_work": None, "max_consecutive_nights": 2, "ban_n_to_d": True, "ban_e_to_d": True,
        "ban_n_to_e": True, "two_offs_after_two_nig": False, "two_offs_after_three_nig": False,
        "not_one_night": False,
    }
    assert precheck_sequence_params({"three_seq_nig": True})["max_consecutive_nights"] == 3
    assert precheck_sequence_params({"banned_day_after_eve": False})["ban_e_to_d"] is False
    carried = {"three_seq_nig": True, "max_consecutive_nights": 2, "banned_day_after_eve": True, "ban_e_to_d": False}
    params = precheck_sequence_params(carried)
    assert params["max_consecutive_nights"] == 2 and params["ban_e_to_d"] is False
    assert precheck_sequence_params({"max_consecutive_nights": "abc"})["max_consecutive_nights"] == 2
    # 엔진은 `banned_day_after_eve` 가 거짓이면 개별 스위치와 무관하게 전이 금지를 모두 끈다(마스킹이 이긴다) —
    # 옛 단언(개별 `ban_e_to_d=True` 가 마스킹을 이김)은 엔진과 다른 값을 정답으로 고정하고 있었다.
    masked = precheck_sequence_params({"banned_day_after_eve": False, "ban_e_to_d": True, "ban_n_to_d": True, "ban_n_to_e": True})
    assert (masked["ban_e_to_d"], masked["ban_n_to_d"], masked["ban_n_to_e"]) == (False, False, False)


#: 전이 금지 3종을 서로 다르게 둔 조합(8) × 마스킹 스위치(2) — 어느 둘을 뒤바꿔 읽어도 한 조합은 값이 달라진다.
TRANSITION_COMBOS = list(itertools.product((True, False), repeat=3))


@pytest.mark.parametrize("banned_day_after_eve", (True, False))
@pytest.mark.parametrize(("ban_e_to_d", "ban_n_to_d", "ban_n_to_e"), TRANSITION_COMBOS)
def test_c8_transition_ban_combinations_match_the_engine_model(ban_e_to_d, ban_n_to_d, ban_n_to_e, banned_day_after_eve):
    """전이 금지 3종 8조합 × 마스킹 2 — 엔진이 모델에 거는 값(마스킹 포함)과 precheck 가 읽는 값이 조합마다 같다.

    단일 키 행렬(위)은 한 키만 바꾸므로 두 키를 뒤바꾼 변이(예: effective 가 `ban_e_to_d` 와 `ban_n_to_e` 를 바꿔 돌려줌)를
    못 잡는다. 서로 다른 값을 동시에 두면 뒤바뀜이 드러난다.
    """
    cfg = {**BASE, "ban_e_to_d": ban_e_to_d, "ban_n_to_d": ban_n_to_d, "ban_n_to_e": ban_n_to_e,
           "banned_day_after_eve": banned_day_after_eve}
    engine = _engine_value(cfg)
    view = _precheck_view(_roster_config(cfg))
    for name in ("ban_e_to_d", "ban_n_to_d", "ban_n_to_e"):
        assert view[name] == engine[name], (name, view[name], engine[name])
    expected = (ban_e_to_d, ban_n_to_d, ban_n_to_e) if banned_day_after_eve else (False, False, False)
    assert tuple(engine[name] for name in ("ban_e_to_d", "ban_n_to_d", "ban_n_to_e")) == expected


def test_c8_engine_attribute_table_covers_every_hard_lock_key():
    """이 파일의 엔진 속성 표가 하드락 cfg 경유 11키를 빠짐없이, 서로 다른 속성으로 가리킨다(표 자체의 정합)."""
    assert set(ENGINE_ATTR) == set(HARD_LOCK_CFG_KEYS)
    assert len(set(ENGINE_ATTR.values())) == len(ENGINE_ATTR)


@pytest.mark.parametrize("flags", itertools.product((True, False), repeat=4))
def test_c8_direct_call_sequence_params_equal_the_engine_when_keys_are_explicit(flags):
    """bridge 를 거치지 않는 직접 호출부도 키를 명시하면 엔진 모델과 같은 규칙 값을 본다(마스킹·연속 N 상한 포함).

    키가 없을 때의 옛 기본값은 따로 고정돼 있다(`..._keep_legacy_defaults_for_direct_callers`). 여기서는 명시한 키만 비교한다 —
    16조합(three_seq_nig × banned_day_after_eve × ban_e_to_d × ban_n_to_e, ban_n_to_d 는 ban_e_to_d 의 반대, 회복·1N 은 조합으로 순환).
    """
    three_seq, banned, ban_e, ban_n_e = flags
    raw = {"three_seq_nig": three_seq, "banned_day_after_eve": banned, "ban_e_to_d": ban_e, "ban_n_to_d": not ban_e,
           "ban_n_to_e": ban_n_e, "two_offs_after_two_nig": ban_e, "two_offs_after_three_nig": ban_n_e,
           "not_one_night": three_seq}
    engine = _engine_value({**BASE, **raw})
    params = precheck_sequence_params(raw)
    assert params["max_consecutive_nights"] == engine["three_seq_nig"]
    for name in ("ban_e_to_d", "ban_n_to_d", "ban_n_to_e", "two_offs_after_two_nig", "two_offs_after_three_nig", "not_one_night"):
        assert params[name] == bool(engine[name]), (name, params[name], engine[name])


# ── engine_hard_lock 폴백 — 엔진 계약 드리프트가 precheck 를 죽이지 않고, 조용한 폴백과 로그 있는 폴백이 갈린다 ───────────────


def test_engine_hard_lock_silent_fallback_for_the_exceptions_the_engine_raises_too(capsys, monkeypatch):
    """엔진도 같은 예외(TypeError·ValueError·OverflowError)로 못 읽는 설정은 값을 싣지 않고 **로그도 남기지 않는다**.

    골든 S3(`shift_priority=None`)가 이 경로다 — 로그가 '실패'를 포함하면 골든 `swallowed_failures` 가 바뀐다.
    """
    from services.precheck import hard_lock_view

    for error in (TypeError("x"), ValueError("x"), OverflowError("x")):
        def boom(_cfg, _error=error):
            raise _error

        monkeypatch.setattr("services.engine_contract.effective.effective_hard_lock", boom)
        assert hard_lock_view.engine_hard_lock({}) is None
        assert capsys.readouterr().out == ""


@pytest.mark.parametrize("error", [KeyError("no such cfg field"), RuntimeError("drift"), AttributeError("gone")])
def test_engine_hard_lock_logs_one_line_for_unexpected_errors_and_falls_back(error, capsys, monkeypatch):
    """그 밖의 예외(엔진 계약 드리프트 — NotInConfig 등)도 precheck 를 죽이지 않고 값만 안 싣는다 — 그때만 한 줄 로그를 남긴다."""
    from services.precheck import hard_lock_view

    def boom(_cfg):
        raise error

    monkeypatch.setattr("services.engine_contract.effective.effective_hard_lock", boom)
    assert hard_lock_view.engine_hard_lock({}) is None
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"[Precheck] 엔진 하드락 조회 실패(무시 — 10c 이전 값으로 판정): {type(error).__name__}: {error}"]
    roster_config: dict = {}
    assert hard_lock_view.attach_engine_hard_lock(roster_config, {}) is False and roster_config == {}


def test_prepare_precheck_input_survives_engine_contract_drift(capsys, monkeypatch):
    """엔진 계약 드리프트가 나도 생성 경로 입구(`prepare_precheck_input`)는 죽지 않고 10c 이전과 같은 입력을 돌려준다."""
    def boom(_cfg):
        raise KeyError("drift")

    monkeypatch.setattr("services.engine_contract.effective.effective_hard_lock", boom)
    roster_config = _roster_config(_with("max_conseq_work", 5))
    assert not (NEWLY_CARRIED & set(roster_config))
    assert "엔진 하드락 조회 실패" in capsys.readouterr().out
