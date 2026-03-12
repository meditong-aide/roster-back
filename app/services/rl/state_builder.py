"""RL State 구성 모듈.

State는 총 12차원 연속 벡터:
  [0]  n_nurses_norm          - 간호사 수 / 30
  [1]  n_days_norm            - 일수 / 31
  [2]  night_demand_ratio     - 총 야간 필요 / (N*D)
  [3]  preference_density     - 선호도 행렬 비영 비율
  [4]  fixed_cell_fraction    - 고정 셀 비율
  [5]  n_only_nurse_fraction  - 야간 전담 간호사 비율
  [6]  weekend_fraction       - 주말 일수 비율
  [7]  stage1_short_norm      - Stage1 커버리지 부족 (정규화)
  [8]  stage1_relax_norm      - Stage1 완화 레벨 / 10
  [9]  stage1_time_ratio      - Stage1 시간 사용 비율 (추정)
  [10] stage2_safety_norm     - Stage2 안전 위반 합 (정규화)
  [11] stage2_time_ratio      - Stage2 시간 사용 비율 (추정)

[0:7]  문제 특성 (episode 시작 시 고정)
[7:10] Stage 1 결과 (Stage 1 완료 후 업데이트)
[10:12] Stage 2 결과 (Stage 2 완료 후 업데이트)
"""
from __future__ import annotations

import numpy as np

OBS_DIM = 12


def build_initial_state(
    n_nurses: int,
    n_days: int,
    daily_night_demand: float,
    preference_density: float,
    fixed_cell_fraction: float,
    n_only_nurse_fraction: float,
    weekend_fraction: float,
) -> np.ndarray:
    """문제 특성만으로 초기 state 구성 (Stage 결과 부분은 0)."""
    obs = np.zeros(OBS_DIM, dtype=np.float32)
    obs[0] = np.clip(n_nurses / 30.0, 0.0, 2.0)
    obs[1] = np.clip(n_days / 31.0, 0.0, 1.0)
    obs[2] = np.clip(daily_night_demand, 0.0, 1.0)
    obs[3] = np.clip(preference_density, 0.0, 1.0)
    obs[4] = np.clip(fixed_cell_fraction, 0.0, 1.0)
    obs[5] = np.clip(n_only_nurse_fraction, 0.0, 1.0)
    obs[6] = np.clip(weekend_fraction, 0.0, 1.0)
    return obs


def update_state_after_stage1(
    obs: np.ndarray,
    coverage_short: int,
    relax_level_used: int,
    n_nurses: int,
    n_days: int,
    tl1_used_ratio: float = 1.0,
) -> np.ndarray:
    """Stage 1 완료 후 state 업데이트."""
    obs = obs.copy()
    max_possible_short = n_nurses * n_days * 0.3  # 최대 예상 부족 (정규화 기준)
    obs[7] = np.clip(coverage_short / max(1.0, max_possible_short), 0.0, 1.0)
    obs[8] = np.clip(relax_level_used / 10.0, 0.0, 1.0)
    obs[9] = np.clip(tl1_used_ratio, 0.0, 1.0)
    return obs


def update_state_after_stage2(
    obs: np.ndarray,
    safety_violation_sum: int,
    n_nurses: int,
    n_days: int,
    tl2_used_ratio: float = 1.0,
) -> np.ndarray:
    """Stage 2 완료 후 state 업데이트."""
    obs = obs.copy()
    max_possible_viol = n_nurses * n_days * 0.5
    obs[10] = np.clip(safety_violation_sum / max(1.0, max_possible_viol), 0.0, 1.0)
    obs[11] = np.clip(tl2_used_ratio, 0.0, 1.0)
    return obs


def extract_problem_features_from_scenario(scenario: dict) -> dict:
    """synthetic scenario dict에서 문제 특성 지표를 추출."""
    n_nurses = scenario["n_nurses"]
    n_days = scenario["n_days"]
    demand = scenario.get("daily_requirements", {"D": 2, "E": 2, "N": 2})
    total_demand_per_day = sum(demand.values())
    night_demand_per_day = demand.get("N", 0)

    prefs = scenario.get("preference_matrix", np.zeros((n_nurses, n_days, 4)))
    nonzero = int(np.sum(np.abs(prefs) > 0))
    total_cells = n_nurses * n_days * prefs.shape[2] if prefs.ndim == 3 else 1

    fixed_cells = scenario.get("fixed_cells", [])
    n_only_nurses = sum(1 for nu in scenario.get("nurses", []) if nu.get("is_night_nurse") == "N")
    weekend_count = scenario.get("weekend_count", 8)

    return {
        "n_nurses": n_nurses,
        "n_days": n_days,
        "daily_night_demand": night_demand_per_day / max(1, n_nurses),
        "preference_density": nonzero / max(1, total_cells),
        "fixed_cell_fraction": len(fixed_cells) / max(1, n_nurses * n_days),
        "n_only_nurse_fraction": n_only_nurses / max(1, n_nurses),
        "weekend_fraction": weekend_count / max(1, n_days),
    }
