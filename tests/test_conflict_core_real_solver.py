"""실제 CP-SAT solver 기반 conflict-core 신뢰성 테스트.

기존 conflict-core 테스트(test_carryover_conflict_core_wrap.py 등)는 전부
``_FakeInfeasibleSolver`` 로 assumption 인덱스를 미리 주입한다 — 즉 실제 ortools
solver 를 infeasible 까지 돌려 ``SufficientAssumptionsForInfeasibility()`` 가
정말 코어를 내놓는지는 한 번도 검증된 적이 없었다.

이 파일은 그 공백을 메운다. 동시에 **신뢰성 경계**를 명시적으로 못박는다:
post-failure MUS 진단은 *원인 제약이 add_hard 로 assumption-wrap 된 경우에만*
작동하고, wrap 되지 않은 제약(team_min / grade / coverage 의 대부분 — 일반
``m.Add`` 로 추가됨)이 진짜 원인이면 ``SufficientAssumptionsForInfeasibility()``
가 빈 리스트를 반환해 **침묵 실패(conflict_cores=[])** 한다.

이것이 실험로그(Exp-12~24)에서 라이브 런 다수가 ``conflict_cores=[]`` 로 끝난
구조적 이유이며, 통합 그래프 + 수요·공급(max-flow) state 노드가 필요한 근거다.
(docs/UNIFIED_ONTOLOGY_GRAPH_SCHEMA.md)
"""

from ortools.sat.python import cp_model

from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard


def _solve(build):
    model = cp_model.CpModel()
    reg = HardAssumptionRegistry(model)
    build(model, reg)
    reg.attach_to_model()
    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    cores = (
        reg.extract_conflict_cores(solver, solver_phase="primary")
        if status == cp_model.INFEASIBLE
        else []
    )
    return solver.StatusName(status), cores


# ── 작동하는 경계: 원인 제약이 wrap 된 경우 ────────────────────────────────
def test_real_solver_both_causes_wrapped_yields_cores():
    """x>=5 ∧ x<=3, 둘 다 add_hard → 실제 solver INFEASIBLE → 코어 2개."""
    def build(m, reg):
        x = m.NewIntVar(0, 10, "x")
        add_hard(m, reg, name="CovMin:x", constraint_expr=x >= 5,
                 meta={"scope_key": "cov_x", "pattern": "coverage", "type": "ConstraintNode"})
        add_hard(m, reg, name="CovMax:x", constraint_expr=x <= 3,
                 meta={"scope_key": "covmax_x", "pattern": "coverage_max", "type": "ConstraintNode"})

    status, cores = _solve(build)
    assert status == "INFEASIBLE"
    assert len(cores) == 2
    patterns = {c["pattern"] for c in cores}
    assert patterns == {"cpsat_mus:coverage", "cpsat_mus:coverage_max"}


def test_real_solver_coverage_shortage_wrapped_yields_core():
    """단일 nurse 인데 D 2명 요구 (wrap) → INFEASIBLE → 코어 1개."""
    def build(m, reg):
        w0 = m.NewBoolVar("w0_D")
        add_hard(m, reg, name="CovMin:day0:D", constraint_expr=w0 >= 2,
                 meta={"scope_key": "day0_D", "pattern": "coverage", "type": "ConstraintNode"})

    status, cores = _solve(build)
    assert status == "INFEASIBLE"
    assert len(cores) == 1
    assert cores[0]["pattern"] == "cpsat_mus:coverage"


def test_real_solver_presolve_trivial_still_wrapped_yields_core():
    """x==5 ∧ x==3 (presolve 단계서 모순), 둘 다 wrap → 코어 잡힘."""
    def build(m, reg):
        x = m.NewIntVar(0, 10, "x")
        add_hard(m, reg, name="Fix5", constraint_expr=x == 5,
                 meta={"scope_key": "fix5", "pattern": "fixed"})
        add_hard(m, reg, name="Fix3", constraint_expr=x == 3,
                 meta={"scope_key": "fix3", "pattern": "fixed"})

    status, cores = _solve(build)
    assert status == "INFEASIBLE"
    assert len(cores) >= 1


# ── 침묵 실패 경계: 원인 제약이 wrap 되지 않은 경우 (현 동작 고정) ──────────
def test_real_solver_all_unwrapped_is_silent_failure():
    """진짜 모순이 전부 일반 m.Add → INFEASIBLE 이지만 cores=[] (침묵 실패).

    이 어설션은 결함을 '통과'시키는 게 아니라, MUS 경로의 구조적 사각지대를
    회귀로 고정해 가시화한다. wrap 커버리지가 늘면 이 테스트가 깨지며 재검토를
    강제한다."""
    def build(m, reg):
        x = m.NewIntVar(0, 10, "x")
        m.Add(x >= 5)
        m.Add(x <= 3)  # 진짜 원인 — wrap 안 됨
        add_hard(m, reg, name="Dummy", constraint_expr=m.NewBoolVar("d") >= 0,
                 meta={"scope_key": "dummy", "pattern": "dummy"})

    status, cores = _solve(build)
    assert status == "INFEASIBLE"
    assert cores == []  # ← 침묵 실패. 원인이 wrap 밖이라 MUS 가 비어 있음


def test_real_solver_cause_unwrapped_only_irrelevant_wrapped_is_silent():
    """원인(x>=5 ∧ x<=3)은 unwrap, wrap 된 건 무관한 feasible 제약 →
    assumption-독립 infeasible → cores=[]. team_min/coverage 가 일반 Add 로
    들어가고 그게 병목일 때의 실제 라이브 상황과 동형."""
    def build(m, reg):
        x = m.NewIntVar(0, 10, "x")
        m.Add(x >= 5)
        m.Add(x <= 3)            # 진짜 원인 — unwrapped
        y = m.NewIntVar(0, 10, "y")
        add_hard(m, reg, name="Cov:y", constraint_expr=y >= 1,
                 meta={"scope_key": "cov_y", "pattern": "coverage"})  # 무관·feasible

    status, cores = _solve(build)
    assert status == "INFEASIBLE"
    assert cores == []
