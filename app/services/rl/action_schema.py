"""RL Action Space 정의.

Action은 solver 전략을 결정하는 다차원 이산 공간:
- stage1_frac:   Stage 1 (coverage) 에 배분할 시간 비율 인덱스
- stage2_frac:   Stage 2 (safety)   에 배분할 시간 비율 인덱스
- night_w_mult:  야간 균등화 가중치 배수 인덱스
- exp_w_mult:    경력자 부족 패널티 배수 인덱스

총 조합: 5 × 4 × 4 × 3 = 240

RL 명분:
- 문제 특성(간호사 수, 커버리지 난이도, 야간 전담 비율 등)에 따라
  Stage별 최적 시간 배분이 달라진다.
- 이는 전형적인 contextual sequential decision 문제이며,
  학습된 policy가 rule-based 고정 배분(45/35/20%)보다 우수할 수 있다.
"""
from __future__ import annotations

import numpy as np

# ── Stage 1 시간 비율 후보 (전체 time_limit 대비) ──
STAGE1_FRACTIONS = [0.30, 0.40, 0.45, 0.50, 0.60]

# ── Stage 2 시간 비율 후보 ──
STAGE2_FRACTIONS = [0.20, 0.30, 0.35, 0.45]

# ── 야간 균등화 가중치 배수 후보 ──
NIGHT_WEIGHT_MULTS = [0.5, 1.0, 2.0, 4.0]

# ── 경력자 부족 패널티 배수 후보 ──
EXP_WEIGHT_MULTS = [0.5, 1.0, 2.0]

# MultiDiscrete nvec
ACTION_NVEC = [
    len(STAGE1_FRACTIONS),
    len(STAGE2_FRACTIONS),
    len(NIGHT_WEIGHT_MULTS),
    len(EXP_WEIGHT_MULTS),
]

N_ACTIONS_TOTAL = 1
for n in ACTION_NVEC:
    N_ACTIONS_TOTAL *= n  # 240


def decode_action(action: np.ndarray | list | int) -> dict:
    """MultiDiscrete action 벡터를 solver 파라미터 dict로 변환.

    Args:
        action: 길이 4 정수 배열 [s1_idx, s2_idx, night_idx, exp_idx]
                OR 단일 정수 (flattened index)

    Returns:
        dict with keys:
            stage1_frac, stage2_frac, night_weight_mult, exp_weight_mult
    """
    if isinstance(action, (int, np.integer)):
        # flattened → multi-dimensional
        idx = int(action)
        s1 = idx % len(STAGE1_FRACTIONS)
        idx //= len(STAGE1_FRACTIONS)
        s2 = idx % len(STAGE2_FRACTIONS)
        idx //= len(STAGE2_FRACTIONS)
        nw = idx % len(NIGHT_WEIGHT_MULTS)
        idx //= len(NIGHT_WEIGHT_MULTS)
        ew = idx % len(EXP_WEIGHT_MULTS)
        action = [s1, s2, nw, ew]

    s1_idx, s2_idx, nw_idx, ew_idx = int(action[0]), int(action[1]), int(action[2]), int(action[3])
    return {
        "stage1_frac": STAGE1_FRACTIONS[s1_idx],
        "stage2_frac": STAGE2_FRACTIONS[s2_idx],
        "night_weight_mult": NIGHT_WEIGHT_MULTS[nw_idx],
        "exp_weight_mult": EXP_WEIGHT_MULTS[ew_idx],
    }


def encode_action(
    stage1_frac: float = 0.45,
    stage2_frac: float = 0.35,
    night_weight_mult: float = 1.0,
    exp_weight_mult: float = 1.0,
) -> np.ndarray:
    """파라미터 값 → MultiDiscrete action 인덱스 (가장 가까운 후보 선택)."""
    s1 = int(np.argmin([abs(f - stage1_frac) for f in STAGE1_FRACTIONS]))
    s2 = int(np.argmin([abs(f - stage2_frac) for f in STAGE2_FRACTIONS]))
    nw = int(np.argmin([abs(m - night_weight_mult) for m in NIGHT_WEIGHT_MULTS]))
    ew = int(np.argmin([abs(m - exp_weight_mult) for m in EXP_WEIGHT_MULTS]))
    return np.array([s1, s2, nw, ew], dtype=np.int64)


# 기본(baseline) action: 현재 하드코딩과 동일한 45/35/1.0/1.0
DEFAULT_ACTION = encode_action(stage1_frac=0.45, stage2_frac=0.35)
