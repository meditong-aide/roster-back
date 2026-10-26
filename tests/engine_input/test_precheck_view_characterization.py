"""EI-10b 특성화 테스트 — precheck 입력(등급·주말휴무·투영·팀)을 단계 A view 로 바꾼 뒤의
동작을 고정한다(`EI10AB_SPEC.md` §4.2, 이번 차례는 precheck(10b)만).

골든 하네스(`harness.run_with_output` · `scenario_defs.SCENARIOS`)를 그대로 쓴다. 기존
시나리오·골든(`golden/*.json`)은 건드리지 않는다 — 이 파일은 시나리오에 몽키패치를
덧붙인 **사본**(`dataclasses.replace`)만 실행한다.

여기서 고정하는 것은 골든이 아직 못 가리는 자리다(§1.5 실측 — "팀을 건너뛰는 분기를 겪는
골든은 0"):
  팀-건너뛰기 — `team_min_by_team` 이 이미 있으면 팀 조회(`teams`) 자체가 안 나간다.
  팀 실패     — 팀 조회·빌드 실패는 지금과 같은 문구로 로그만 남기고 삼킨다.
  주말 실패   — 주말휴무 조회 실패는 로그 없이 `set()`(전원 `is_weekend_off=False`, C2).
  config 정체성 — `precheck_config` 는 `config_dict` 의 얕은 사본(최상위는 다른 객체,
    중첩 값은 같은 객체)이다(C7).

★ 스파이는 **버전에 묶이지 않는 자리**(전환 전·후 모두 존재)만 쓴다 — `rcs.to_precheck_input`
  처럼 전환 후에만 있는 심볼을 몽키패치하면 `base_before`(전환 전 코드) 사본에서
  `AttributeError` 로 깨져, "전환 전 코드에서 먼저 통과"(§4.2 지시)를 어긴다(리뷰 지적
  MEDIUM — 수정 전 버전으로 `base_before` 사본 재현: 3 failed, 1 passed).
"""
from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable

import pytest

from .harness import Scenario, run_with_output
from .scenario_defs import SCENARIOS


def _with_patches(scenario: Scenario, *extra_patches: Callable) -> Scenario:
    """기존 시나리오를 바꾸지 않고 패치만 덧붙인 사본을 만든다(frozen dataclass)."""
    return dataclasses.replace(scenario, patches=scenario.patches + tuple(extra_patches))


def _with_call_kwargs(scenario: Scenario, **extra_call_kwargs) -> Scenario:
    """`call_kwargs`(= 라우터가 넘기는 추가 인자)만 덧붙인 사본."""
    return dataclasses.replace(
        scenario, call_kwargs={**scenario.call_kwargs, **extra_call_kwargs})


def _tables_before_mark(result, mark: str) -> list[str]:
    """`mark`(예: "L0")가 찍히기 **전까지**의 쿼리열."""
    return result.queries["tables"][: result.queries["marks"][mark]]


# ── 정체성 스파이 — rcs.to_precheck_input(전환 후 전용) 대신 양쪽에 다 있는 자리 ──────


def _spy_precheck_and_engine_configs(captured: dict) -> Callable[[pytest.MonkeyPatch], None]:
    """precheck 가 `run_runtime_precheck` 에 실제로 넘긴 `config_dict`(=precheck_config)와
    첫 시도 엔진 호출(`rcs._run_cp_sat_basic`)의 `config_override`(= precheck 가 시작한
    바로 그 `config_dict` 객체, rcs :6794 원문)를 남긴다.

    둘 다 전환 전·후 공통으로 있는 이름이라(`services.precheck.run_runtime_precheck`·
    `rcs._run_cp_sat_basic`) `base_before` 사본에서도 그대로 먹는다."""

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.precheck as precheck
        import services.roster_create_service as rcs

        original_precheck = precheck.run_runtime_precheck
        original_engine = rcs._run_cp_sat_basic

        def spy_precheck(**kwargs):
            captured["precheck_config"] = kwargs["config_dict"]
            captured["precheck_fixed_cells"] = kwargs.get("fixed_cells")
            return original_precheck(**kwargs)

        def spy_engine(*args, **kwargs):
            if "engine_config" not in captured:
                captured["engine_config"] = kwargs.get("config_override")
                captured["engine_fixed_cells"] = kwargs.get("fixed_cells")
            return original_engine(*args, **kwargs)

        mp.setattr(precheck, "run_runtime_precheck", spy_precheck)
        mp.setattr(rcs, "_run_cp_sat_basic", spy_engine)

    return patch


