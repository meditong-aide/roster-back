"""Regression: weekend-off 간호사의 주말 강제휴무가 per-day 커버리지에 미반영되던 갭.

gap-hunter(8 wards): 전원 weekend-off → 주말 시프트를 아무도 못 채워 INFEASIBLE 인데
precheck 는 weekend-off 를 off '카운트'로만 접고 어느 날인지 몰라 주말 공급부족을
per-day 로 못 봤다(6/8 만 incidental 진단).

수정: build_precheck_input 이 weekend_off_only_enable(엔진 기본 True)일 때 weekend-off
간호사의 주말 날짜를 fixed_off 로 표시 → 기존 per-day 커버리지 체크가 주말을 본다.
"""

from __future__ import annotations

from services.precheck import run_runtime_precheck

YEAR, MONTH = 2026, 8


def _codes(nurses, cfg):
    r = run_runtime_precheck(nurses_dict=nurses, config_dict=cfg, grade_config=None,
                             fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False)
    return {i.get("reason_code") for i in r.get("issues", [])}


def _nurses(n, weekend_off_upto=0):
    return [{"nurse_id": f"n{i}", "grade": 1, "allowed_shifts": [],
             "is_weekend_off": i < weekend_off_upto} for i in range(n)]


def _cfg(enable=True, D=2, E=2, N=2):
    return {"daily_shift_requirements": {"D": D, "E": E, "N": N},
            "weekend_off_only_enable": enable,
            "global_monthly_off_days": 2, "standard_personal_off_days": 8}


def test_all_weekend_off_triggers_weekend_coverage_shortage():
    # 전원 weekend-off → 주말 가용 0 < 수요 → per-day 부족
    assert "GLOBAL_DAY_CAPACITY_SHORTAGE" in _codes(_nurses(10, weekend_off_upto=10), _cfg())


def test_partial_weekend_off_no_false_positive():
    # 2명만 weekend-off, 나머지 8명이 주말 커버 가능 → 부족 없음
    assert "GLOBAL_DAY_CAPACITY_SHORTAGE" not in _codes(_nurses(10, weekend_off_upto=2), _cfg())


def test_weekend_policy_disabled_no_effect():
    # weekend_off_only_enable=False → 주말 강제휴무 미적용 → 주말 부족 없음
    assert "GLOBAL_DAY_CAPACITY_SHORTAGE" not in _codes(_nurses(10, weekend_off_upto=10), _cfg(enable=False))
