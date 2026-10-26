"""EI-10a 특성화 테스트 — 나이트 과공급 탐지기 입력을 단계 A view 로 바꾸기 **전** 동작을
고정한다(`EI10AB_SPEC.md` §4.2, 이번 차례는 탐지기(10a)만 — precheck(10b)는 범위 밖).

골든 하네스(`harness.run_with_output` · `scenario_defs.SCENARIOS`)를 그대로 쓴다. 기존
시나리오·골든(`golden/*.json`)은 건드리지 않는다 — 이 파일은 시나리오에 몽키패치를
덧붙인 **사본**(`dataclasses.replace`)만 실행한다.

C4 — 근무코드 조회(`_load_shift_lookup`) 실패 → 탐지기 판정 실패 로그, 차단 없이 진행.
C5 — code2main(`_build_code_to_main_map`) 실패 → 근무코드 조회가 **나가지 않는다**(순서).
C6 — 탐지기 인자의 객체 정체성 = 첫 시도(`_run_cp_sat_basic`) 인자와 같은 객체.
C8 — precheck 기록 실패(솔버로 진행) 뒤에도 나이트 탐지기는 단계 A 를 그대로 써서 실행된다.
"""
from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable

import pytest

from .harness import Scenario, run_with_output
from .scenario_defs import SCENARIOS
from .test_presolve_block_characterization import PRECHECK_BLOCK_SCENARIO


def _with_patches(scenario: Scenario, *extra_patches: Callable) -> Scenario:
    """기존 시나리오를 바꾸지 않고 패치만 덧붙인 사본을 만든다(frozen dataclass)."""
    return dataclasses.replace(scenario, patches=scenario.patches + tuple(extra_patches))


def _shift_query_count(result) -> int:
    """전체 쿼리열에서 `select:shifts`(+조인) 출현 수 — `_run_cp_sat_basic` 내부도 근무코드를
    여러 자리에서 조회하므로, 윈도만으로는 '나이트 블록 몫 1건'을 가려낼 수 없다. 그래서
    주입 전/후 **총수의 차이**로 '나이트 블록의 조회 1건이 빠졌는가' 를 본다."""
    return sum(1 for t in result.queries["tables"] if t == "select:shifts")


# ── C4 — 근무코드 조회 실패(shift_lookup) ─────────────────────────────────


def _raise_on_shift_lookup_once(mp: pytest.MonkeyPatch) -> None:
    """`rcs._load_shift_lookup` 첫 호출만 예외 — 나이트 블록의 `stage_a.shift_lookup` getter
    가 쓰는 바로 그 함수(builder.py `_load_shift_lookup` 클로저가 지연 import 한다)."""
    import services.roster_create_service as rcs

    state = {"called": False}
    original = rcs._load_shift_lookup

    def _raiser(*args, **kwargs):
        if not state["called"]:
            state["called"] = True
            raise RuntimeError("characterization: shift_lookup 실패 주입")
        return original(*args, **kwargs)

    mp.setattr(rcs, "_load_shift_lookup", _raiser)


def test_night_block_shift_lookup_failure_proceeds_without_blocking(engine, engine_input_tables):
    """S1(고정근무자 없음·비차단 — 베이스라인·주입 둘 다 L1 까지 같은 깊이로 간다) + 근무코드
    조회 실패 → 판정 실패 로그만 남기고 차단 없이 솔버(L1)까지 간다. `L0p`(탐지기 호출
    표지)가 없고, 나이트 블록 몫의 `shifts` 조회 1건이 통째로 빠진다(전체 횟수가 베이스라인
    보다 정확히 1 적다 — `_run_cp_sat_basic` 내부도 근무코드를 여러 자리에서 조회해 '0건'은
    기대할 수 없다, exp3 로 확인한 사실)."""
    baseline = run_with_output(engine, SCENARIOS["S1"])
    scenario = _with_patches(SCENARIOS["S1"], _raise_on_shift_lookup_once)
    run = run_with_output(engine, scenario)
    result = run.result

    assert "characterization: shift_lookup 실패 주입" in run.stdout
    assert "[NightOversupply] 판정 실패(무시하고 진행)" in run.stdout
    assert "L0p" not in result.queries["marks"], "조립 실패 — 탐지기가 돌면 안 된다"
    assert result.outcome == "golden_stop", (result.outcome, result.error)
    assert result.path == "first_attempt"
    # EI-13a-2 — 나이트 블록 몫의 shifts 조회 1건은 빠지지만, 단계 A 근무코드 getter 가 실패를 캐시해 빌더가 `AttemptSource` 를 못 지으므로
    #   `_run_cp_sat_basic` 이 지금 경로로 근무코드를 다시 읽는다(A7) — 정상 런은 이 조회를 빌더의 캐시 getter 에서 얻어 총수가 같다.
    assert _shift_query_count(result) == _shift_query_count(baseline.result), (
        "근무코드 조회 실패 — 나이트 블록 몫 1건이 빠지는 대신 시도가 근무코드를 다시 읽는다(빌더 폴백)")