# ── 팀-건너뛰기 — team_min_by_team 이 이미 있으면 팀 조회가 안 나간다 ────────


def _count_inject_team_min_builds(counter: list) -> Callable[[pytest.MonkeyPatch], None]:
    """`rcs.build_team_min_by_team` 호출 중 precheck 팀 주입(`_inject_team_min`)에서 온 것만 센다(원본은 그대로 호출)."""

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.roster_create_service as rcs

        original = rcs.build_team_min_by_team

        def spy(*args, **kwargs):
            if sys._getframe(1).f_code.co_name == "_inject_team_min":
                counter.append(1)
            return original(*args, **kwargs)

        mp.setattr(rcs, "build_team_min_by_team", spy)

    return patch


def test_team_min_by_team_already_present_skips_team_query(engine, engine_input_tables):
    """S4(팀 3개·멤버 있음) — override 로 `team_min_by_team` 을 미리 넣으면 precheck 팀 주입(`_inject_team_min`)이 팀 값을 **다시 짓지
    않는다**(팀 구성 호출 0 · 대조군은 1). 값은 override 그대로 L0 config 에 실린다.

    EI-10g3 — 팀 조회(`teams`) 자체는 단계 B 빌더가 precheck 앞에서 한 번 읽는다(캐시 getter — precheck 는 다시 조회하지 않는다). 그래서
    옛 단언 'L0 앞 구간에 `select:teams` 없음' 은 거짓이 됐고, 건너뛰기는 팀 구성 호출 수로 본다."""
    captured: dict = {}
    builds: list = []
    override = {"team_min_by_team": {"1": {"D": 1, "E": 1, "N": 1}}}
    scenario = _with_patches(
        _with_call_kwargs(SCENARIOS["S4"], config_override=override),
        _spy_precheck_and_engine_configs(captured), _count_inject_team_min_builds(builds),
    )
    run = run_with_output(engine, scenario)
    result = run.result

    assert result.outcome == "golden_stop", (result.outcome, result.error, run.stdout[-2000:])
    before_l0 = _tables_before_mark(result, "L0")
    assert before_l0.count("select:teams") == 1, before_l0   # 빌더가 한 번 — precheck 주입은 재조회하지 않는다
    assert builds == []
    assert captured["precheck_config"]["team_min_by_team"] == override["team_min_by_team"]
    assert captured["precheck_config"]["team_min_by_team"] is captured["engine_config"][
        "team_min_by_team"], "이미 있으면 재구성 없이 같은 객체를 그대로 들고 있어야 한다"


def test_team_min_by_team_absent_queries_teams_before_l0(engine, engine_input_tables):
    """대조군 — override 없는 보통 S4 는 팀 조회가 L0 **전**에 나가고(1건 · 빌더), precheck 팀 주입이 팀 값을 짓는다(호출 1)."""
    builds: list = []
    run = run_with_output(engine, _with_patches(SCENARIOS["S4"], _count_inject_team_min_builds(builds)))
    result = run.result
    assert result.outcome == "golden_stop", (result.outcome, result.error)
    before_l0 = _tables_before_mark(result, "L0")
    assert before_l0.count("select:teams") == 1, before_l0
    assert builds == [1]


# ── 팀 실패 — 조회·빌드 어느 쪽이 실패해도 로그만 남기고 삼킨다 ─────────────


def _raise_on_build_team_min_once(mp: pytest.MonkeyPatch) -> None:
    import services.roster_create_service as rcs

    state = {"called": False}
    original = rcs.build_team_min_by_team

    def _raiser(*args, **kwargs):
        # EI-10g3 — 단계 B 빌더가 같은 함수를 precheck 앞에서 먼저 부르므로, precheck 팀 주입(`_inject_team_min`)의 호출만 겨냥한다.
        if not state["called"] and sys._getframe(1).f_code.co_name == "_inject_team_min":
            state["called"] = True
            raise RuntimeError("characterization: team_min 빌드 실패 주입")
        return original(*args, **kwargs)

    mp.setattr(rcs, "build_team_min_by_team", _raiser)


