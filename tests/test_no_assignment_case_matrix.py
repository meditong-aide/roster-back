import pytest

from services.precheck.payload import build_unrecoverable_payload


def _build_payload(*, precheck_issues=None, violated=None, cores=None, shortages=None):
    return build_unrecoverable_payload(
        precheck_result={"issues": list(precheck_issues or [])},
        applied_relaxations=[],
        last_error_reason="infeasible",
        violated_constraints=list(violated or [{"reason_code": "NO_ASSIGNMENT"}]),
        conflict_cores=list(cores or []),
        pool_snapshot={"shortages": list(shortages or [])},
    )


@pytest.mark.parametrize(
    "case_id,precheck_issues,violated,cores,shortages,expected",
    [
        ("NA-CAP-002", [], [{"reason_code": "NO_ASSIGNMENT"}], [], [{"pool_id": "team_pool:1:D", "shortage": 3}], {"capacity_shortage"}),
        ("NA-ELI-007", [{"reason_code": "ALLOWED_SHIFTS_ISOLATES_NURSE"}], [{"reason_code": "NO_ASSIGNMENT"}], [], [], {"eligibility_lock"}),
        ("NA-ELI-009", [], [{"reason_code": "NO_ASSIGNMENT"}], [{"pattern": "cpsat_mus:allowed_shift_mask"}], [], {"eligibility_lock"}),
        ("NA-FIX-011", [], [{"reason_code": "NO_ASSIGNMENT"}, {"reason_code": "FIXED_ASSIGN_EXCEEDS_NEED"}], [], [], {"fixed_lock"}),
        ("NA-FIX-015", [], [{"reason_code": "NO_ASSIGNMENT"}], [{"pattern": "cpsat_mus:initial_forbidden"}], [], {"fixed_lock"}),
        ("NA-CAR-016", [], [{"reason_code": "NO_ASSIGNMENT"}, {"reason_code": "PREV_MONTH_TRANSITION"}], [], [], {"carryover_lock"}),
        ("NA-CAR-017", [], [{"reason_code": "NO_ASSIGNMENT"}], [{"pattern": "cpsat_mus:carryover_boundary"}], [], {"carryover_lock"}),
        ("NA-MIX-019", [{"reason_code": "ALLOWED_SHIFTS_ISOLATES_NURSE"}], [{"reason_code": "NO_ASSIGNMENT"}], [], [{"pool_id": "team_pool:1:E", "shortage": 1}], {"capacity_shortage", "eligibility_lock"}),
        ("NA-MIX-020", [], [{"reason_code": "NO_ASSIGNMENT"}, {"reason_code": "FIXED_ASSIGN_VIOLATES_ALLOWED"}], [], [{"pool_id": "grade_pool:1:N", "shortage": 2}], {"capacity_shortage", "fixed_lock"}),
        ("NA-MIX-023", [{"reason_code": "ALLOWED_SHIFTS_ISOLATES_NURSE"}], [{"reason_code": "NO_ASSIGNMENT"}, {"reason_code": "PREV_MONTH_TRANSITION"}], [{"pattern": "cpsat_mus:carryover_boundary"}], [], {"eligibility_lock", "carryover_lock"}),
        ("NA-MIX-024", [{"reason_code": "ALLOWED_SHIFTS_ISOLATES_NURSE"}], [{"reason_code": "NO_ASSIGNMENT"}, {"reason_code": "FIXED_ASSIGN_BREAKS_TEAM_MIN"}, {"reason_code": "PREV_MONTH_TRANSITION"}], [{"pattern": "cpsat_mus:carryover_boundary"}], [{"pool_id": "team_pool:2:D", "shortage": 4}], {"capacity_shortage", "eligibility_lock", "fixed_lock", "carryover_lock"}),
    ],
)
def test_no_assignment_breakdown_case_matrix(case_id, precheck_issues, violated, cores, shortages, expected):
    payload = _build_payload(
        precheck_issues=precheck_issues,
        violated=violated,
        cores=cores,
        shortages=shortages,
    )
    fp = payload["infeasibility"]["fix_plan"]
    got = set(fp.get("no_assignment_breakdown") or [])
    assert got == expected, f"{case_id} mismatch: got={got} expected={expected}"


def test_no_assignment_shortage_targets_exist_in_primary_action():
    payload = _build_payload(
        shortages=[
            {"pool_id": "team_pool:team_1:D", "shortage": 6},
            {"pool_id": "team_pool:team_2:N", "shortage": 2},
        ]
    )
    actions = payload["infeasibility"]["fix_plan"]["actions"]
    assert payload["infeasibility"]["fix_plan"]["reason_source"] in {"inferred", "direct"}
    assert actions and actions[0]["action_id"] == "adjust_coverage_or_supply"
    assert "단계적으로" in " ".join(actions[0].get("how_ko") or [])
    assert isinstance(actions[0].get("bounded_adjustment"), dict)
    assert actions[0]["bounded_adjustment"]["min"] >= 1
    assert actions[0]["bounded_adjustment"]["max"] <= 2
    assert "일괄 해제" in (actions[0].get("guardrail_ko") or "")
    pools = {t["pool_id"] for t in (actions[0].get("targets") or [])}
    assert {"team_pool:team_1:D", "team_pool:team_2:N"}.issubset(pools)


def test_no_assignment_direct_reason_takes_precedence_source_tag():
    payload = _build_payload(
        violated=[
            {"reason_code": "NO_ASSIGNMENT"},
            {"reason_code": "NO_ASSIGNMENT_CAPACITY"},
            {"reason_code": "NO_ASSIGNMENT_FIXED"},
        ],
        cores=[],
        shortages=[],
    )
    fp = payload["infeasibility"]["fix_plan"]
    assert fp["reason_source"] == "direct"
    assert set(fp["no_assignment_breakdown"]) >= {"capacity_shortage", "fixed_lock"}
