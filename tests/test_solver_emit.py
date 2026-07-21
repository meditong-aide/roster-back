"""Phase B unit tests: ConstraintEmitRecorder + solver↔graph parity helper."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for p in (str(ROOT), str(APP)):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.constraint_impact.parity_harness import (  # noqa: E402
    SolverGraphParityReport,
    compare_solver_emitted_vs_graph_derived,
)
from services.constraint_impact.solver_emit import (  # noqa: E402
    ConstraintEmitRecorder,
    EmittedConstraint,
    get_or_attach_recorder,
)


def test_recorder_records_emit_with_mode_and_atoms():
    rec = ConstraintEmitRecorder()
    rec.emit(
        family="BoundaryTransitionBan",
        scope={"nurse_index": 0, "day": 5, "transition": "N->D"},
        target="forbid",
        mode="enforced",
        related_atom_keys=[(0, 3), (0, 4)],
    )
    rec.emit(
        family="BoundaryTransitionBan",
        scope={"nurse_index": 0, "day": 6, "transition": "E->D"},
        target="forbid",
        mode="bypassed_by_fixed",
        related_atom_keys=[(0, 4), (0, 5)],
    )
    assert rec.count() == 2
    by_fam = rec.by_family("BoundaryTransitionBan")
    assert len(by_fam) == 2
    assert by_fam[0].mode == "enforced"
    assert by_fam[1].mode == "bypassed_by_fixed"
    assert by_fam[0].related_atom_keys == [(0, 3), (0, 4)]


def test_emit_to_dict_round_trip():
    rec = ConstraintEmitRecorder()
    rec.emit(
        family="BoundaryTransitionBan",
        scope={"nurse_index": 1, "day": 2, "transition": "N->E"},
        target="forbid",
        mode="enforced",
        related_atom_keys=[(1, 0), (1, 1)],
    )
    d = rec.records()[0].to_dict()
    assert d["family"] == "BoundaryTransitionBan"
    assert d["scope"]["transition"] == "N->E"
    assert d["mode"] == "enforced"
    assert d["related_atom_keys"] == [[1, 0], [1, 1]]


def test_get_or_attach_recorder_is_idempotent():
    class _FakeRS:
        pass

    rs = _FakeRS()
    rec1 = get_or_attach_recorder(rs)
    rec1.emit(family="X", scope={"k": 1}, target="t")
    rec2 = get_or_attach_recorder(rs)
    assert rec1 is rec2
    assert rec2.count() == 1


def test_parity_helper_returns_empty_report_for_unsupported_family():
    class _FakeSnapshot:
        pass

    rep = compare_solver_emitted_vs_graph_derived(
        snapshot=_FakeSnapshot(), solver_emitted=[], family="GradeMin"
    )
    assert isinstance(rep, SolverGraphParityReport)
    assert rep.family == "GradeMin"
    assert rep.solver_count == 0
    assert rep.graph_count == 0


def test_recorder_handles_allowed_shift_mask_family():
    rec = ConstraintEmitRecorder()
    rec.emit(
        family="AllowedShiftMask",
        scope={"nurse_index": 0, "day": 1, "shift": "D"},
        target="forbid",
        mode="enforced",
        related_atom_keys=[(0, 0)],
        metadata={"allowed": ["N"]},
    )
    assert rec.by_family("AllowedShiftMask")[0].metadata["allowed"] == ["N"]


def test_recorder_handles_not_one_night_family():
    rec = ConstraintEmitRecorder()
    rec.emit(
        family="NotOneNight",
        scope={"nurse_index": 2, "day": 5},
        target="implies_neighbor_n",
        mode="enforced",
        related_atom_keys=[(2, 3), (2, 4), (2, 5)],
    )
    assert rec.by_family("NotOneNight")[0].target == "implies_neighbor_n"
    assert len(rec.by_family("NotOneNight")[0].related_atom_keys) == 3


def test_parity_helper_detects_solver_only_when_graph_empty():
    """When snapshot has no nurses/atoms, graph nodes for transitions are 0;
    any solver-emitted record becomes 'solver_only'."""
    from dataclasses import dataclass, field
    from typing import Any

    @dataclass
    class _Attempt:
        attempt_index: int = 0
        label: str = "primary"
        grade_strategy: str = "COMBINED"
        forced_grade_soft_fallback: bool = False
        config_flags: dict = field(default_factory=dict)

    @dataclass
    class _Snap:
        year: int = 2026
        month: int = 5
        attempt: Any = field(default_factory=_Attempt)
        shift_types: list = field(default_factory=lambda: ["D", "E", "N", "O", "M"])
        config_payload: dict = field(default_factory=dict)
        nurse_ids_in_scope: list = field(default_factory=list)
        inbound_nurse_ids: list = field(default_factory=list)
        nurses: list = field(default_factory=list)
        assignment_windows: list = field(default_factory=list)
        carryover_artifacts: list = field(default_factory=list)
        fixed_cells: list = field(default_factory=list)
        special_fixed_requests: list = field(default_factory=list)
        merged_initial_constraints: dict = field(default_factory=dict)
        join: list = field(default_factory=list)
        leave: list = field(default_factory=list)
        active_days_by_nurse: dict = field(default_factory=dict)
        blocked_by_nurse: dict = field(default_factory=dict)
        fixed_wanted_cells: set = field(default_factory=set)
        fixed_type_by_cell: dict = field(default_factory=dict)
        coverage_exclude_cells: set = field(default_factory=set)
        vacation_off_cells: set = field(default_factory=set)
        structural_off_cells: set = field(default_factory=set)
        forced_off_cap_excluded: set = field(default_factory=set)
        off_exception_cells: set = field(default_factory=set)
        off_exception_vacation_cells: set = field(default_factory=set)
        weekend_days: set = field(default_factory=set)
        n_forbid_n: set = field(default_factory=set)
        preceptee_facts: list = field(default_factory=list)
        preflight_alerts: list = field(default_factory=list)
        mid_feasibility_error: Any = None
        constraint_modes: list = field(default_factory=list)

    snap = _Snap()
    emitted = [
        EmittedConstraint(
            family="BoundaryTransitionBan",
            scope={"nurse_index": 0, "day": 5, "transition": "N->D"},
            target="forbid",
            mode="enforced",
        )
    ]
    rep = compare_solver_emitted_vs_graph_derived(snapshot=snap, solver_emitted=emitted)
    assert rep.solver_count == 1
    assert rep.graph_count == 0
    assert "transition:0:5:N->D" in rep.solver_only
    assert rep.matched == []