# ── C5 — code2main 실패(근무코드 조회가 먼저 막힌다) ──────────────────────


def _called_from(*names: str, depth: int = 10) -> bool:
    """호출 스택에 `names` 중 하나의 함수가 있는가 — EI-10g3: 단계 B 빌더가 같은 하위 함수를 precheck 앞에서 먼저 부르므로
    '첫 호출만 예외' 대신 시험이 겨냥한 소비자(나이트 탐지기의 `presolve_view`)의 호출만 겨냥한다."""
    frame = sys._getframe(2)
    for _ in range(depth):
        if frame is None:
            return False
        if frame.f_code.co_name in names:
            return True
        frame = frame.f_back
    return False


def _raise_on_code2main_once(mp: pytest.MonkeyPatch) -> None:
    """`rcs._build_code_to_main_map` 첫 호출만 예외 — code2main 이 근무코드 조회보다
    먼저이므로(§2.2 순서), 이게 실패하면 `shift_lookup` 조회 자체가 안 나간다."""
    import services.roster_create_service as rcs

    state = {"called": False}
    original = rcs._build_code_to_main_map

    def _raiser(*args, **kwargs):
        if not state["called"] and _called_from("presolve_view"):
            state["called"] = True
            raise RuntimeError("characterization: code2main 실패 주입")
        return original(*args, **kwargs)

    mp.setattr(rcs, "_build_code_to_main_map", _raiser)


def test_night_block_code2main_failure_skips_shift_query(engine, engine_input_tables):
    """S1(고정근무자 없음, 비차단) + code2main 실패 → 같은 판정 실패 로그 · `L0p` 없음 ·
    code2main 이 근무코드 조회보다 먼저라 그 조회 자체가 안 나간다(베이스라인보다 shifts
    조회가 정확히 1 적다 — C4 와 같은 비교 방식)."""
    baseline = run_with_output(engine, SCENARIOS["S1"])
    scenario = _with_patches(SCENARIOS["S1"], _raise_on_code2main_once)
    run = run_with_output(engine, scenario)
    result = run.result

    assert "characterization: code2main 실패 주입" in run.stdout
    assert "[NightOversupply] 판정 실패(무시하고 진행)" in run.stdout
    assert "L0p" not in result.queries["marks"]
    assert result.outcome == "golden_stop", (result.outcome, result.error)
    assert result.path == "first_attempt"
    # EI-13a-2 — 나이트 블록은 근무코드 조회를 안 하지만(code2main 이 먼저 실패) 단계 A getter 는 읽히지 않은 채라, 첫 시도 직전 빌더가
    #   그 자리에서 한 번 읽는다(정상 런은 나이트 블록이 읽고 빌더가 캐시를 쓴다) — 총수는 정상 런과 같다.
    assert _shift_query_count(result) == _shift_query_count(baseline.result), (
        "code2main 이 먼저 실패해도 근무코드는 빌더가 한 번 읽는다 — 총수는 정상 런과 같다")


# ── C6 — 탐지기 인자 객체 정체성(= 첫 시도 인자와 같은 객체) ──────────────


def _spy_detector_raw_args(captured: dict) -> Callable[[pytest.MonkeyPatch], None]:
    """`detect_night_forced_oversupply` 가 받는 `nurses`·`config` 의 **원본 객체**를 남긴다."""

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.ontology_graph.lagrangian as lagrangian

        original = lagrangian.detect_night_forced_oversupply

        def spy(nurses, config, *args, **kwargs):
            captured["detector_nurses"] = nurses
            captured["detector_config"] = config
            return original(nurses, config, *args, **kwargs)

        mp.setattr(lagrangian, "detect_night_forced_oversupply", spy)

    return patch


