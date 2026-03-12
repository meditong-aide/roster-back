"""RL Reward 계산 모듈.

Reward 구조:
    R = w_cov  * coverage_reward
      + w_safe * safety_reward
      + w_sat  * satisfaction_reward
      + w_fair * fairness_reward
      - w_time * time_penalty

Hard 제약 위반 시:
    - coverage_short > 0: 큰 패널티 (-1000 * short_normalized)
    - safety_violation > 0: 패널티 (-500 * viol_normalized)

Soft 목적 달성 시:
    - preference satisfaction: 0~1 정규화 점수
    - night fairness (편차 최소화): 0~1 정규화

논문 주장 가능성:
    이 reward 구조는 기존 lexicographic optimization의 순위를 반영하면서도,
    RL이 stage별 시간 예산을 조절해 최종 품질을 향상시킬 수 있음을 보인다.
"""
from __future__ import annotations

import numpy as np

# Reward 가중치
W_COVERAGE = 5.0    # 커버리지 달성 중요도 (최고)
W_SAFETY = 3.0      # 안전 위반 방지
W_SATISFACTION = 1.0  # 선호 만족도
W_FAIRNESS = 0.5    # 공정성 (야간 균등)
W_TIME = 0.05       # 시간 사용 패널티 (약함)

# 하드 위반 페널티
COVERAGE_VIOLATION_PENALTY = -200.0
SAFETY_VIOLATION_PENALTY = -100.0

# 에피소드 성공 보너스
FEASIBILITY_BONUS = 50.0


def compute_reward(
    coverage_short: int,
    safety_violation_sum: int,
    preference_score: float,
    night_fairness_score: float,
    solve_time_ratio: float,
    n_nurses: int,
    n_days: int,
) -> tuple[float, dict]:
    """최종 근무표 품질 기반 reward 계산.

    Args:
        coverage_short:        총 커버리지 부족 수
        safety_violation_sum:  총 안전 위반 합
        preference_score:      선호 만족 점수 (0~1 정규화)
        night_fairness_score:  야간 균등성 점수 (0~1, 1이 완전 균등)
        solve_time_ratio:      실제 소요시간 / 허용시간 (0~1)
        n_nurses:              간호사 수 (정규화 기준)
        n_days:                일수 (정규화 기준)

    Returns:
        (total_reward, reward_components_dict)
    """
    max_possible_short = n_nurses * n_days * 0.3

    # 커버리지 reward: short=0 이면 +W_COVERAGE, 클수록 페널티
    if coverage_short == 0:
        cov_reward = W_COVERAGE + FEASIBILITY_BONUS * 0.5
    else:
        short_ratio = coverage_short / max(1.0, max_possible_short)
        cov_reward = COVERAGE_VIOLATION_PENALTY * short_ratio

    # 안전 위반 reward
    max_viol = n_nurses * n_days * 0.2
    if safety_violation_sum == 0:
        safe_reward = W_SAFETY
    else:
        viol_ratio = safety_violation_sum / max(1.0, max_viol)
        safe_reward = SAFETY_VIOLATION_PENALTY * viol_ratio

    # 선호 만족 reward (0~1 정규화 전제)
    sat_reward = W_SATISFACTION * np.clip(preference_score, 0.0, 1.0)

    # 야간 공정성 reward
    fair_reward = W_FAIRNESS * np.clip(night_fairness_score, 0.0, 1.0)

    # 시간 패널티 (너무 빨리 끝나는 것도 약간 패널티 - 탐색 미흡 방지)
    time_penalty = -W_TIME * max(0.0, 1.0 - solve_time_ratio)

    total = cov_reward + safe_reward + sat_reward + fair_reward + time_penalty

    components = {
        "coverage_reward": cov_reward,
        "safety_reward": safe_reward,
        "satisfaction_reward": sat_reward,
        "fairness_reward": fair_reward,
        "time_penalty": time_penalty,
        "total": total,
    }
    return float(total), components


def compute_night_fairness_score(
    night_counts: list[int],
    n_only_mask: list[bool] | None = None,
) -> float:
    """야간 근무 수 편차 기반 공정성 점수 계산.

    편차가 0이면 1.0, 클수록 0에 가까워짐.
    N 전담 간호사는 분리해서 계산.

    Args:
        night_counts: 간호사별 야간 근무 횟수 리스트
        n_only_mask:  True면 야간 전담 간호사

    Returns:
        0~1 공정성 점수
    """
    if not night_counts:
        return 1.0

    mask = n_only_mask or [False] * len(night_counts)
    regular = [c for c, m in zip(night_counts, mask) if not m]
    n_only = [c for c, m in zip(night_counts, mask) if m]

    score = 0.0
    total_groups = 0

    for group in [g for g in [regular, n_only] if g]:
        if len(group) < 2:
            score += 1.0
        else:
            std = np.std(group)
            mean = np.mean(group)
            cv = std / max(1.0, mean)  # coefficient of variation
            score += max(0.0, 1.0 - cv)
        total_groups += 1

    return score / max(1, total_groups)


def compute_preference_satisfaction_score(
    preference_matrix: np.ndarray,
    roster: np.ndarray,
) -> float:
    """선호도 매트릭스와 실제 배정 결과 기반 선호 만족 점수.

    Args:
        preference_matrix: shape (N, D, S), 값 -1~1
        roster:            shape (N, D, S), 0/1 이진

    Returns:
        0~1 정규화된 선호 만족 점수
    """
    if preference_matrix is None or roster is None:
        return 0.5

    try:
        # 실제 배정된 셀의 선호도 합
        actual_score = float(np.sum(preference_matrix * roster))

        # 최대 가능 점수: 각 (n,d)에서 선호도가 가장 높은 shift 선택
        max_score = float(np.sum(np.maximum(0, preference_matrix.max(axis=2))))
        min_score = float(np.sum(np.minimum(0, preference_matrix.min(axis=2))))

        if max_score <= min_score:
            return 0.5

        normalized = (actual_score - min_score) / (max_score - min_score)
        return float(np.clip(normalized, 0.0, 1.0))
    except Exception:
        return 0.5
