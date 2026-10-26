"""EI-10g3 — 단계 B 빌더(첫 시도 직전 엔진 입력)가 precheck 앞으로 옮겨졌다(p2/D/SPEC.md §4.4).

- 빌더에 넘긴 `first_attempt_config`(`_sa_` 제외)가 첫 `_run_cp_sat_basic` 직전 `config_dict` 와 같다 — 이동이 안전한 근거.
- 등급 getter 가 실패해도(빌더는 삼키고 `Lazy` 가 예외를 캐시) `to_precheck_input` 이 같은 예외를 다시 올려 '[Precheck] 실행 실패' 길로 간다.
"""
from __future__ import annotations

import copy

import pytest

from .harness import run_with_output
from .scenario_defs import SCENARIOS


def _rcs():
    from services import roster_create_service

    return roster_create_service


@pytest.mark.parametrize("name", ["S1", "S5", "S4"])
def test_builder_input_equals_first_attempt_config(name, engine, engine_input_tables):
    rcs = _rcs()
    seen: dict = {}
    original_builder = rcs.build_engine_input_at_first_attempt
    original_run = rcs._run_cp_sat_basic

    def builder_spy(db, stage_a, **kwargs):
        seen.setdefault("builder_cfg", copy.deepcopy(kwargs["first_attempt_config"]))
        seen.setdefault("builder_calls", 0)
        seen["builder_calls"] += 1
        return original_builder(db, stage_a, **kwargs)

    def run_spy(*args, **kwargs):
        if "run_cfg" not in seen:
            seen["run_cfg"] = {k: copy.deepcopy(v) for k, v in kwargs["config_override"].items() if not str(k).startswith("_sa_")}
        return original_run(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "build_engine_input_at_first_attempt", builder_spy)
        mp.setattr(rcs, "_run_cp_sat_basic", run_spy)
        run_with_output(engine, SCENARIOS[name])
    assert seen["builder_calls"] == 1
    assert seen["builder_cfg"] == seen["run_cfg"]


def test_grade_getter_failure_still_takes_the_precheck_failure_path(engine, engine_input_tables):
    """등급 조회 실패 — 빌더는 `_safe_call` 로 삼키지만 `Lazy` 가 예외를 캐시해 `to_precheck_input` 이 같은 예외를 올린다:
    '[Precheck] 실행 실패(무시하고 솔버 진행)' 로그 + precheck 결과 없음(L0 없음) — 옛 동작과 같다."""
    rcs = _rcs()

    def boom(*_args, **_kwargs):
        raise RuntimeError("grade-boom")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "_fetch_grade_config_dict", boom)
        run = run_with_output(engine, SCENARIOS["S1"])
    assert "[Precheck] 실행 실패(무시하고 솔버 진행): " in run.stdout
    assert run.result.layers["L0"] == []
