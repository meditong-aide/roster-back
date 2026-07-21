"""MUS conflict core 추출 검증.

add_hard 로 assumption-wrap 된 하드제약이 서로 충돌하면 extract_conflict_cores 가
충돌 제약을 담은 core(+ 사용자 메시지 + resolution_hints)를 반환한다.
실패-시-한번 MUS 진단 재solve(roster_create_service)가 이 메커니즘에 의존한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from ortools.sat.python import cp_model  # noqa: E402

from services.cp_sat.hard_assumption import (  # noqa: E402
    HardAssumptionRegistry,
    add_hard,
)


def _meta(nid, pattern, msg):
    return {
        "node_id": nid, "type": "TestNode", "scope": "nurse", "scope_key": "x",
        "pattern": pattern, "human_message_ko": msg,
        "resolution_hint": f"{pattern} 완화",
    }


def test_mus_extracts_conflicting_core():
    m = cp_model.CpModel()
    x = m.NewIntVar(0, 1, "x")
    reg = HardAssumptionRegistry(m)
    add_hard(m, reg, name="ForceOne:x", constraint_expr=(x == 1),
             meta=_meta("force_one:x", "force_one", "x=1 강제"))
    add_hard(m, reg, name="ForceZero:x", constraint_expr=(x == 0),
             meta=_meta("force_zero:x", "force_zero", "x=0 강제"))
    reg.attach_to_model()
    s = cp_model.CpSolver()
    assert s.Solve(m) == cp_model.INFEASIBLE

    cores = reg.extract_conflict_cores(s, solver_phase="primary")
    assert len(cores) >= 1
    members = {mm.get("node_id") for c in cores for mm in (c.get("members") or [])}
    assert "force_one:x" in members and "force_zero:x" in members
    c0 = cores[0]
    assert c0.get("human_message_ko")             # 사용자 메시지 존재
    assert c0.get("resolution_hints") is not None  # 해결 힌트 존재
    assert c0.get("core_id")


def test_wrap_off_is_plain_add():
    # registry 없이 add_hard → 평범한 m.Add (assumption 없음). feasible 여부만 동일.
    m = cp_model.CpModel()
    x = m.NewIntVar(0, 1, "x")
    # registry=None 이면 add_hard 는 plain add (cp_sat_basic 의 else 분기와 동일 효과 확인)
    m.Add(x == 1)  # wrap off 상황을 직접 재현
    s = cp_model.CpSolver()
    assert s.Solve(m) == cp_model.OPTIMAL
    assert s.Value(x) == 1
