"""Outcome-conditional retention tests for _build_constraint_impact_payload.

We don't run the full solver here — instead we mock a roster_system with the
attributes _build_constraint_impact_payload reads, plus an attached emit
recorder, and assert that payload shape differs by outcome (SAT vs UNSAT).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for p in (str(ROOT), str(APP)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _stub_payload_runner(*, valid_outcome: bool, emit_count: int):
    """Run _build_constraint_impact_payload against a stubbed roster_system
    where analyze_current_roster is monkey-patched to control SAT/UNSAT,
    and the recorder has a configurable number of emit records."""
    from services.constraint_impact.solver_emit import (
        ConstraintEmitRecorder,
        EmittedConstraint,
    )

    class _Req:
        year = 2026
        month = 5

    class _StubRS:
        roster = None  # build_assignment_atoms tolerates by guard
        config = None

    rs = _StubRS()
    recorder = ConstraintEmitRecorder()
    for i in range(emit_count):
        recorder.emit(
            family="BoundaryTransitionBan",
            scope={"nurse_index": 0, "day": i + 1, "transition": "N->D"},
            target="forbid",
            mode="enforced" if i > 0 else "bypassed_by_fixed",
            related_atom_keys=[(0, i), (0, i + 1)],
        )
    setattr(rs, "_constraint_impact_solver_emit_recorder", recorder)

    # Patch heavy dependencies in roster_create_service to avoid full DB pipeline
    from services import roster_create_service as rcs

    class _Snapshot:
        class attempt:
            label = "primary"
            attempt_index = 0
            forced_grade_soft_fallback = False
        nurses: list = []
        fixed_cells: list = []
        preceptee_facts: list = []

    class _Analysis:
        def __init__(self, valid: bool):
            self.valid_under_current_semantics = valid
            self.atom_count = 0
            self.fixed_atom_count = 0
            self.preceptee_atom_count = 0
            self.coverage_excluded_atom_count = 0
            self.hard_violation_count = 0 if valid else 5
            self.risky_constraint_count = 0
            self.constraint_mode_summary = {}
            self.preflight_alerts = []
            self.violated_constraints = []
            self.risky_constraints = []

    import services.constraint_impact as ci

    real_snap = ci.build_semantics_snapshot_from_roster_system
    real_atoms = ci.build_current_atoms_from_roster_system
    real_analyze = ci.analyze_current_roster
    real_gaps = rcs._compute_coverage_gaps

    ci.build_semantics_snapshot_from_roster_system = lambda *a, **kw: _Snapshot()
    ci.build_current_atoms_from_roster_system = lambda snapshot, rs: []
    ci.analyze_current_roster = lambda snapshot, current_atoms: _Analysis(valid_outcome)
    rcs._compute_coverage_gaps = lambda rs: []

    try:
        payload = rcs._build_constraint_impact_payload(rs, _Req())
    finally:
        ci.build_semantics_snapshot_from_roster_system = real_snap
        ci.build_current_atoms_from_roster_system = real_atoms
        ci.analyze_current_roster = real_analyze
        rcs._compute_coverage_gaps = real_gaps
    return payload


def test_sat_outcome_does_not_dump_full_emit_records():
    payload = _stub_payload_runner(valid_outcome=True, emit_count=10)
    assert payload["outcome"] == "sat"
    assert payload["solver_emitted_nodes"] == []  # SAT: no granular dump
    # interesting_events still surfaces the bypassed_by_fixed
    kinds = {e["mode"] for e in payload["interesting_emit_events"]}
    assert "bypassed_by_fixed" in kinds
    # summary still aggregates everything
    assert payload["solver_emitted_summary"]["BoundaryTransitionBan"]["enforced"] == 9
    assert payload["solver_emitted_summary"]["BoundaryTransitionBan"]["bypassed_by_fixed"] == 1
    # no conflict probe needed for SAT
    assert payload["conflict_probe"] is None


def test_unsat_outcome_includes_full_records_and_probe():
    payload = _stub_payload_runner(valid_outcome=False, emit_count=10)
    assert payload["outcome"] == "unsat"
    # UNSAT: full granular records present (capped per family)
    assert len(payload["solver_emitted_nodes"]) == 10
    # conflict_probe payload populated
    assert payload["conflict_probe"] is not None
    assert "ranked_candidates" in payload["conflict_probe"]
    assert "matched_scenarios" in payload["conflict_probe"]
    assert "probe_plan" in payload["conflict_probe"]


def test_unsat_caps_records_per_family():
    payload = _stub_payload_runner(valid_outcome=False, emit_count=200)
    # cap = 50 per family
    assert len(payload["solver_emitted_nodes"]) == 50
    assert payload["solver_emitted_summary"]["BoundaryTransitionBan"]["enforced"] == 199
