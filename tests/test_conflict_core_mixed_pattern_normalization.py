from ortools.sat.python import cp_model

from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard


class _FakeInfeasibleSolver:
    def __init__(self, indices):
        self._indices = indices

    def SufficientAssumptionsForInfeasibility(self):
        return list(self._indices)


def _idx(registry: HardAssumptionRegistry, name: str) -> int:
    return registry._by_name[name].lit.Index()


def test_mixed_patterns_normalized_with_candidates():
    model = cp_model.CpModel()
    reg = HardAssumptionRegistry(model)

    add_hard(
        model,
        reg,
        name="A:nurse_7",
        constraint_expr=(0 <= 1),
        meta={
            "node_id": "a",
            "type": "CarryoverTransitionNode",
            "label": "A",
            "scope": "nurse",
            "scope_key": "nurse_7",
            "nurse_id": "7",
            "pattern": "carryover_boundary",
        },
    )
    add_hard(
        model,
        reg,
        name="B:nurse_7",
        constraint_expr=(0 <= 1),
        meta={
            "node_id": "b",
            "type": "CarryoverTransitionNode",
            "label": "B",
            "scope": "nurse",
            "scope_key": "nurse_7",
            "nurse_id": "7",
            "pattern": "recovery_2n2off",
        },
    )

    solver = _FakeInfeasibleSolver([_idx(reg, "A:nurse_7"), _idx(reg, "B:nurse_7")])
    cores = reg.extract_conflict_cores(solver)
    assert len(cores) == 1
    core = cores[0]
    assert core["pattern"] == "cpsat_mus:mixed"
    assert set(core["pattern_candidates"]) == {
        "cpsat_mus:carryover_boundary",
        "cpsat_mus:recovery_2n2off",
    }


def test_mixed_signature_collapses_across_nurses_even_with_order_difference():
    model = cp_model.CpModel()
    reg = HardAssumptionRegistry(model)

    # nurse_7: A then B
    add_hard(
        model,
        reg,
        name="A:nurse_7",
        constraint_expr=(0 <= 1),
        meta={
            "node_id": "a7",
            "type": "CarryoverTransitionNode",
            "label": "A7",
            "scope": "nurse",
            "scope_key": "nurse_7",
            "nurse_id": "7",
            "pattern": "carryover_boundary",
        },
    )
    add_hard(
        model,
        reg,
        name="B:nurse_7",
        constraint_expr=(0 <= 1),
        meta={
            "node_id": "b7",
            "type": "RecoveryOffNode",
            "label": "B7",
            "scope": "nurse",
            "scope_key": "nurse_7",
            "nurse_id": "7",
            "pattern": "recovery_2n2off",
        },
    )

    # nurse_8: B then A (reverse)
    add_hard(
        model,
        reg,
        name="B:nurse_8",
        constraint_expr=(0 <= 1),
        meta={
            "node_id": "b8",
            "type": "RecoveryOffNode",
            "label": "B8",
            "scope": "nurse",
            "scope_key": "nurse_8",
            "nurse_id": "8",
            "pattern": "recovery_2n2off",
        },
    )
    add_hard(
        model,
        reg,
        name="A:nurse_8",
        constraint_expr=(0 <= 1),
        meta={
            "node_id": "a8",
            "type": "CarryoverTransitionNode",
            "label": "A8",
            "scope": "nurse",
            "scope_key": "nurse_8",
            "nurse_id": "8",
            "pattern": "carryover_boundary",
        },
    )

    solver = _FakeInfeasibleSolver(
        [
            _idx(reg, "A:nurse_7"),
            _idx(reg, "B:nurse_7"),
            _idx(reg, "B:nurse_8"),
            _idx(reg, "A:nurse_8"),
        ]
    )
    cores = reg.extract_conflict_cores(solver)
    assert len(cores) == 1
    core = cores[0]
    assert core["pattern"] == "cpsat_mus:mixed"
    assert core["scope"] == "multi_nurse"
    assert core["affected_count"] == 2
