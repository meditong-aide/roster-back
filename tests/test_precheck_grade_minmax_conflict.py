from __future__ import annotations

from services.precheck.team_grade_precheck import PrecheckInput, PrecheckNurse, run_precheck


def test_precheck_detects_grade_min_exceeds_max_conflict():
    inp = PrecheckInput(
        num_days=31,
        nurses=[
            PrecheckNurse(nurse_id="N1", grade=1, team_id=1, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="N2", grade=1, team_id=1, join_day=0, leave_day=30),
            PrecheckNurse(nurse_id="N3", grade=2, team_id=1, join_day=0, leave_day=30),
        ],
        teams=[1],
        roster_config={"use_mid": False, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}},
        team_coverage={},
        grade_constraints={
            "minimum_by_shift": {"D": {"1": 3}},
            "max_by_shift": {"D": {"1": 1}},
        },
    )

    out = run_precheck(inp)
    assert out["status"] == "HAS_ISSUES"
    codes = {i["reason_code"] for i in out["issues"]}
    assert "GRADE_MIN_EXCEEDS_MAX" in codes
