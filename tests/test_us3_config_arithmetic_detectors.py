"""US-3 검증 — Config arithmetic detector 의 4개 신규 reason_code.

핵심 invariants (사용자 요구 'test case 가 과적합되게 정적이지 않아야'):
  1. dynamic generator 가 매개변수 (nurse_count, num_days, demand, off_budget) 다르게 생성.
  2. feasible 케이스 50개 sweep → false positive 0건.
  3. infeasible 케이스 50개 sweep → 해당 detector 가 정확히 catch (false negative 0건).
  4. detector 가 어떤 nurse_count / team / grade 분포에서도 일관 동작 (특정 case 에 hardcoded X).
"""

from __future__ import annotations

import random
import pytest

from services.precheck.config_arithmetic_detector import (
    detect_daily_demand_exceeds_nurse_count,
    detect_off_budget_exceeds_num_days,
    detect_monthly_limit_min_exceeds_max,
    detect_team_min_exceeds_team_size,
    run_arithmetic_detectors,
)


# ─────────────────────────────────────────────────────────────────────────
# detect_daily_demand_exceeds_nurse_count
# ─────────────────────────────────────────────────────────────────────────
def test_daily_demand_no_false_positive_when_feasible():
    issues = detect_daily_demand_exceeds_nurse_count(
        daily_shift_requirements={"D": 5, "E": 4, "N": 3, "O": 0},
        nurse_count=15,
        num_days=30,
    )
    assert issues == []


def test_daily_demand_catches_uniform_excess():
    issues = detect_daily_demand_exceeds_nurse_count(
        daily_shift_requirements={"D": 10, "E": 10, "N": 10},
        nurse_count=15,
        num_days=30,
    )
    # 모든 day 가 excess
    assert len(issues) == 30
    assert all(i["reason_code"] == "DAILY_DEMAND_EXCEEDS_NURSE_COUNT" for i in issues)
    assert issues[0]["details"]["total_demand"] == 30
    assert issues[0]["details"]["shortage"] == 15


def test_daily_demand_per_day_override_works():
    issues = detect_daily_demand_exceeds_nurse_count(
        daily_shift_requirements_by_day=[
            {"D": 5, "E": 3, "N": 2},   # 10 → OK (nurse_count 15)
            {"D": 8, "E": 5, "N": 3},   # 16 > 15 → excess
            {"D": 4, "E": 4, "N": 4},   # 12 → OK
        ],
        nurse_count=15,
        num_days=3,
    )
    assert len(issues) == 1
    assert issues[0]["details"]["day"] == 2
    assert issues[0]["details"]["shortage"] == 1


def test_daily_demand_ignores_off_codes():
    issues = detect_daily_demand_exceeds_nurse_count(
        daily_shift_requirements={"D": 5, "E": 5, "O": 100},  # OFF 는 demand 아님
        nurse_count=15,
        num_days=10,
    )
    assert issues == []


# ─────────────────────────────────────────────────────────────────────────
# detect_off_budget_exceeds_num_days
# ─────────────────────────────────────────────────────────────────────────
def test_off_budget_no_false_positive():
    issues = detect_off_budget_exceeds_num_days(
        nurse_off_budgets=[
            {"nurse_id": "A", "off_days_total": 8},
            {"nurse_id": "B", "off_days_total": 12},
        ],
        num_days=30,
    )
    assert issues == []


def test_off_budget_catches_excess():
    issues = detect_off_budget_exceeds_num_days(
        nurse_off_budgets=[
            {"nurse_id": "A", "off_days_total": 35},
            {"nurse_id": "B", "off_days_total": 30},  # 정확히 num_days = no excess
            {"nurse_id": "C", "off_days_total": 31},
        ],
        num_days=30,
    )
    assert len(issues) == 2
    ids = {i["details"]["nurse_id"] for i in issues}
    assert ids == {"A", "C"}


# ─────────────────────────────────────────────────────────────────────────
# detect_monthly_limit_min_exceeds_max
# ─────────────────────────────────────────────────────────────────────────
def test_monthly_limit_no_false_positive():
    issues = detect_monthly_limit_min_exceeds_max(
        monthly_limits=[
            {"nurse_id": "A", "shift": "N", "min_val": 2, "max_val": 5},
            {"nurse_id": "B", "shift": "D", "min_val": 0, "max_val": 0},  # min==max OK
        ],
    )
    assert issues == []


def test_monthly_limit_catches_contradiction():
    issues = detect_monthly_limit_min_exceeds_max(
        monthly_limits=[
            {"nurse_id": "A", "shift": "N", "min_val": 6, "max_val": 3},
            {"nurse_id": "B", "shift": "D", "min_val": 10, "max_val": 5},
            {"nurse_id": "C", "shift": "N", "min_val": 1, "max_val": 1},  # OK
        ],
    )
    assert len(issues) == 2
    # detail 정확성
    a_issue = next(i for i in issues if i["details"]["nurse_id"] == "A")
    assert a_issue["details"]["min_val"] == 6
    assert a_issue["details"]["max_val"] == 3


# ─────────────────────────────────────────────────────────────────────────
# detect_team_min_exceeds_team_size
# ─────────────────────────────────────────────────────────────────────────
def test_team_min_no_false_positive():
    issues = detect_team_min_exceeds_team_size(
        team_min_by_team={1: {"D": 2, "E": 1}, 2: {"D": 1}},
        team_size={1: 5, 2: 4},
    )
    assert issues == []