def test_team_build_failure_logged_and_swallowed(engine, engine_input_tables):
    """S4 + `build_team_min_by_team` 첫 호출만 예외 — 지금과 같은 문구로 로그만 남기고
    `team_min_by_team` 없이 L0 까지 그대로 진행한다(차단 없음)."""
    captured: dict = {}
    scenario = _with_patches(
        SCENARIOS["S4"], _raise_on_build_team_min_once, _spy_precheck_and_engine_configs(captured))
    run = run_with_output(engine, scenario)
    result = run.result

    assert "[Precheck] team_min 로딩 실패(무시): characterization: team_min 빌드 실패 주입" in run.stdout
    assert result.outcome == "golden_stop", (result.outcome, result.error)
    assert result.path == "first_attempt"
    assert "team_min_by_team" not in captured["precheck_config"]
    before_l0 = _tables_before_mark(result, "L0")
    assert "select:teams" in before_l0, "팀 조회(실패 전 쿼리)는 나갔어야 한다"


# ── 주말 실패 — 전체 파이프라인으로 재현(C2) ────────────────────────────────


def _caller_name(depth: int = 2) -> str:
    """`depth` 단계 위 호출자의 함수 이름 — `sys._getframe` 는 패치 wrapper 자신(depth=1) 을
    건너뛰려고 기본 2."""
    return sys._getframe(depth).f_code.co_name


#: precheck 가 주말휴무를 읽는 자리의 호출자 — 전환 전은 `_generate_roster_service_impl`
#: 본체 안(인라인), 전환 후는 단계 A 클로저 `_load_weekend_ids`(builder.py). 이 둘로
#: 좁혀야 그 **앞**(rcs:1724, `_compute_weekly_off_day_indices_for_month` 안의 주휴
#: 보충 — try 로 안 감싸져 있다)의 호출을 건드리지 않는다. 호출자 구분 없이 "첫 호출만
#: 예외"로 주입하면 그 앞 호출이 먼저 터져 서비스 전체가 미분류 예외로 끝난다(리뷰
#: 지적 MEDIUM — STATUS 의 "rcs:1724 가 먼저 소비해 재현 불가" 판단은 틀렸다. 호출자를
#: 가리면 재현된다).
_WEEKEND_PRECHECK_CALLERS: tuple[tuple[str, str | None], ...] = (
    ("_generate_roster_service_impl", None),
    ("_load_weekend_ids", "builder.py"),
)


def _raise_weekend_lookup_once(callers: tuple[tuple[str, str | None], ...]):
    """`weekend_off_ids_asof` 를 감싸, 호출자 함수 이름(+파일 조각)이 `callers` 중 하나와
    맞을 때만(그리고 그 자리에서 처음 한 번만) 예외를 낸다. 맞지 않는 호출(예: rcs:1724)은
    원본 그대로 통과시킨다."""

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.nurse_period_resolver as period_resolver

        original = period_resolver.weekend_off_ids_asof
        state = {"done": False}

        def wrapper(*args, **kwargs):
            frame = sys._getframe(1)
            name, filename = frame.f_code.co_name, frame.f_code.co_filename
            hit = any(name == caller_name and (frag is None or frag in filename)
                     for caller_name, frag in callers)
            if hit and not state["done"]:
                state["done"] = True
                raise RuntimeError("characterization: weekend 조회 실패 주입")
            return original(*args, **kwargs)

        mp.setattr(period_resolver, "weekend_off_ids_asof", wrapper)

    return patch


def _weekend_queries_before_l0(result) -> int:
    """L0 앞 구간의 `nurse_weekendoff_period` 조회 수 — 주휴 보충(rcs:1724) 몫과 단계 A getter 몫이 섞여 있어 **차이**로만 비교한다."""
    return _tables_before_mark(result, "L0").count("select:nurse_weekendoff_period")


