"""13b: rollback 뒤에도 메모리 전용 오버레이(DB 와 다른 등급)가 탐색 입력에 남는다 — 간호사 재결속 고정(리뷰 MEDIUM 1)."""
from __future__ import annotations

import dataclasses

import pytest

from .backend_check import assert_sqlite_backends
from .golden import build_record
from .harness import run_with_output
from .ladder import drain_live_graph_dirs
from .scenario_ladder import LADDER_SCENARIOS
from .test_ei13b_rollback_keeps_probe_inputs import _attempts, _force_rollback, _options

SEEN = {"grade_before": None}


def _overlay(mp: pytest.MonkeyPatch) -> None:
    from services import roster_create_service as rcs

    orig = rcs._run_cp_sat_basic
    st = {"done": False}

    def wrapper(db, current_user, nurses_in_group, *a, **k):
        if not st["done"] and nurses_in_group:
            st["done"] = True
            for n in nurses_in_group[:3]:
                cur = n.__dict__.get("grade")
                SEEN["grade_before"] = cur
                n.__dict__["grade"] = 3 if cur != 3 else 2   # overlay that differs from the DB column
        return orig(db, current_user, nurses_in_group, *a, **k)

    mp.setattr(rcs, "_run_cp_sat_basic", wrapper)


def _run(engine, name, *, rollback):
    sc = LADDER_SCENARIOS[name]
    patches = sc.patches + (_overlay,) + ((_force_rollback,) if rollback else ())
    sc = dataclasses.replace(sc, patches=patches)
    run = run_with_output(engine, sc)
    return build_record(sc, run), run.stdout


@pytest.fixture(scope="module")
def runs(engine, engine_input_tables):
    assert_sqlite_backends(engine)
    out = {}
    for name in ("S1-L",):
        out[(name, False)] = _run(engine, name, rollback=False)
        out[(name, True)] = _run(engine, name, rollback=True)
    drain_live_graph_dirs()
    return out


def test_overlay_applied_and_probe_inputs_equal(runs):
    base, rolled = runs[("S1-L", False)][0], runs[("S1-L", True)][0]
    assert SEEN["grade_before"] is not None
    assert [a["site"] for a in _attempts(rolled)] == [a["site"] for a in _attempts(base)]
    for i, (b, r) in enumerate(zip(_attempts(base), _attempts(rolled), strict=True)):
        assert b["delta"] == r["delta"], (i, b["site"])
    assert _options(rolled) == _options(base)