def test_team_min_catches_excess_per_shift():
    issues = detect_team_min_exceeds_team_size(
        team_min_by_team={1: {"D": 4, "E": 2}, 2: {"D": 6}, 3: {"N": 1}},
        team_size={1: 3, 2: 4, 3: 10},
    )
    # team 1 D 만 excess, team 2 D 만 excess
    assert len(issues) == 2
    teams = {(i["details"]["team_id"], i["details"]["shift"]) for i in issues}
    assert (1, "D") in teams
    assert (2, "D") in teams


# ─────────────────────────────────────────────────────────────────────────
# DYNAMIC SWEEP — feasible 50개 + infeasible 50개
# 정적 fixture 가 아닌 매개변수 변동으로 생성. seed 별 reproducible.
# ─────────────────────────────────────────────────────────────────────────
def _build_feasible(seed: int) -> dict:
    rng = random.Random(seed)
    num_days = rng.randint(28, 31)
    nurse_count = rng.randint(8, 30)
    # demand 합계가 nurse_count 의 70% 이하로 (안전 margin)
    cap = max(1, int(nurse_count * 0.7))
    d = rng.randint(1, max(1, cap // 3))
    e = rng.randint(1, max(1, cap // 3))
    n = rng.randint(1, max(1, cap // 4))
    off_base = 8
    return {
        "daily_shift_requirements": {"D": d, "E": e, "N": n, "O": 0},
        "nurse_count": nurse_count,
        "num_days": num_days,
        "nurse_off_budgets": [
            {"nurse_id": f"N{i}", "off_days_total": rng.randint(6, num_days - 5)}
            for i in range(nurse_count)
        ],
        "monthly_limits": [
            {"nurse_id": f"N{i}", "shift": rng.choice(["N", "D", "E"]),
             "min_val": rng.randint(0, 2), "max_val": rng.randint(3, 8)}
            for i in range(min(8, nurse_count))
        ],
        "team_min_by_team": {
            t: {"D": rng.randint(1, 2), "E": rng.randint(1, 2)}
            for t in range(1, rng.randint(2, 4))
        },
        "team_size": {
            t: rng.randint(4, 8)
            for t in range(1, 5)
        },
    }


def _inject_daily_demand_excess(data: dict, seed: int) -> dict:
    rng = random.Random(seed * 7919)
    data = dict(data)
    data["daily_shift_requirements"] = {
        **data["daily_shift_requirements"],
        "D": data["nurse_count"] + rng.randint(1, 10),
    }
    return data


def _inject_off_budget_excess(data: dict, seed: int) -> dict:
    rng = random.Random(seed * 7919 + 1)
    data = dict(data)
    bad_id = f"BAD{seed}"
    new_budgets = list(data["nurse_off_budgets"])
    new_budgets.append({
        "nurse_id": bad_id,
        "off_days_total": data["num_days"] + rng.randint(1, 5),
    })
    data["nurse_off_budgets"] = new_budgets
    return data


def _inject_monthly_limit_contradiction(data: dict, seed: int) -> dict:
    rng = random.Random(seed * 7919 + 2)
    data = dict(data)
    new_limits = list(data["monthly_limits"])
    new_limits.append({
        "nurse_id": f"BAD{seed}",
        "shift": rng.choice(["N", "D", "E"]),
        "min_val": rng.randint(5, 10),
        "max_val": rng.randint(0, 3),
    })
    data["monthly_limits"] = new_limits
    return data


def _inject_team_min_excess(data: dict, seed: int) -> dict:
    rng = random.Random(seed * 7919 + 3)
    data = dict(data)
    # 한 팀의 한 shift min 을 팀 크기 + 1 이상으로
    t = rng.choice(list(data["team_size"].keys()) or [1])
    sz = int(data["team_size"].get(t, 1))
    new_mins = dict(data["team_min_by_team"])
    new_mins[t] = {**(new_mins.get(t) or {}), "D": sz + rng.randint(1, 5)}
    data["team_min_by_team"] = new_mins
    return data


def test_dynamic_sweep_50_feasible_no_false_positives():
    fp = []
    for seed in range(50):
        data = _build_feasible(seed)
        issues = run_arithmetic_detectors(data)
        if issues:
            fp.append((seed, [i["reason_code"] for i in issues]))
    assert fp == [], f"feasible sweep — {len(fp)}/50 cases triggered false positive. samples: {fp[:3]}"


@pytest.mark.parametrize("injector,expected_code", [
    (_inject_daily_demand_excess, "DAILY_DEMAND_EXCEEDS_NURSE_COUNT"),
    (_inject_off_budget_excess, "OFF_BUDGET_EXCEEDS_NUM_DAYS"),
    (_inject_monthly_limit_contradiction, "MONTHLY_LIMIT_MIN_EXCEEDS_MAX"),
    (_inject_team_min_excess, "TEAM_MIN_EXCEEDS_TEAM_SIZE"),
])
def test_dynamic_sweep_50_infeasible_catches_correct_code(injector, expected_code):
    misses = []
    for seed in range(50):
        data = injector(_build_feasible(seed), seed)
        issues = run_arithmetic_detectors(data)
        codes = {i["reason_code"] for i in issues}
        if expected_code not in codes:
            misses.append((seed, codes))
    assert misses == [], f"{expected_code} — {len(misses)}/50 cases missed. samples: {misses[:3]}"
