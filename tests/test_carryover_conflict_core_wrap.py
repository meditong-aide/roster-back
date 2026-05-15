from ortools.sat.python import cp_model

from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard


class _FakeInfeasibleSolver:
    def __init__(self, indices):
        self._indices = indices

    def SufficientAssumptionsForInfeasibility(self):
        return list(self._indices)


def _assumption_index(registry: HardAssumptionRegistry, name: str) -> int:
    return registry._by_name[name].lit.Index()  # 테스트용 내부 접근


def test_carryover_boundary_pattern_exposed_in_conflict_core_members():
    model = cp_model.CpModel()
    registry = HardAssumptionRegistry(model)

    add_hard(
        model,
        registry,
        name="CarryoverBoundary:nurse_7:day_0",
        constraint_expr=(0 <= 1),
        meta={
            "node_id": "carryover_boundary:nurse_7:day_0",
            "type": "CarryoverTransitionNode",
            "label": "carryover boundary 3N2OFF",
            "scope": "nurse",
            "scope_key": "nurse_7",
            "nurse_id": "7",
            "pattern": "carryover_boundary",
        },
    )

    fake_solver = _FakeInfeasibleSolver([
        _assumption_index(registry, "CarryoverBoundary:nurse_7:day_0")
    ])
    cores = registry.extract_conflict_cores(fake_solver)

    assert len(cores) == 1
    core = cores[0]
    assert core["pattern"] == "cpsat_mus:carryover_boundary"
    assert core["solver_phase"] == "primary"
    assert core["causal_layer"] == "structural"
    assert core["per_layer_counts"] == {"structural": 1}
    assert core["affected_count"] == 1
    assert core["affected_nurse_ids"] == ["7"]
    assert any(m.get("type") == "CarryoverTransitionNode" for m in core["members"])


def test_carryover_boundary_non_nurse_scope_is_grouped_with_scope_keys():
    model = cp_model.CpModel()
    registry = HardAssumptionRegistry(model)

    add_hard(
        model,
        registry,
        name="CarryoverBoundary:grade_1_n:day_0",
        constraint_expr=(0 <= 1),
        meta={
            "node_id": "carryover_boundary:grade_1_n:day_0",
            "type": "CarryoverTransitionNode",
            "label": "grade 1 carryover boundary",
            "scope": "grade",
            "scope_key": "grade_1_n",
            "pattern": "carryover_boundary",
        },
    )
    add_hard(
        model,
        registry,
        name="CarryoverBoundary:grade_2_n:day_0",
        constraint_expr=(0 <= 1),
        meta={
            "node_id": "carryover_boundary:grade_2_n:day_0",
            "type": "CarryoverTransitionNode",
            "label": "grade 2 carryover boundary",
            "scope": "grade",
            "scope_key": "grade_2_n",
            "pattern": "carryover_boundary",
        },
    )

    fake_solver = _FakeInfeasibleSolver([
        _assumption_index(registry, "CarryoverBoundary:grade_1_n:day_0"),
        _assumption_index(registry, "CarryoverBoundary:grade_2_n:day_0"),
    ])
    cores = registry.extract_conflict_cores(fake_solver)

    assert len(cores) == 1
    core = cores[0]
    assert core["pattern"] == "cpsat_mus:carryover_boundary"
    assert core["solver_phase"] == "primary"
    assert core["scope"] == "grade"
    assert core["affected_count"] == 2
    assert sorted(core["affected_scope_keys"]) == ["grade_1_n", "grade_2_n"]
    assert core["causal_layer"] == "structural"
    assert core["per_layer_counts"] == {"structural": 2}
    assert len(core["members"]) == 2
    assert {m["node_id"] for m in core["members"]} == {
        "carryover_boundary:grade_1_n:day_0",
        "carryover_boundary:grade_2_n:day_0",
    }
    assert len(core["per_member_cores"]) == 2


def test_carryover_boundary_multi_constraints_same_nurse_stays_single_core():
    model = cp_model.CpModel()
    registry = HardAssumptionRegistry(model)

    for name, node_id in [
        ("CarryoverRecovery3N2OFFTail2:nurse_7:day_0", "carryover_recovery_3n2off_tail2:nurse_7:day_0"),
        ("CarryoverRecovery3N2OFFGuard:nurse_7:day_2", "carryover_recovery_3n2off_guard:nurse_7:day_2"),
        ("CarryoverRecovery2N2OFFBoundary:nurse_7:day_0", "carryover_recovery_2n2off_boundary:nurse_7:day_0"),
    ]:
        add_hard(
            model,
            registry,
            name=name,
            constraint_expr=(0 <= 1),
            meta={
                "node_id": node_id,
                "type": "CarryoverTransitionNode",
                "label": "carryover boundary",
                "scope": "nurse",
                "scope_key": "nurse_7",
                "nurse_id": "7",
                "pattern": "carryover_boundary",
            },
        )

    fake_solver = _FakeInfeasibleSolver([
        _assumption_index(registry, "CarryoverRecovery3N2OFFTail2:nurse_7:day_0"),
        _assumption_index(registry, "CarryoverRecovery3N2OFFGuard:nurse_7:day_2"),
        _assumption_index(registry, "CarryoverRecovery2N2OFFBoundary:nurse_7:day_0"),
    ])

    cores = registry.extract_conflict_cores(fake_solver)
    assert len(cores) == 1
    core = cores[0]
    assert core["pattern"] == "cpsat_mus:carryover_boundary"
    assert core["affected_count"] == 1
    assert core["affected_nurse_ids"] == ["7"]
    assert core["members_total"] == 3
    assert core["causal_layer"] == "structural"
