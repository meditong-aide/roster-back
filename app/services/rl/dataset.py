"""Synthetic Roster Scenario Generator.

실제 DB 없이도 다양한 병동 상황을 모사하는 시나리오를 생성한다.
학습/검증/테스트 split을 지원하며 deterministic seed를 사용한다.

시나리오 유형:
    - small:   간호사 5~8명, 10~15일 (빠른 학습용)
    - medium:  간호사 10~15명, 28~31일 (표준)
    - large:   간호사 20~30명, 28~31일 (복잡한 케이스)
    - stress:  높은 커버리지 요구 + 적은 간호사 (infeasibility 케이스)
    - n_only:  야간 전담 간호사 비율 높음
    - pref_heavy: 선호도가 강한 케이스
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np


@dataclass
class NurseScenario:
    """하나의 근무표 생성 시나리오."""
    scenario_id: str
    scenario_type: str
    n_nurses: int
    n_days: int
    year: int
    month: int
    nurses: list[dict] = field(default_factory=list)
    daily_requirements: dict[str, int] = field(default_factory=dict)
    preference_matrix: Optional[np.ndarray] = None  # shape (N, D, S)
    fixed_cells: list[dict] = field(default_factory=list)
    weekend_count: int = 8
    config_overrides: dict = field(default_factory=dict)
    seed: int = 42
    # 메타 정보 (평가용)
    expected_difficulty: float = 0.5  # 0=쉬움, 1=어려움


def generate_scenario(
    scenario_type: str = "medium",
    seed: int = 42,
    scenario_id: str = "",
) -> NurseScenario:
    """단일 시나리오 생성.

    Args:
        scenario_type: 'small', 'medium', 'large', 'stress', 'n_only', 'pref_heavy'
        seed:          랜덤 시드
        scenario_id:   시나리오 식별자 (빈 문자열이면 자동 생성)

    Returns:
        NurseScenario 인스턴스
    """
    rng = np.random.RandomState(seed)
    py_rng = random.Random(seed)

    if not scenario_id:
        scenario_id = f"{scenario_type}_{seed}"

    # ── 시나리오 유형별 파라미터 ──
    if scenario_type == "small":
        n_nurses = py_rng.randint(5, 9)
        n_days = py_rng.randint(10, 16)
        demand_scale = 0.3
        n_only_frac = 0.0
        pref_density = 0.3
        difficulty = 0.3
        year, month = 2025, 3

    elif scenario_type == "medium":
        n_nurses = py_rng.randint(10, 16)
        n_days = py_rng.randint(28, 32)
        demand_scale = 0.35
        n_only_frac = 0.1
        pref_density = 0.4
        difficulty = 0.5
        year, month = 2025, 3

    elif scenario_type == "large":
        n_nurses = py_rng.randint(20, 31)
        n_days = py_rng.randint(28, 32)
        demand_scale = 0.35
        n_only_frac = 0.15
        pref_density = 0.45
        difficulty = 0.6
        year, month = 2025, 3

    elif scenario_type == "stress":
        n_nurses = py_rng.randint(7, 12)
        n_days = 31
        demand_scale = 0.5  # 높은 커버리지 요구
        n_only_frac = 0.2
        pref_density = 0.2
        difficulty = 0.9
        year, month = 2025, 3

    elif scenario_type == "n_only":
        n_nurses = py_rng.randint(10, 16)
        n_days = py_rng.randint(28, 32)
        demand_scale = 0.35
        n_only_frac = 0.35  # 야간 전담 많음
        pref_density = 0.3
        difficulty = 0.65
        year, month = 2025, 3

    elif scenario_type == "pref_heavy":
        n_nurses = py_rng.randint(10, 15)
        n_days = py_rng.randint(28, 32)
        demand_scale = 0.30
        n_only_frac = 0.1
        pref_density = 0.7  # 선호도 강함
        difficulty = 0.55
        year, month = 2025, 3

    else:
        raise ValueError(f"Unknown scenario_type: {scenario_type}")

    # ── 간호사 생성 ──
    nurses = []
    shift_types = ["D", "E", "N", "O"]
    S = len(shift_types)

    for i in range(n_nurses):
        is_n_only = rng.random() < n_only_frac
        nurse = {
            "nurse_id": f"nurse_{i}",
            "name": f"간호사{i}",
            "experience": py_rng.randint(1, 15),
            "is_night_nurse": "N" if is_n_only else None,
            "joining_date": date(year, month, 1),
            "resignation_date": None,
            "db_id": f"db_{i}",
        }
        nurses.append(nurse)

    # ── 일별 커버리지 요구 ──
    # 간호사 수 기반으로 demand 계산 (대략 D:E:N = 1:1:1 비율)
    base_demand = max(1, int(n_nurses * demand_scale))
    daily_requirements = {
        "D": max(1, base_demand),
        "E": max(1, base_demand),
        "N": max(1, base_demand - 1),
    }

    # ── 선호도 행렬 생성 ──
    P = np.zeros((n_nurses, n_days, S), dtype=np.float32)
    for n in range(n_nurses):
        for d in range(n_days):
            if rng.random() < pref_density:
                # 랜덤하게 1~2개 shift에 선호도 부여
                n_prefs = py_rng.randint(1, 3)
                for _ in range(n_prefs):
                    s = py_rng.randint(0, S - 2)  # O 제외
                    val = py_rng.choice([-1.0, -0.5, 0.5, 1.0])
                    P[n, d, s] = val

    # ── 고정 셀 (특별 근무: 휴가 등) ──
    fixed_cells = []
    n_fixed = int(n_nurses * n_days * 0.03)  # 약 3% 고정
    for _ in range(n_fixed):
        n_idx = py_rng.randint(0, n_nurses - 1)
        d_idx = py_rng.randint(0, n_days - 1)
        fixed_cells.append({
            "nurse_index": n_idx,
            "day_index": d_idx,
            "shift": "O",  # 휴가 등은 OFF로 모사
        })

    # ── 주말 일수 계산 ──
    first_day = date(year, month, 1)
    weekend_count = sum(
        1 for d in range(n_days)
        if (first_day.toordinal() + d) % 7 in (5, 6)  # Sat=5, Sun=6
    )

    # ── config 오버라이드 ──
    config_overrides = {
        "max_night_shifts_per_month": min(10, n_days // 3),
        "max_consecutive_work": 5,
        "off_days": max(7, int(n_days * 0.25)),
        "shift_types": shift_types,
        "daily_shift_requirements": daily_requirements,
    }

    return NurseScenario(
        scenario_id=scenario_id,
        scenario_type=scenario_type,
        n_nurses=n_nurses,
        n_days=n_days,
        year=year,
        month=month,
        nurses=nurses,
        daily_requirements=daily_requirements,
        preference_matrix=P,
        fixed_cells=fixed_cells,
        weekend_count=weekend_count,
        config_overrides=config_overrides,
        seed=seed,
        expected_difficulty=difficulty,
    )


def generate_dataset(
    n_train: int = 200,
    n_val: int = 50,
    n_test: int = 50,
    base_seed: int = 0,
    scenario_types: list[str] | None = None,
) -> dict[str, list[NurseScenario]]:
    """학습/검증/테스트용 시나리오 데이터셋 생성.

    Args:
        n_train:         학습 시나리오 수
        n_val:           검증 시나리오 수
        n_test:          테스트 시나리오 수
        base_seed:       기본 시드 (split 재현성 보장)
        scenario_types:  사용할 시나리오 유형 리스트 (None이면 전부)

    Returns:
        {'train': [...], 'val': [...], 'test': [...]}
    """
    if scenario_types is None:
        scenario_types = ["small", "medium", "large", "stress", "n_only", "pref_heavy"]

    def make_split(n: int, split_name: str, offset: int) -> list[NurseScenario]:
        scenarios = []
        for i in range(n):
            stype = scenario_types[i % len(scenario_types)]
            seed = base_seed + offset + i
            sid = f"{split_name}_{stype}_{seed}"
            scenarios.append(generate_scenario(stype, seed=seed, scenario_id=sid))
        return scenarios

    return {
        "train": make_split(n_train, "train", 0),
        "val": make_split(n_val, "val", n_train),
        "test": make_split(n_test, "test", n_train + n_val),
    }
