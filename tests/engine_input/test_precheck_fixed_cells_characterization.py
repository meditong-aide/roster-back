"""EI-10d 특성화 테스트 — precheck 고정 칸 번역이 실패해도 생성은 막히지 않고, 번역이 조회를 늘리지 않는다.

골든 하네스(`harness.run_with_output` · `scenario_defs.SCENARIOS`)를 그대로 쓴다. 기존 시나리오·골든은
건드리지 않는다 — 이 파일은 시나리오에 몽키패치를 덧붙인 **사본**(`dataclasses.replace`)만 실행한다.

고정하는 것(골든이 가리지 못하는 자리):
  번역 실패 — `precheck_fixed_cells` 가 예외를 내면 `[Precheck] 고정 칸 변환 실패(무시 …)` 한 줄을 남기고
              L0 `fixed_cells == []` 로 판정한다(= 10d 이전과 같은 '칸 없이 판정'). 경로·쿼리는 그대로다.
  호출 순서 — precheck 번역이 `rcs._build_code_to_main_map` 을 **먼저** 부르므로 그 첫 호출 실패는 번역만
              죽이고, 뒤이은 나이트 탐지기 호출(둘째)은 영향이 없다.
"""
from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable

import pytest

from .harness import Scenario, run_with_output
from .scenario_defs import SCENARIOS


def _called_from(*names: str, depth: int = 10) -> bool:
    """호출 스택(패치 wrapper 의 호출자부터 `depth` 단계)에 `names` 중 하나의 함수가 있는가 — EI-10g3: 단계 B 빌더가 precheck 앞에서
    같은 하위 함수를 먼저 부르므로 '첫 호출만 예외' 대신 시험이 겨냥한 소비자(precheck 번역·나이트 탐지기·팀 주입)의 호출만 겨냥한다."""
    frame = sys._getframe(2)
    for _ in range(depth):
        if frame is None:
            return False
        if frame.f_code.co_name in names:
            return True
        frame = frame.f_back
    return False


FAILURE_LINE = "[Precheck] 고정 칸 변환 실패(무시 — 고정 칸 없이 판정): RuntimeError: characterization: code2main 실패 주입"


def _with_patches(scenario: Scenario, *extra_patches: Callable) -> Scenario:
    """기존 시나리오를 바꾸지 않고 패치만 덧붙인 사본을 만든다(frozen dataclass)."""
    return dataclasses.replace(scenario, patches=scenario.patches + tuple(extra_patches))


def _raise_on_code2main_once(mp: pytest.MonkeyPatch) -> None:
    """`rcs._build_code_to_main_map` 의 precheck 번역 호출만 예외(첫 한 번) — EI-10g3 부터 단계 B 빌더가 같은 함수를 precheck 앞에서 먼저 부른다."""
    import services.roster_create_service as rcs

    state = {"called": False}
    original = rcs._build_code_to_main_map

    def _raiser(*args, **kwargs):
        if not state["called"] and _called_from("_precheck_cells_or_empty"):
            state["called"] = True
            raise RuntimeError("characterization: code2main 실패 주입")
        return original(*args, **kwargs)

    mp.setattr(rcs, "_build_code_to_main_map", _raiser)


def test_translation_failure_logged_and_precheck_runs_without_cells(engine, engine_input_tables):
    """S8(고정 칸 4개) + 번역 실패 → 로그 한 줄 · L0 `fixed_cells == []` · 경로·쿼리는 기준(S8)과 같다."""
    baseline = run_with_output(engine, SCENARIOS["S8"]).result
    run = run_with_output(engine, _with_patches(SCENARIOS["S8"], _raise_on_code2main_once))
    result = run.result

    assert run.stdout.count(FAILURE_LINE) == 1
    assert [len(call["fixed_cells"]) for call in result.layers["L0"]] == [0]
    assert (result.outcome, result.path) == (baseline.outcome, baseline.path) == ("golden_stop", "first_attempt")
    assert result.queries["tables"] == baseline.queries["tables"], "번역 실패가 조회를 바꾸면 안 된다"
    assert result.queries["count"] == baseline.queries["count"]


def test_translation_failure_does_not_reach_the_night_detector(engine, engine_input_tables):
    """번역이 `_build_code_to_main_map` 첫 호출에서 죽어도 둘째 호출(나이트 탐지기)은 멀쩡하다 — 탐지기 로그 없음."""
    run = run_with_output(engine, _with_patches(SCENARIOS["S8"], _raise_on_code2main_once))
    assert "[NightOversupply] 판정 실패" not in run.stdout


@pytest.mark.parametrize("name, expected", [("S8", 4), ("S5", 17), ("S1", 0)])
def test_translation_keeps_every_modeled_cell(name, expected, engine, engine_input_tables):
    """번역본은 precheck 가 모델링하는 칸을 하나도 잃지 않는다(S8 4칸 · S5 주휴 17칸 · S1 0칸) — 모양은 세 칸뿐이다."""
    result = run_with_output(engine, SCENARIOS[name]).result
    cells = result.layers["L0"][0]["fixed_cells"]
    assert len(cells) == expected
    assert all(set(cell) == {"nurse_id", "day_index", "shift"} for cell in cells)
    assert all(cell["shift"] in {"D", "E", "N", "O"} for cell in cells)
