"""EI-14r3 — 엔진 OFF 하드 하한(min_off)의 하한값이 precheck 용량 바닥에 실린다.

엔진(fallback_lex.py:3253-3345)은 off_first=False 이고 N 전담·주말휴무·프리셉티가 아닌 간호사에게 `compute_off_bounds` 의
`min_off_required` 를 slack 0 하드로 건다. bridge 가 같은 순수 함수로 간호사별 값을 구해 `PrecheckNurse.min_off_required` 에 싣고
`_hard_off_floor` 가 `max(global + p, min_off_required)` 로 쓴다.
"""
from __future__ import annotations

import pytest
from services.cp_sat.off_policy import compute_off_bounds
from services.precheck.runtime_bridge import build_precheck_input
from services.precheck.team_grade_precheck import PrecheckNurse, _hard_off_floor, _working_capacity

YEAR, MONTH = 2026, 11


def _cfg(**extra):
    base = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "max_conseq_work": 6, "off_days": 8, "off_first": False}
    base.update(extra)
    return base


def _nurse(nid="n0", **extra):
    return {"nurse_id": nid, "grade": 1, "allowed_shifts": [], "sequence": 0, **extra}


def _built(nurses, cfg):
    return build_precheck_input(nurses_dict=nurses, config_dict=cfg, grade_config=None, fixed_cells=None, year=YEAR, month=MONTH).nurses


def test_full_month_nurse_gets_off_days_as_floor():
    (n,) = _built([_nurse()], _cfg())
    assert n.min_off_required == 8
    assert _hard_off_floor(n, {}) == 8
    assert _working_capacity(n, {}) == 22


def test_value_equals_compute_off_bounds_for_partial_month_joiner():
    cfg = _cfg()
    (n,) = _built([_nurse(joining_date="2026-11-21T00:00:00")], cfg)
    expect = compute_off_bounds(source=cfg, avail_days=10, vacation_cnt=0, reference_days=30)["min_off_required"]
    assert (n.join_day, n.leave_day) == (20, 29)
    assert n.min_off_required == expect == 3        # 8 × 10/30 비례


def test_legacy_config_without_off_days_uses_the_engine_legacy_sum_without_double_global():
    cfg = _cfg(global_monthly_off_days=2, standard_personal_off_days=8)
    cfg.pop("off_days")
    (n,) = _built([_nurse()], cfg)
    assert n.min_off_required == 10                 # 엔진 effective = standard + global
    roster_cfg = {"global_monthly_off_days": 2}
    assert _hard_off_floor(n, roster_cfg) == 10     # global 을 한 번 더 더하지 않는다(12 가 아니다)


def test_floor_is_max_of_global_plus_p_and_min_off():
    n = PrecheckNurse(nurse_id="n0", join_day=0, leave_day=29, personal_off_adjustment=5, min_off_required=8)
    assert _hard_off_floor(n, {"global_monthly_off_days": 0}) == 8
    big = PrecheckNurse(nurse_id="n0", join_day=0, leave_day=29, personal_off_adjustment=11, min_off_required=8)
    assert _hard_off_floor(big, {"global_monthly_off_days": 2}) == 13
    none = PrecheckNurse(nurse_id="n0", join_day=0, leave_day=29, personal_off_adjustment=5)
    assert _hard_off_floor(none, {}) == 5            # 필드 기본값 0 이면 전과 같다


@pytest.mark.parametrize(("nurse_extra", "cfg_extra"), [
    ({}, {"off_first": True}),
    ({"is_weekend_off": True}, {}),
    ({"allowed_shifts": ["N"]}, {}),
    ({"preceptor_id": "p1"}, {}),
    ({"joining_date": "2026-12-05T00:00:00"}, {}),          # 다음 달 입사 — 구간 없음
    ({"resignation_date": "2026-11-01T00:00:00"}, {}),      # 1일 퇴사 — 근무 구간 없음
])
def test_excluded_cases_have_no_floor(nurse_extra, cfg_extra):
    (n,) = _built([_nurse(**nurse_extra)], _cfg(**cfg_extra))
    assert n.min_off_required == 0


def test_missing_off_first_key_means_engine_default_false():
    cfg = _cfg()
    cfg.pop("off_first")
    (n,) = _built([_nurse()], cfg)
    assert n.min_off_required == 8


def test_tight_ward_is_newly_blocked_and_control_passes():
    """수요 8명/일 · 10명 · off_first=False — 총 수요 240 > 용량 220(= 10 × (30 − 8)). 같은 병동의 off_first=True 는 통과한다."""
    from services.precheck import run_runtime_precheck

    def codes(**extra):
        cfg = _cfg(daily_shift_requirements={"D": 3, "E": 3, "N": 2}, max_conseq_work=5, **extra)
        result = run_runtime_precheck(
            nurses_dict=[_nurse(f"n{i}", sequence=i) for i in range(10)], config_dict=cfg, grade_config=None,
            fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False)
        return {i["reason_code"] for i in result["issues"]}

    assert "CAPACITY_TOTAL_SHORTAGE" in codes(off_first=False)
    assert "CAPACITY_TOTAL_SHORTAGE" not in codes(off_first=True)


def test_c1r_off_floor_shortage_carries_off_days_lever():
    """EI-14C1r — OFF 하한이 원인인 차단은 `off_floor_binding` 과 `off_days` 처방을 싣는다(원인 = 간호사 부족이 아니다)."""
    from services.cp_sat.undiagnosed_probe import config_lever_options_from_issues
    from services.precheck import run_runtime_precheck

    cfg = _cfg(daily_shift_requirements={"D": 3, "E": 3, "N": 2}, max_conseq_work=5, off_first=False)
    result = run_runtime_precheck(
        nurses_dict=[_nurse(f"n{i}", sequence=i) for i in range(10)], config_dict=cfg, grade_config=None,
        fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False)
    (issue,) = [i for i in result["issues"] if i["reason_code"] == "CAPACITY_TOTAL_SHORTAGE"]
    ev = issue["evidence"]
    assert ev["conseq_cap_binding"] is None
    b = ev["off_floor_binding"]
    assert (b["config_key"], b["current"], b["suggested_value"]) == ("off_days", 8, 6)   # 10 × (30 − 6) = 240 ≥ 240
    (opt,) = config_lever_options_from_issues([issue])
    assert opt["apply"] == {"off_days": 6}


def test_c1r_off_floor_lever_absent_for_true_shortage():
    """off_days 를 0 으로 해도 못 채우는 병동(진짜 인원 부족)은 레버를 만들지 않는다."""
    from services.precheck import run_runtime_precheck

    cfg = _cfg(daily_shift_requirements={"D": 5, "E": 5, "N": 5}, max_conseq_work=5, off_first=False)
    result = run_runtime_precheck(
        nurses_dict=[_nurse(f"n{i}", sequence=i) for i in range(10)], config_dict=cfg, grade_config=None,
        fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False)
    (issue,) = [i for i in result["issues"] if i["reason_code"] == "CAPACITY_TOTAL_SHORTAGE"]
    assert issue["evidence"]["off_floor_binding"] is None
