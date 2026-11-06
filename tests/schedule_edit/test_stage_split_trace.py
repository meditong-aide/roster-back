"""엔진 단계 분리(S7a) 골든 — CP-SAT 호출의 모양(시간 상한·씨앗·작업자·모델 지문·힌트·목적식)이 리팩터 전과 같은가.

`propose_edits` 를 단계 함수로 나눠도 풀이 호출의 순서·인자·모델은 1비트도 달라지면 안 된다. 호출마다 기록해 골든
(`fixtures/stage_split_trace.json`)과 견준다. 시간은 가짜 시계로 고정한다(풀이 한 번에 0.5초 흐름). 결과는 `result_to_json` 전체를 견주되 후보의 optimal 표시는
뺀다(OPTIMAL/FEASIBLE 은 장비 속도에 갈린다). 호출별 상태는 solved/infeasible/other 로 묶는다.
레버는 같은 비용의 둘째가 갈리므로 가장 적게 바꾸는 레버만 전부, 나머지는 개수·칸 수만 견준다.

골든은 리팩터 전 코드로 한 번 만들었다 — 기본은 비교만 한다. 다시 만들어야 하면 `STAGE_TRACE_UPDATE=1`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import time
from pathlib import Path

import pytest
from ortools.sat.python import cp_model

from services.schedule_edit import solver as S
from services.schedule_edit.requests import CountTarget, LeaveCell, SetCell, StaffingPlan, StaffRule
from services.schedule_edit.serialize import result_to_json
from tests.schedule_edit.test_solver import BEFORE_MONTH, make_ctx
from tests.schedule_edit.test_shortcuts import _long_ward, _wide_ward

GOLDEN = Path(__file__).parent / "fixtures" / "stage_split_trace.json"
TICK = 0.5


def _hash(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()[:16]


def _plan(low_d=1, high_d=2):
    return StaffingPlan(alternatives=((StaffRule("all", "D", low_d, high_d, exact=False),
                                       StaffRule("all", "E", 1, 2, exact=False)),),
                        first_day=0, last_day=9, chosen=0, short=100, count_fixed=True)


def _plan_ctx():
    return make_ctx({"A": "DEOODDDEOE", "B": "EDEODEOODD", "C": "ODDOOEDEDE", "D": "DDEEOODDEO"},
                    cov_min={"D": 1, "E": 1}, config=None)


def _hint():
    """고른 후보를 시작 해로 — 실제 시계로 한 번 풀어 얻는다(기록 밖)."""
    ctx = _wide_ward()
    res = S.propose_edits(ctx, [SetCell("B", 0, "O")], today=BEFORE_MONTH)
    return S.apply_candidate(ctx, res.candidates[0])


def _leave_ward():
    ctx = _wide_ward()
    ctx.leave_codes = {"연": "연차휴가"}
    return ctx


def _cases():
    """이름 → (재료, 요청, propose_edits 추가 인자)."""
    wide, long = _wide_ward(), _long_ward()
    return {
        "window_wide_setcell": (wide, [SetCell("B", 0, "O")], {}),
        "window_long_setcell_levers": (long, [SetCell("A", 1, "O")], {}),
        "count_target": (wide, [CountTarget("A", frozenset({"D"}), 3, 0, 9)], {}),
        "plan": (_plan_ctx(), [_plan()], {}),
        "window_leave": (_leave_ward(), [LeaveCell("B", 2, "연")], {}),
        "window_explain_false_infeasible": (_wide_ward(), [SetCell("A", 1, "O"), SetCell("B", 1, "O"),
                                                           SetCell("C", 1, "O")], {"explain": False}),
        "window_hint": (_wide_ward(), [SetCell("B", 0, "O")], {"hint": "HINT"}),
        "plan_infeasible": (_plan_ctx(), [dataclasses.replace(_plan(5, 6), short=None)], {}),
        "plan_polish_skipped": (_plan_ctx(), [_plan()], {"time_budget": 2.2}),
    }


def _bucket(status: int) -> str:
    return "solved" if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else (
        "infeasible" if status == cp_model.INFEASIBLE else "other")


def _strip_optimal(data):
    """OPTIMAL/FEASIBLE 은 느린 장비에서 갈릴 수 있어 비교에서 뺀다(후보의 optimal 표시)."""
    if isinstance(data, dict):
        return {k: _strip_optimal(v) for k, v in data.items() if k != "optimal"}
    if isinstance(data, list):
        return [_strip_optimal(v) for v in data]
    return data


def _normalize(result: dict) -> dict:
    """레버는 같은 비용의 둘째가 풀 때마다 다른 사람으로 갈린다(test_shortcuts 주석) — 가장 칸이 적게 드는 레버만 전부
    견주고, 나머지는 개수와 칸 수만 견준다."""
    out = _strip_optimal(result)
    levers = out["levers"]
    if levers:
        best = min(levers, key=lambda lv: lv["changes"][1])
        out["levers"] = {"count": len(levers), "changes": sorted(lv["changes"] for lv in levers),
                         "best": best if sum(lv["changes"][1] == best["changes"][1] for lv in levers) == 1 else None}
    return out


def _record(case: str):
    ctx, requests, kwargs = _cases()[case]
    if kwargs.get("hint") == "HINT":
        kwargs = {**kwargs, "hint": _hint()}
    calls: list = []
    clock = [1000.0]
    real_solve = cp_model.CpSolver.Solve

    def solve(self, model, *args, **kwargs):
        proto = model.Proto()
        site = inspect.stack()[1].function
        calls.append({
            "site": site,
            "seconds": round(self.parameters.max_time_in_seconds, 6),
            "seed": self.parameters.random_seed,
            "workers": self.parameters.num_workers,
            "subsolvers": list(self.parameters.subsolvers),
            "vars": len(proto.variables),
            "constraints": len(proto.constraints),
            "model": _hash(proto.SerializeToString(deterministic=True)),
            "hints": len(proto.solution_hint.vars),
            "hint_hash": _hash(proto.solution_hint.SerializeToString(deterministic=True)),
            "objective": _hash(proto.objective.SerializeToString(deterministic=True)),
        })
        status = real_solve(self, model, *args, **kwargs)
        calls[-1]["status"] = _bucket(status)
        clock[0] += TICK
        return status

    mp = pytest.MonkeyPatch()
    mp.setattr(time, "monotonic", lambda: clock[0])
    mp.setattr(cp_model.CpSolver, "Solve", solve)
    try:
        result = S.propose_edits(ctx, requests, today=BEFORE_MONTH, **kwargs)
    finally:
        mp.undo()
    return {"result": _normalize(result_to_json(result)), "calls": calls}


def test_solver_call_trace_matches_golden():
    got = {case: _record(case) for case in _cases()}
    if os.environ.get("STAGE_TRACE_UPDATE") == "1":
        GOLDEN.write_text(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        pytest.skip("골든을 다시 썼다")
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    got = json.loads(json.dumps(got, sort_keys=True))
    for case in want:
        assert got[case]["calls"] == want[case]["calls"], case
        assert got[case]["result"] == want[case]["result"], case
    assert got.keys() == want.keys()
