"""Dynamic scenario generator — combinatorial, parameterized, seed-deterministic.

설계 원칙 (사용자 핵심 요구 '정적 fixture 과적합 금지'):
  - seed 고정 시 reproducible. seed 다르면 nurse_count / team / grade / demand 변동.
  - 의도적 violation 주입 가능: violation_kind 파라미터로 어떤 모순을 만들지 지정.
  - 모든 병원/병동/간호사 수 분포 generalization 가능.

VIOLATION_KINDS:
  feasible                       — 위반 없는 입력 (false-positive 검증용)
  daily_demand_excess            — 일 수요 > 간호사 수
  off_budget_excess              — 한 간호사 OFF 예산 > num_days
  monthly_limit_contradiction    — nurse_monthly_limit min > max
  team_min_excess                — team_min > team_size
  complex_2                      — 위 2 가지 동시 (compound)
  complex_3                      — 위 3 가지 동시
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any


VIOLATION_KINDS = (
    "feasible",
    "daily_demand_excess",
    "off_budget_excess",
    "monthly_limit_contradiction",
    "team_min_excess",
    "complex_2",
    "complex_3",
)


@dataclass(slots=True)
class ScenarioParams:
    seed: int
    nurse_count: int = 0    # 0 → randomized
    team_count: int = 0
    grade_count: int = 3
    num_days: int = 0
    violation_kind: str = "feasible"


@dataclass(slots=True)
class GeneratedScenario:
    params: ScenarioParams
    input_data: dict[str, Any]
    expected_causes: list[str]
    description: str


def generate_scenario(params: ScenarioParams) -> GeneratedScenario:
    rng = random.Random(params.seed)
    nurse_count = params.nurse_count or rng.randint(8, 30)
    team_count = params.team_count or rng.randint(2, max(2, nurse_count // 4))
    num_days = params.num_days or rng.randint(28, 31)
    grade_count = max(1, params.grade_count)

    # base feasible — demand 가 nurse_count 의 70% 이하로
    cap_per_day = max(2, int(nurse_count * 0.7))
    d = rng.randint(1, max(1, cap_per_day // 3))
    e = rng.randint(1, max(1, cap_per_day // 3))
    n = rng.randint(1, max(1, cap_per_day // 4))

    # team_size 분배 (각 팀 최소 2명)
    team_size: dict[Any, int] = {}
    remaining = nurse_count
    for t in range(1, team_count + 1):
        if t == team_count:
            team_size[t] = max(2, remaining)
        else:
            max_for_this = max(2, remaining - 2 * (team_count - t))
            s = rng.randint(2, max(2, max_for_this))
            team_size[t] = s
            remaining = max(0, remaining - s)

    team_min_by_team = {
        t: {"D": rng.randint(1, max(1, sz // 3)),
            "E": rng.randint(1, max(1, sz // 3))}
        for t, sz in team_size.items()
    }

    nurse_off_budgets = [
        {"nurse_id": f"N{i}", "off_days_total": rng.randint(6, max(7, num_days - 6))}
        for i in range(nurse_count)
    ]

    monthly_limits = [
        {"nurse_id": f"N{i}", "shift": rng.choice(["N", "D", "E"]),
         "min_val": rng.randint(0, 2), "max_val": rng.randint(3, 8)}
        for i in range(min(8, nurse_count))
    ]

    grade_constraints = {
        "constraints_max_json": {
            "N": {g: rng.randint(2, max(2, nurse_count // grade_count + 1))
                  for g in range(1, grade_count + 1)},
            "D": {g: rng.randint(2, max(2, nurse_count // grade_count + 1))
                  for g in range(1, grade_count + 1)},
        },
        "constraints_json": {
            "N": {g: rng.randint(0, 1) for g in range(1, grade_count + 1)},
            "D": {g: rng.randint(0, 1) for g in range(1, grade_count + 1)},
        },
    }

    input_data: dict[str, Any] = {
        "daily_shift_requirements": {"D": d, "E": e, "N": n, "O": 0},
        "nurse_count": nurse_count,
        "num_days": num_days,
        "nurse_off_budgets": nurse_off_budgets,
        "monthly_limits": monthly_limits,
        "team_min_by_team": team_min_by_team,
        "team_size": team_size,
        "grade_constraints": grade_constraints,
        "grade_count": grade_count,
    }

    expected: list[str] = []
    desc = "feasible baseline"

    kind = params.violation_kind
    if kind == "feasible":
        pass

    elif kind == "daily_demand_excess":
        input_data["daily_shift_requirements"]["D"] = nurse_count + rng.randint(1, 5)
        expected.append("DAILY_DEMAND_EXCEEDS_NURSE_COUNT")
        desc = f"daily demand D={input_data['daily_shift_requirements']['D']} > nurse_count={nurse_count}"

    elif kind == "off_budget_excess":
        nurse_off_budgets.append({
            "nurse_id": "BAD",
            "off_days_total": num_days + rng.randint(1, 5),
        })
        expected.append("OFF_BUDGET_EXCEEDS_NUM_DAYS")
        desc = f"off budget BAD={nurse_off_budgets[-1]['off_days_total']} > num_days={num_days}"

    elif kind == "monthly_limit_contradiction":
        monthly_limits.append({
            "nurse_id": "BAD",
            "shift": "N",
            "min_val": rng.randint(5, 10),
            "max_val": rng.randint(0, 3),
        })
        expected.append("MONTHLY_LIMIT_MIN_EXCEEDS_MAX")
        desc = f"monthly_limit BAD N min={monthly_limits[-1]['min_val']} > max={monthly_limits[-1]['max_val']}"

    elif kind == "team_min_excess":
        target = rng.choice(list(team_size.keys()))
        sz = team_size[target]
        team_min_by_team[target] = {"D": sz + rng.randint(1, 3), "E": team_min_by_team[target].get("E", 1)}
        expected.append("TEAM_MIN_EXCEEDS_TEAM_SIZE")
        desc = f"team {target} min D={team_min_by_team[target]['D']} > size={sz}"

    elif kind == "complex_2":
        input_data["daily_shift_requirements"]["D"] = nurse_count + rng.randint(1, 3)
        nurse_off_budgets.append({
            "nurse_id": "BAD",
            "off_days_total": num_days + rng.randint(1, 5),
        })
        expected.extend(["DAILY_DEMAND_EXCEEDS_NURSE_COUNT", "OFF_BUDGET_EXCEEDS_NUM_DAYS"])
        desc = "complex 2: daily + off"

    elif kind == "complex_3":
        input_data["daily_shift_requirements"]["D"] = nurse_count + rng.randint(1, 3)
        nurse_off_budgets.append({
            "nurse_id": "BAD",
            "off_days_total": num_days + rng.randint(1, 5),
        })
        monthly_limits.append({
            "nurse_id": "BAD2",
            "shift": "N",
            "min_val": rng.randint(5, 10),
            "max_val": rng.randint(0, 3),
        })
        expected.extend([
            "DAILY_DEMAND_EXCEEDS_NURSE_COUNT",
            "OFF_BUDGET_EXCEEDS_NUM_DAYS",
            "MONTHLY_LIMIT_MIN_EXCEEDS_MAX",
        ])
        desc = "complex 3: daily + off + monthly_limit"

    else:
        raise ValueError(f"unknown violation_kind: {kind}")

    return GeneratedScenario(
        params=params,
        input_data=input_data,
        expected_causes=expected,
        description=desc,
    )