def test_weekend_lookup_failure_removes_exactly_one_query_and_clears_flags(
    engine, engine_input_tables,
):
    """S5(골든 L0 에 `is_weekend_off=True` 2건 실측) 대조 — 단계 A 의 주말휴무 getter 만 첫 호출 예외 → 로그 없이 `set()` 으로 떨어져
    전원 `False`, L0 앞 구간의 주말휴무 조회(`nurse_weekendoff_period`) **1건만** 사라진다(C2).

    EI-10g3 — getter 를 처음 여는 것은 이제 precheck 앞의 단계 B 빌더다(실패는 `Lazy` 가 캐시해 precheck 가 같은 실패를 받는다). 그래서
    옛 '등급 조회 뒤 구간' 비교 대신 L0 앞 전체의 조회 수 차이(정확히 1)로 본다.

    호출자 구분 없이 주입하면 그 앞(rcs:1724)이 먼저 터져 서비스 전체가 미분류 예외로 끝난다 — 그래서 `_WEEKEND_PRECHECK_CALLERS` 로 좁힌다."""
    base_run = run_with_output(engine, SCENARIOS["S5"])
    base_result = base_run.result
    assert base_result.outcome == "golden_stop", (base_result.outcome, base_result.error)
    base_count = _weekend_queries_before_l0(base_result)
    assert base_count >= 1
    base_nurses = base_result.layers["L0"][0]["nurses_dict"]
    assert any(nd["is_weekend_off"] for nd in base_nurses), (
        "대조군(주입 없음)은 is_weekend_off=True 가 있어야 한다(골든 L0 실측 2건)")

    scenario = _with_patches(SCENARIOS["S5"], _raise_weekend_lookup_once(_WEEKEND_PRECHECK_CALLERS))
    run = run_with_output(engine, scenario)
    result = run.result
    assert result.outcome == "golden_stop", (result.outcome, result.error, run.stdout[-2000:])
    assert _weekend_queries_before_l0(result) == base_count - 1, (base_count, _weekend_queries_before_l0(result))

    nurses_dict = result.layers["L0"][0]["nurses_dict"]
    assert all(nd["is_weekend_off"] is False for nd in nurses_dict)


# ── config 정체성 — precheck_config 는 config_dict 의 얕은 사본(C7) ─────────


def test_precheck_config_is_shallow_copy_sharing_nested_identity(engine, engine_input_tables):
    """precheck_config 는 `config_dict` 와 **다른 최상위 객체**(`dict(config_dict)`)이지만,
    중첩 값은 같은 객체를 공유한다(얕은 사본 — §0-2, R2, C7). nested 값은 어떤 기존 로직도
    해석하지 않는 무해한 키로 주입해(의미 있는 키는 다른 블록의 판정까지 바꿔 버린다 —
    예: `preceptee_period_by_nurse_id` 는 precheck 를 막아 ladder 로 빠진다) 순수하게
    '얕은 사본' 만 확인한다. `fixed_cells` 는 precheck·첫 시도 둘 다 같은 객체를 받는다."""
    captured: dict = {}
    nested = {"foo": "bar"}
    scenario = _with_patches(
        _with_call_kwargs(SCENARIOS["S1"], config_override={"_ei10b_inert_marker": nested}),
        _spy_precheck_and_engine_configs(captured),
    )
    run = run_with_output(engine, scenario)
    assert run.result.outcome == "golden_stop", (run.result.outcome, run.result.error)

    engine_config = captured["engine_config"]
    precheck_config = captured["precheck_config"]
    assert precheck_config is not engine_config, "최상위는 dict(config_dict) 로 다른 객체여야 한다"
    assert precheck_config["_ei10b_inert_marker"] is engine_config["_ei10b_inert_marker"], (
        "중첩 값은 같은 객체를 공유해야 한다(얕은 사본)")
    if captured.get("engine_fixed_cells"):
        assert captured["precheck_fixed_cells"] is captured["engine_fixed_cells"], (
            "fixed_cells 는 precheck·첫 시도가 같은 객체를 받아야 한다(C7)")

    # ★ `layers["L0"]` 는 `canonical()` 로 키가 정렬돼 삽입 순서가 안 남는다(canonical.py
    #   'dict 키 정렬') — 그래서 투영의 키 **순서**(ONTOLOGY_NURSE_FIELDS + db_id·name·
    #   is_weekend_off)는 골든 경로가 아니라 `test_views.py` 의 단위 테스트로 고정한다.


def test_precheck_config_is_shallow_copy_with_treatment(engine, engine_input_tables):
    """C7 S8 — 처방(온톨로지 treatment)이 걸린 경로에서도 얕은 사본 규칙은 같다(치료
    경로가 precheck_config 를 깊은 사본·실물 재사용 등으로 바꿔 버리지 않는지 확인)."""
    captured: dict = {}
    scenario = _with_patches(SCENARIOS["S8"], _spy_precheck_and_engine_configs(captured))
    run = run_with_output(engine, scenario)
    assert run.result.outcome == "golden_stop", (run.result.outcome, run.result.error)
    assert captured["precheck_config"] is not captured["engine_config"]
