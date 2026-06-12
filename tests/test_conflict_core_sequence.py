"""실제 CP-SAT 시퀀스 충돌이 assumption wrap → conflict core 로 잡히는지.

test_conflict_core_real_solver.py(용량 충돌)의 연장 — 여기서는 '시간축 시퀀스 충돌'
(전이금지 N→D + 고정 N,D)이 MUS 로 보고됨을 확인. 이게 50케이스의 Window/Sequence
충돌을 '실제 검출 경로'로 올리는 토대(연구 §4: solver conflict → domain cause).
"""

from ortools.sat.python import cp_model

from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard


def _seq_model():
    m = cp_model.CpModel()
    reg = HardAssumptionRegistry(m)
    shifts = ["D", "E", "N", "O"]
    x = {(d, s): m.NewBoolVar(f"x_{d}_{s}") for d in range(2) for s in shifts}
    for d in range(2):
        m.Add(sum(x[(d, s)] for s in shifts) == 1)
    # 고정(도메인): day0=N, day1=D
    m.Add(x[(0, "N")] == 1)
    m.Add(x[(1, "D")] == 1)
    # 전이금지 N->D 를 assumption 으로 wrap
    add_hard(m, reg, name="TransitionBan:N->D:day0",
             constraint_expr=(x[(0, "N")] + x[(1, "D")] <= 1),
             meta={"node_id": "transition:N->D:day0", "type": "TransitionBanNode",
                   "label": "전이금지 N→D", "scope": "nurse", "scope_key": "nurse_seq",
                   "pattern": "transition_ban"})
    reg.attach_to_model()
    return m, reg


def test_sequence_conflict_surfaces_as_mus_core():
    m, reg = _seq_model()
    solver = cp_model.CpSolver()
    st = solver.Solve(m)
    assert solver.StatusName(st) == "INFEASIBLE"
    cores = reg.extract_conflict_cores(solver, solver_phase="primary")
    assert len(cores) == 1
    c = cores[0]
    assert c["pattern"] == "cpsat_mus:transition_ban"
    assert c["causal_layer"] == "structural"
    assert any(mm["type"] == "TransitionBanNode" for mm in c["members"])


def test_unwrapped_sequence_conflict_is_silent():
    """대조: 전이금지를 wrap 하지 않으면(일반 Add) 시퀀스 충돌도 MUS 침묵."""
    m = cp_model.CpModel()
    reg = HardAssumptionRegistry(m)
    shifts = ["D", "E", "N", "O"]
    x = {(d, s): m.NewBoolVar(f"x_{d}_{s}") for d in range(2) for s in shifts}
    for d in range(2):
        m.Add(sum(x[(d, s)] for s in shifts) == 1)
    m.Add(x[(0, "N")] == 1)
    m.Add(x[(1, "D")] == 1)
    m.Add(x[(0, "N")] + x[(1, "D")] <= 1)   # 전이금지 — wrap 안 함
    add_hard(m, reg, name="Dummy", constraint_expr=m.NewBoolVar("d") >= 0,
             meta={"scope_key": "dummy", "pattern": "dummy"})
    reg.attach_to_model()
    solver = cp_model.CpSolver()
    st = solver.Solve(m)
    assert solver.StatusName(st) == "INFEASIBLE"
    cores = reg.extract_conflict_cores(solver, solver_phase="primary")
    assert cores == []   # 침묵 — wrap 커버리지가 검출의 전제임을 못박음
