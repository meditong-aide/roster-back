from services.roster_create_service import _extract_unrecoverable_violated_constraints


class _DummyRosterSystem:
    def __init__(self):
        self._constraint_pool_snapshot = {
            "shortages": [{"pool_id": "team_pool:team_1:D", "shortage": 2}]
        }
        self.nurses = []
        self.blocked_by_nurse = {}
        self._validator_evidence = {}


def test_extract_unrecoverable_includes_no_assignment_direct_capacity_and_carryover():
    rs = _DummyRosterSystem()
    msg = "[reason_code=NO_ASSIGNMENT] Infeasible 진단 | [reason_code=PREV_MONTH_TRANSITION]"
    items = _extract_unrecoverable_violated_constraints(rs, generated=None, validation_error=msg)
    codes = {x.get("reason_code") for x in items}
    assert "NO_ASSIGNMENT" in codes
    assert "NO_ASSIGNMENT_CAPACITY" in codes
    assert "NO_ASSIGNMENT_CARRYOVER" in codes


def test_extract_unrecoverable_includes_no_assignment_direct_fixed_when_fixed_signal_present():
    rs = _DummyRosterSystem()
    msg = "[reason_code=NO_ASSIGNMENT] conflict with FIXED_ASSIGN_EXCEEDS_NEED"
    items = _extract_unrecoverable_violated_constraints(rs, generated=None, validation_error=msg)
    codes = {x.get("reason_code") for x in items}
    assert "NO_ASSIGNMENT_FIXED" in codes


def test_extract_unrecoverable_uses_validator_evidence_for_eligibility():
    rs = _DummyRosterSystem()
    rs._validator_evidence = {
        "total_failed_cells": 4,
        "eligible_zero_cells": 3,
        "required_minus_assigned_total": 5,
        "top_failed_cells": [
            {"day": 1, "shift": "D", "required": 3, "assigned": 0, "eligible": 0, "shortage": 3, "eligible_gap": 3}
        ],
    }
    msg = "[reason_code=NO_ASSIGNMENT] Infeasible"
    items = _extract_unrecoverable_violated_constraints(rs, generated=None, validation_error=msg)
    by_code = {x.get("reason_code"): x for x in items}
    assert "NO_ASSIGNMENT_ELIGIBILITY" in by_code
    det = by_code["NO_ASSIGNMENT_ELIGIBILITY"].get("details") or {}
    assert det.get("source") == "no_assignment_direct_rule"
    assert isinstance(det.get("validator_evidence"), dict)


def test_extract_unrecoverable_uses_validator_evidence_for_fixed_and_carryover():
    rs = _DummyRosterSystem()
    rs._validator_evidence = {
        "total_failed_cells": 5,
        "eligible_zero_cells": 0,
        "required_minus_assigned_total": 7,
        "fixed_forbidden_count": 14,
        "carryover_artifact_count": 2,
        "top_failed_cells": [],
    }
    msg = "[reason_code=NO_ASSIGNMENT] Infeasible"
    items = _extract_unrecoverable_violated_constraints(rs, generated=None, validation_error=msg)
    codes = {x.get("reason_code") for x in items}
    assert "NO_ASSIGNMENT_FIXED" in codes
    assert "NO_ASSIGNMENT_CARRYOVER" in codes
