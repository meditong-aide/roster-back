"""presolve_shortage_diagnosis — 솔버 전 max-flow 부족 조기진단(2단계 ①).

자격 부족(eligibility) vs 총 capacity 부족을 구분하고, 복구 선택지는 관리자 노브만(개인
속성 불가침) 제시한다. 증명된 하한(max-flow) — 조합적 원인은 못 보므로 과소추정 가능.
"""

from __future__ import annotations

from services.ontology_graph.presolve_diagnosis import presolve_shortage_diagnosis

Y, M = 2026, 8


def _nurses(allowed_list):
    return [{"nurse_id": f"n{i}", "grade": 1, "team_id": None, "allowed_shifts": a}
            for i, a in enumerate(allowed_list)]


def test_eligibility_shortage_detected_with_reason():
    # 5명 중 3명 주간전담 → N 가능 2명 < N 수요 3
    nurses = _nurses([[], []] + [["D", "E"]] * 3)
    cfg = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 3}, "off_days": 8, "max_nig_per_month": 15}
    d = presolve_shortage_diagnosis(nurses, cfg, Y, M)
    n = next(s for s in d["shortages"] if s["shift"] == "N")
    assert n["eligible_nurses"] == 2
    assert n["reason"] == "eligibility_shortage"
    assert n["monthly_shortage_lower_bound"] > 0


def test_capacity_shortage_when_eligible_but_low_workdays():
    # 5명 전원 N 가능, off 28 → 근무가능 3일 → 총 capacity 부족(자격 아님)
    nurses = _nurses([[]] * 5)
    cfg = {"daily_shift_requirements": {"D": 0, "E": 0, "N": 5}, "off_days": 28, "max_nig_per_month": 15}
    d = presolve_shortage_diagnosis(nurses, cfg, Y, M)
    n = next(s for s in d["shortages"] if s["shift"] == "N")
    assert n["eligible_nurses"] == 5
    assert n["reason"] == "capacity_shortage"


def test_recovery_excludes_personal_attributes():
    # 복구 선택지에 개인 속성 노브(max_nig/allowed_shifts 등)가 절대 없어야
    nurses = _nurses([[]] + [["D", "E"]] * 9)
    cfg = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 5}, "off_days": 8, "max_nig_per_month": 15}
    d = presolve_shortage_diagnosis(nurses, cfg, Y, M)
    assert d["shortages"], "부족이 있어야 복구 선택지 테스트 의미"
    personal = {"max_nig_per_month", "max_night_shifts_per_month", "allowed_shifts",
                "n_max", "n_exact", "is_weekend_off"}
    for r in d["recovery_options"]:
        assert r["config_key"] not in personal


def test_no_shortage_when_ample():
    nurses = _nurses([[]] * 20)
    cfg = {"daily_shift_requirements": {"D": 2, "E": 2, "N": 2}, "off_days": 8, "max_nig_per_month": 15}
    d = presolve_shortage_diagnosis(nurses, cfg, Y, M)
    assert d["shortages"] == []
    assert d["recovery_options"] == []


def test_legacy_demand_fields_supported():
    # daily_shift_requirements 없이 day_req/eve_req/nig_req 만 있어도 동작
    nurses = _nurses([[], []] + [["D", "E"]] * 3)
    cfg = {"day_req": 1, "eve_req": 1, "nig_req": 3, "off_days": 8, "max_nig_per_month": 15}
    d = presolve_shortage_diagnosis(nurses, cfg, Y, M)
    assert any(s["shift"] == "N" and s["reason"] == "eligibility_shortage" for s in d["shortages"])


def test_fast_no_solver():
    nurses = _nurses([[]] * 38)
    cfg = {"daily_shift_requirements": {"D": 8, "E": 8, "N": 8}, "off_days": 10, "max_nig_per_month": 15}
    d = presolve_shortage_diagnosis(nurses, cfg, Y, M)
    assert d["elapsed_ms"] < 500  # 솔버 없음 → 넉넉히 500ms 미만