def _spy_first_attempt_args(captured: dict) -> Callable[[pytest.MonkeyPatch], None]:
    """`_run_cp_sat_basic` 첫 시도 호출의 `nurses_for_engine`·`config_override` 원본 객체를 남긴다."""

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.roster_create_service as rcs

        original = rcs._run_cp_sat_basic

        def spy(*args, **kwargs):
            captured["first_attempt_nurses"] = args[2] if len(args) > 2 else kwargs.get(
                "nurses_for_engine")
            captured["first_attempt_config"] = kwargs.get("config_override")
            return original(*args, **kwargs)

        mp.setattr(rcs, "_run_cp_sat_basic", spy)

    return patch


def test_night_block_detector_args_are_same_objects_as_first_attempt(engine, engine_input_tables):
    """S5(고정근무자 있음 — 차감 적용) — 탐지기가 받는 간호사 리스트·config 는 첫 시도
    (`_run_cp_sat_basic`)가 받는 바로 그 객체와 **동일**해야 한다(`is`, EI10AB_SPEC §2.1).
    차감이 `config_dict` 에 제자리 대입을 하므로, 탐지기 config 는 '차감 뒤' 값이어야 한다."""
    captured: dict = {}
    scenario = _with_patches(
        SCENARIOS["S5"], _spy_detector_raw_args(captured), _spy_first_attempt_args(captured))
    run = run_with_output(engine, scenario)
    result = run.result

    assert result.outcome == "golden_stop", (result.outcome, result.error, run.stdout[-2000:])
    assert "detector_nurses" in captured, "탐지기가 불리지 않았다(차단 전제 확인 필요)"
    assert captured["detector_nurses"] is captured["first_attempt_nurses"], (
        "탐지기 간호사 리스트는 첫 시도 nurses_for_engine 과 같은 객체여야 한다")
    assert captured["detector_config"] is captured["first_attempt_config"], (
        "탐지기 config 는 첫 시도 config_override 와 같은 객체(차감 뒤)여야 한다")


# ── C8 — precheck 기록 실패 뒤에도 나이트 탐지기는 단계 A 를 그대로 쓴다 ──


class _InjectedRecordFailure(RuntimeError):
    """`_mark_config_generate_status` 를 예외로 바꿔 끼운 자리."""


def _raise_on_mark(mp: pytest.MonkeyPatch) -> None:
    import services.roster_create_service as rcs

    def _raiser(*_args, **_kwargs):
        raise _InjectedRecordFailure("characterization: 기록 실패 주입")

    mp.setattr(rcs, "_mark_config_generate_status", _raiser)


def test_night_block_survives_precheck_record_failure(engine, engine_input_tables):
    """`EI5-precheck-block` 시나리오(precheck 가 정상적으로 차단) + 기록(`_mark_config_
    generate_status`) 실패 → precheck 는 그 예외를 삼키고 '솔버로 진행'한다(기존 동작,
    `test_precheck_block_record_failure_proceeds_to_solver` 가 이미 고정). 핵심 불변(10a) —
    단계 A(`stage_a`)는 precheck try 의 **첫 문장**에서(기록보다 훨씬 전에) 지어지므로,
    그 뒤 precheck 안에서 예외가 나도 이미 지어진 단계 A 는 살아남아 나이트 블록이 그대로
    쓴다. `L0p`(탐지기 호출 표지, 캡처는 `_view_nfo is not None` 일 때만 탐지기를 부르므로
    그때만 찍힌다)로 확인한다 — 베이스라인(기록 실패 없음)은 precheck 가 먼저 차단해 나이트
    블록에 **닿지도 못해**(`L0p` 없음), 기록 실패를 주입했을 때만 나이트 블록이 단계 A 로
    정상 판정해 `L0p` 가 찍힌다."""
    baseline = run_with_output(engine, PRECHECK_BLOCK_SCENARIO)
    assert baseline.result.outcome == "http_exception", baseline.result
    assert "L0p" not in baseline.result.queries["marks"], (
        "베이스라인은 precheck 가 먼저 막아 나이트 블록에 닿지 못해야 한다")

    scenario = _with_patches(PRECHECK_BLOCK_SCENARIO, _raise_on_mark)
    run = run_with_output(engine, scenario)
    result = run.result

    assert "[NightOversupply] 판정 실패" not in run.stdout, (
        "단계 A 가 살아 있어야 한다 — RuntimeError(단계 A 없음) 로 빠지면 안 된다")
    assert result.outcome == "golden_stop", (result.outcome, result.error)
    assert result.path == "first_attempt"
    assert "L0p" in result.queries["marks"], (
        "기록 실패로 솔버 진행 경로를 타면 나이트 블록이 단계 A 로 정상 판정해 탐지기까지 돌아야 한다")
