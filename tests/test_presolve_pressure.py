"""온톨로지 presolve — 개인 제약 압박(제약모순형) 감지 (regression).

커버리지 부족이 아니어도 개인 제약이 물리적 모순/과부하를 만들면 presolve 가 잡아
probe 우선 완화군(pressure_families)으로 넘긴다.
  · night_floor_over_cap: n_exact/n_min > config max_nig
  · weekend_off_load: 주말휴무자(주말 전일 강제OFF)
"""
from __future__ import annotations

from services.ontology_graph.presolve_diagnosis import presolve_shortage_diagnosis
from services.roster_create_service import _priority_families_from_presolve


def _cfg(max_nig=7):
    return {"max_nig_per_month": max_nig, "off_days": 10,
            "day_req": 2, "eve_req": 1, "nig_req": 1, "use_mid": False}


def _nurse(nid, **kw):
    base = {"nurse_id": nid, "grade": 1, "team_id": None, "allowed_shifts": []}
    base.update(kw)
    return base


def test_night_floor_over_cap_flags_night_cap():
    # 177659: n_exact=13 > max_nig=7 → 달성 불가 → night_cap 압박.
    nurses = [_nurse("177659", n_exact=13)] + [_nurse(str(i)) for i in range(5)]
    res = presolve_shortage_diagnosis(nurses, _cfg(max_nig=7), 2026, 8)
    assert "night_cap" in res["pressure_families"]
    flags = {f["type"] for f in res["constraint_flags"]}
    assert "night_floor_over_cap" in flags


def test_weekend_off_flags_weekend_family():
    nurses = [_nurse("177659", is_weekend_off=True)] + [_nurse(str(i)) for i in range(5)]
    res = presolve_shortage_diagnosis(nurses, _cfg(), 2026, 8)
    assert "weekend_off" in res["pressure_families"]
    assert any(f["type"] == "weekend_off_load" for f in res["constraint_flags"])


def test_coupled_case_prioritizes_both():
    # 라이브 케이스: 주말휴무 + 야간하한 초과 동시 → probe 우선순위 앞에 둘 다.
    nurses = [_nurse("177659", is_weekend_off=True, n_exact=13)] + [_nurse(str(i)) for i in range(5)]
    res = presolve_shortage_diagnosis(nurses, _cfg(max_nig=7), 2026, 8)
    prio = _priority_families_from_presolve(res)
    assert "weekend_off" in prio and "night_cap" in prio
    # 압박군이 맨 앞(콤보 raise_max_night + disable_weekend 를 앞당김)
    assert set(prio[:2]) == {"weekend_off", "night_cap"}


def test_shift_floor_over_workdays_flags_off_budget():
    # d_exact=25 인데 근무가능일=31-10=21 → 강제 하한 합 > 근무가능일 → off_budget 압박.
    nurses = [_nurse("x", d_exact=25)] + [_nurse(str(i)) for i in range(5)]
    res = presolve_shortage_diagnosis(nurses, _cfg(), 2026, 8)
    assert "off_budget" in res["pressure_families"]
    assert any(f["type"] == "shift_floor_over_workdays" for f in res["constraint_flags"])


def test_weekend_release_needed_from_dayflow():
    # 관계 분석: 주말휴무자를 주말 요일 공급 0으로 모델링 → max-flow 로 필요 해제 인원 산출.
    # 수요 D2+E1+N1=4/일. 총 6명.
    # 1명 주말휴무 → 주말 가용 5 ≥ 4 → 0명
    r1 = presolve_shortage_diagnosis(
        [_nurse("a", is_weekend_off=True)] + [_nurse(str(i)) for i in range(5)], _cfg(), 2026, 8)
    assert r1["weekend_release_needed"] == 0
    # 3명 주말휴무 → 주말 가용 3 < 4 → 부족 1 → 1명
    r3 = presolve_shortage_diagnosis(
        [_nurse(c, is_weekend_off=True) for c in "abc"] + [_nurse(str(i)) for i in range(3)],
        _cfg(), 2026, 8)
    assert r3["weekend_release_needed"] == 1
    # 4명 주말휴무 → 주말 가용 2 < 4 → 부족 2 → 2명
    r4 = presolve_shortage_diagnosis(
        [_nurse(c, is_weekend_off=True) for c in "abcd"] + [_nurse(str(i)) for i in range(2)],
        _cfg(), 2026, 8)
    assert r4["weekend_release_needed"] == 2
    # flag 에도 실림
    assert any(f.get("type") == "weekend_off_load" and f.get("release_needed") == 2
               for f in r4["constraint_flags"])


def test_fixed_shift_reduces_other_shift_eligibility():
    # 전원 D 고정 → E·N 가용 0 → eligibility_shortage (시프트축 관계 반영).
    nurses = [_nurse(str(i), fixed_shift="D") for i in range(6)]
    res = presolve_shortage_diagnosis(nurses, _cfg(), 2026, 8)
    short = {s["shift"]: s["reason"] for s in res["shortages"]}
    assert short.get("E") == "eligibility_shortage"
    assert short.get("N") == "eligibility_shortage"


def test_joining_midmonth_reduces_early_supply():
    # 5명 8/20 입사(월초 비활성) + 1명 풀달 → 월초 커버 급감(요일별 부족 발생).
    from datetime import date
    nurses = [_nurse("full")] + [_nurse(str(i), joining_date=date(2026, 8, 20)) for i in range(5)]
    res = presolve_shortage_diagnosis(nurses, _cfg(), 2026, 8)
    # 부분월 미반영이면 월초도 6명 가용으로 봐 부족 0이지만, 반영하면 월초 1명뿐 → 부족.
    assert res["weekend_release_needed"] >= 0  # 계산 자체가 터지지 않음
    assert any(s["monthly_shortage_lower_bound"] > 0 for s in res["shortages"])


def test_no_pressure_when_within_bounds():
    # n_exact 없음/한도 내 + 주말휴무 없음 → 압박 없음.
    nurses = [_nurse(str(i)) for i in range(6)]
    res = presolve_shortage_diagnosis(nurses, _cfg(), 2026, 8)
    assert res["pressure_families"] == []
    assert res["constraint_flags"] == []
