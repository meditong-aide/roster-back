from __future__ import annotations

from services.semantics import (
    attach_constraint_ontology,
    attach_reason_code_ontology,
    extract_reason_code,
    get_default_ontology,
)
from services.precheck.team_grade_precheck import PrecheckInput, PrecheckNurse, run_precheck


def test_ontology_loader_resolves_aliases_and_modes():
    onto = get_default_ontology()
    entry = onto.get_constraint("TEAM_MIN_EXCEEDS_GLOBAL_NEED")
    assert entry is not None
    assert entry.constraint_id == "TeamMin"
    assert onto.get_parent("TeamMin") == "CoverageConstraint"
    assert onto.can_bypass("FixedWanted", "BoundaryTransitionBan") is True
    assert onto.can_bypass("FixedWanted", "ConsecutiveWorkLimit") is False


def test_attach_reason_code_ontology_from_message():
    payload = attach_reason_code_ontology(
        message="[reason_code=DAY_ZERO_COVERAGE] Infeasible 진단: 1일 필수 근무 미배정",
        severity="hard",
        evidence={"day": 1},
    )
    assert payload["reason_code"] == "DAY_ZERO_COVERAGE"
    assert payload["ontology"]["constraint_id"] == "CoverageMin"
    assert payload["ontology"]["group"] == "CoverageConstraint"


def test_attach_constraint_ontology_unknown_family_passthrough():
    fact = {"reason_code": "SOMETHING_UNKNOWN", "mode": "enforced"}
    attached = attach_constraint_ontology(fact)
    assert attached == fact


def test_precheck_issue_contains_ontology_metadata():
    inp = PrecheckInput(
        num_days=30,
        nurses=[
            PrecheckNurse(
                nurse_id="N1",
                team_id=1,
                join_day=0,
                leave_day=29,
            )
        ],
        teams=[1],
        roster_config={"use_mid": True, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}},
        team_coverage={},
        grade_constraints={},
    )
    result = run_precheck(inp)
    assert result["status"] == "HAS_ISSUES"
    issue = result["issues"][0]
    assert issue["reason_code"] == "MID_REQUIRED_MISSING"
    assert issue["ontology"]["constraint_id"] == "ConfigIntegrity"
    assert issue["ontology"]["mode"] == "precheck_blocked"


def test_extract_reason_code_returns_none_for_plain_message():
    assert extract_reason_code("plain message") is None
