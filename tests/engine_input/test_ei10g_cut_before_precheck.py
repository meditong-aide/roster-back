"""EI-10g2 — 고정근무 차감이 precheck 앞(단계 A 직후)으로 옮겨졌다(p2/D/SPEC.md §4.3).

- `stage_a.config_dict` 는 깊은 사본이라 차감 **전** 수요 그대로(`demand.pre`) · precheck 입력(L0)은 차감 **뒤**.
- 차감 실패(모르는 고정 코드 등)는 precheck 를 막지 않는다 — precheck 는 차감 전 입력으로 계속.
- `[FixedShiftCoverage]` 로그가 `[Precheck]` 로그보다 먼저 찍힌다.
- 단계 A 조립이 실패해 거기서 못 돈 경우의 안전망: 옛 자리에서 한 번만 돈다.
"""
from __future__ import annotations

import copy

import pytest

from .harness import run_with_output
from .scenario_defs import SCENARIOS
from .test_fixed_cut_characterization import _s5_badcode


def _rcs():
    from services import roster_create_service

    return roster_create_service


def _run_capturing_stage_a(engine, scenario, mp):
    """`stage_a_at_precheck` 가 돌려준 단계 A 의 `config_dict`(참조) · 입구 `config_dict`(깊은 사본)를 담는다."""
    rcs = _rcs()
    seen: dict = {}
    original = rcs.stage_a_at_precheck

    def spy(db, **kwargs):
        stage = original(db, **kwargs)
        seen.setdefault("stage_config", stage.config_dict)
        seen.setdefault("entry_config", copy.deepcopy(kwargs["config_dict"]))
        return stage

    mp.setattr(rcs, "stage_a_at_precheck", spy)
    return seen, run_with_output(engine, scenario)


def _n_by_day(cfg: dict, code: str) -> list:
    return [d.get(code) for d in cfg.get("daily_shift_requirements_by_day") or []]


def test_stage_a_keeps_pre_cut_demand_and_precheck_input_is_post_cut(engine, engine_input_tables):
    with pytest.MonkeyPatch.context() as mp:
        seen, run = _run_capturing_stage_a(engine, SCENARIOS["S5"], mp)
    l0 = run.result.layers["L0"][0]["config_dict"]
    pre, post = _n_by_day(seen["stage_config"], "D"), _n_by_day(l0, "D")
    assert pre == _n_by_day(seen["entry_config"], "D")      # 단계 A = 입구 = 차감 전
    assert pre[0] == 2 and post[0] == 1                       # 평일 D 2 → 1 (고정근무자 1명)
    assert pre[24] == 2 and post[24] == 2                     # 25일 공휴일은 차감 없음
    assert all(b <= a for a, b in zip(pre, post, strict=True))             # 차감은 수요를 줄이기만 한다


def test_cut_failure_keeps_precheck_on_the_pre_cut_input(engine, engine_input_tables):
    """모르는 고정 코드(Z) — 차감 실패(무시). precheck 입력(L0)은 단계 A 와 같은 차감 전 수요이고 로그 순서는 차감 → precheck 다."""
    with pytest.MonkeyPatch.context() as mp:
        seen, run = _run_capturing_stage_a(engine, _s5_badcode(), mp)
    l0 = run.result.layers["L0"][0]["config_dict"]
    assert _n_by_day(l0, "D") == _n_by_day(seen["stage_config"], "D")
    lines = run.stdout.splitlines()
    fail = next(i for i, ln in enumerate(lines) if ln.startswith("[FixedShiftCoverage] 차감 실패(무시)"))
    assert all(not ln.startswith("[Precheck]") for ln in lines[:fail])


def test_cut_log_comes_before_precheck_log_and_precheck_blocked_paths_still_cut(engine, engine_input_tables):
    """S9-F — 차감 로그가 `[Precheck]`·나이트 차단 로그보다 앞이다(precheck 가 막는 경로에서도 차감이 먼저 돈다)."""
    run = run_with_output(engine, SCENARIOS["S9-F"])
    lines = run.stdout.splitlines()
    cut = next(i for i, ln in enumerate(lines) if ln.startswith("[FixedShiftCoverage] 고정근무자"))
    first_precheck = next((i for i, ln in enumerate(lines) if ln.startswith("[Precheck]")), len(lines))
    assert cut < first_precheck


def test_stage_a_failure_falls_back_to_the_old_place_and_cuts_once(engine, engine_input_tables):
    """단계 A 조립이 실패해도(precheck 는 '[Precheck] 실행 실패' 로 건너뜀) 차감은 옛 자리에서 한 번 돈다 — 옛 동작 보존."""
    rcs = _rcs()

    def boom(db, **kwargs):
        raise RuntimeError("stage-a-boom")

    calls: list = []
    original = rcs.read_fixed_cut_inputs

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "stage_a_at_precheck", boom)
        mp.setattr(rcs, "read_fixed_cut_inputs", spy)
        run = run_with_output(engine, SCENARIOS["S5"])
    assert len(calls) == 1
    assert any(ln.startswith("[Precheck] 실행 실패") for ln in run.stdout.splitlines())
    assert any(ln.startswith("[FixedShiftCoverage] 고정근무자") for ln in run.stdout.splitlines())


def test_no_fixed_nurses_means_no_cut_reads(engine, engine_input_tables):
    rcs = _rcs()
    calls: list = []
    original = rcs.read_fixed_cut_inputs

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "read_fixed_cut_inputs", spy)
        run_with_output(engine, SCENARIOS["S1"])
    assert calls == []
