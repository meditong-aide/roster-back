"""compute_monthly_supply_demand — 월 단위 max-flow (per-day flow 의 상보).

per-day flow(compute_supply_demand)는 하루 교차경쟁만 본다. 이 월별 flow 는 per-day 가
못 보는 층을 담당한다: 월 총 capacity(간호사당 근무가능일), 월 야간 상한(max_nig),
per-shift eligibility 총량. shift별 required/filled/shortage 를 정확히 산출한다.
"""

from __future__ import annotations

from services.ontology_graph.supply_demand import (
    NurseSupply,
    compute_monthly_supply_demand,
)


def _nurses(n, elig=("D", "E", "N")):
    return [NurseSupply(nurse_id=f"n{i}", eligible_by_day={0: set(elig)}) for i in range(n)]


def _wd(n, days=30):
    return {f"n{i}": days for i in range(n)}


def test_monthly_night_cap_shortage():
    # 10명 × max_nig 5 = 야간 50 < 수요 100 → N 부족 50
    r = compute_monthly_supply_demand(
        _nurses(10), {"N": 100},
        workdays_by_nurse=_wd(10), night_cap_by_nurse={f"n{i}": 5 for i in range(10)})
    ns = next(s for s in r.shifts if s.shift_code == "N")
    assert ns.filled == 50 and ns.shortage == 50


def test_total_capacity_shortage():
    # 5명 × 근무가능 10 = 50 총capacity < 수요 60 → 총부족 10
    r = compute_monthly_supply_demand(
        _nurses(5), {"D": 20, "E": 20, "N": 20}, workdays_by_nurse=_wd(5, 10))
    assert r.total_filled == 50
    assert r.total_shortage() == 10


def test_eligibility_limits_night():
    # 10명이지만 N 가능 2명뿐 → 야간 capacity = 2 × 근무가능일
    nurses = [NurseSupply(nurse_id=f"n{i}",
                          eligible_by_day={0: ({"N"} if i < 2 else {"D", "E"})})
              for i in range(10)]
    r = compute_monthly_supply_demand(nurses, {"N": 100}, workdays_by_nurse=_wd(10))
    ns = next(s for s in r.shifts if s.shift_code == "N")
    assert ns.eligible_nurses == 2
    assert ns.filled == 60 and ns.shortage == 40


def test_feasible_no_shortage():
    r = compute_monthly_supply_demand(
        _nurses(20), {"D": 50, "E": 50, "N": 50}, workdays_by_nurse=_wd(20, 21))
    assert r.total_shortage() == 0


def test_zero_workday_nurse_excluded():
    # 근무가능일 0 인 간호사는 공급에서 제외
    r = compute_monthly_supply_demand(
        _nurses(3), {"N": 30},
        workdays_by_nurse={"n0": 0, "n1": 10, "n2": 10})
    ns = next(s for s in r.shifts if s.shift_code == "N")
    assert ns.filled == 20 and ns.shortage == 10
