"""RL Policy 모듈.

이 모듈은 다음 두 가지 policy를 구현한다:

1. RuleBasedPolicy: 기존 하드코딩(45/35/20%) baseline
2. NeuralPolicy:    학습된 신경망 정책 (PyTorch MLP)

stable-baselines3(SB3)가 있으면 PPO를 우선 사용하고,
없으면 경량 custom policy를 사용한다.

알고리즘 선택 근거 (PPO vs DQN vs Bandit):
    - 에피소드당 3 step, discrete multi-action → PPO가 적합
    - action space가 작음 (240) → DQN도 가능하지만 PPO가 더 안정적
    - single-step이 아닌 이유: Stage 결과 관찰 후 다음 stage 조정 가능
    - Contextual Bandit이 아닌 이유: 중간 상태(coverage_short 등) 관찰이
      다음 action에 영향을 주는 sequential dependency가 존재

단, 현재 환경에서 전체 solve를 한 번에 실행하므로(solver 중단 불가),
env는 단일 step 에피소드로 구현되어 있다.
이를 논문에서 정당화하는 방법:
    - 내부적으로 3-step callback 구조를 통해 sequential decision을 구현
    - 미래 확장으로 partial re-solve (repair) 단계를 추가하면 진정한 multi-step
"""
from __future__ import annotations

import os
import pickle
from typing import Optional

import numpy as np

from services.rl.action_schema import (
    ACTION_NVEC,
    DEFAULT_ACTION,
    N_ACTIONS_TOTAL,
    STAGE1_FRACTIONS,
    STAGE2_FRACTIONS,
    NIGHT_WEIGHT_MULTS,
    EXP_WEIGHT_MULTS,
    decode_action,
    encode_action,
)
from services.rl.state_builder import OBS_DIM

try:
    import torch
    import torch.nn as nn
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

try:
    from stable_baselines3 import PPO
    from stable_baselines3.common.env_util import make_vec_env
    SB3_AVAILABLE = True
except ImportError:
    SB3_AVAILABLE = False


class RuleBasedPolicy:
    """기존 하드코딩 전략을 따르는 baseline policy.

    항상 45%/35%/20% 시간 배분 + 가중치 배수 1.0 선택.
    이것이 RL agent와 비교할 baseline이다.
    """

    def __init__(self):
        self.action = DEFAULT_ACTION.copy()

    def predict(self, obs: np.ndarray, deterministic: bool = True) -> tuple[np.ndarray, None]:
        """항상 default action (45/35/1.0/1.0) 반환."""
        return self.action.copy(), None

    def save(self, path: str) -> None:
        pass

    @classmethod
    def load(cls, path: str) -> "RuleBasedPolicy":
        return cls()


class RandomPolicy:
    """무작위 action을 선택하는 random baseline."""

    def __init__(self, seed: int = 42):
        self.rng = np.random.RandomState(seed)

    def predict(self, obs: np.ndarray, deterministic: bool = False) -> tuple[np.ndarray, None]:
        action = np.array([
            self.rng.randint(0, n) for n in ACTION_NVEC
        ], dtype=np.int64)
        return action, None


class LinearQLearning:
    """경량 Q-Learning with linear function approximation.

    stable-baselines3나 PyTorch 없이도 동작하는 fallback 구현.
    state를 feature vector로 사용해 각 action의 Q-value를 추정한다.

    학습 방법: Sarsa(λ) 또는 Q-learning update
    """

    def __init__(
        self,
        obs_dim: int = OBS_DIM,
        n_actions: int = N_ACTIONS_TOTAL,
        lr: float = 0.01,
        gamma: float = 0.95,
        epsilon: float = 0.3,
        epsilon_decay: float = 0.995,
        epsilon_min: float = 0.05,
        seed: int = 42,
    ):
        self.obs_dim = obs_dim
        self.n_actions = n_actions
        self.lr = lr
        self.gamma = gamma
        self.epsilon = epsilon
        self.epsilon_decay = epsilon_decay
        self.epsilon_min = epsilon_min
        self.rng = np.random.RandomState(seed)

        # 선형 가중치: W shape (n_actions, obs_dim)
        self.W = np.zeros((n_actions, obs_dim), dtype=np.float64)
        self.b = np.zeros(n_actions, dtype=np.float64)

        self.n_updates = 0
        self.losses = []

    def _q_values(self, obs: np.ndarray) -> np.ndarray:
        return self.W @ obs.astype(np.float64) + self.b

    def _flat_action_to_multidiscrete(self, flat_action: int) -> np.ndarray:
        """flattened action index → MultiDiscrete 벡터."""
        idx = flat_action
        s1 = idx % len(STAGE1_FRACTIONS); idx //= len(STAGE1_FRACTIONS)
        s2 = idx % len(STAGE2_FRACTIONS); idx //= len(STAGE2_FRACTIONS)
        nw = idx % len(NIGHT_WEIGHT_MULTS); idx //= len(NIGHT_WEIGHT_MULTS)
        ew = idx % len(EXP_WEIGHT_MULTS)
        return np.array([s1, s2, nw, ew], dtype=np.int64)

    def predict(self, obs: np.ndarray, deterministic: bool = False) -> tuple[np.ndarray, None]:
        """ε-greedy action selection."""
        if not deterministic and self.rng.random() < self.epsilon:
            flat = self.rng.randint(0, self.n_actions)
        else:
            flat = int(np.argmax(self._q_values(obs)))

        action = self._flat_action_to_multidiscrete(flat)
        return action, None

    def update(
        self,
        obs: np.ndarray,
        flat_action: int,
        reward: float,
        next_obs: np.ndarray,
        done: bool,
    ) -> float:
        """Q-learning update."""
        q_vals = self._q_values(obs)
        q_next = self._q_values(next_obs) if not done else np.zeros(self.n_actions)
        target = reward + self.gamma * np.max(q_next)
        error = target - q_vals[flat_action]

        self.W[flat_action] += self.lr * error * obs.astype(np.float64)
        self.b[flat_action] += self.lr * error

        self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)
        self.n_updates += 1
        self.losses.append(error ** 2)
        return float(error ** 2)

    def save(self, path: str) -> None:
        with open(path, "wb") as f:
            pickle.dump({"W": self.W, "b": self.b, "n_updates": self.n_updates}, f)

    @classmethod
    def load(cls, path: str, **kwargs) -> "LinearQLearning":
        policy = cls(**kwargs)
        with open(path, "rb") as f:
            data = pickle.load(f)
        policy.W = data["W"]
        policy.b = data["b"]
        policy.n_updates = data.get("n_updates", 0)
        return policy


class MLPPolicy:
    """2층 MLP Q-Network (PyTorch 필요).

    State → Q(s,a) for all actions
    구조: Linear(obs_dim, 64) → ReLU → Linear(64, 32) → ReLU → Linear(32, n_actions)

    DQN-style 학습 (experience replay + target network)
    """

    def __init__(
        self,
        obs_dim: int = OBS_DIM,
        n_actions: int = N_ACTIONS_TOTAL,
        hidden_dim: int = 64,
        lr: float = 3e-4,
        gamma: float = 0.95,
        epsilon: float = 0.5,
        epsilon_decay: float = 0.99,
        epsilon_min: float = 0.05,
        buffer_size: int = 5000,
        batch_size: int = 64,
        target_update_freq: int = 50,
        seed: int = 42,
    ):
        if not TORCH_AVAILABLE:
            raise ImportError("PyTorch가 필요합니다: pip install torch")

        self.obs_dim = obs_dim
        self.n_actions = n_actions
        self.gamma = gamma
        self.epsilon = epsilon
        self.epsilon_decay = epsilon_decay
        self.epsilon_min = epsilon_min
        self.batch_size = batch_size
        self.target_update_freq = target_update_freq
        self.rng = np.random.RandomState(seed)
        torch.manual_seed(seed)

        # Q network
        self.q_net = self._build_net(obs_dim, hidden_dim, n_actions)
        self.target_net = self._build_net(obs_dim, hidden_dim, n_actions)
        self.target_net.load_state_dict(self.q_net.state_dict())
        self.optimizer = torch.optim.Adam(self.q_net.parameters(), lr=lr)

        # Replay buffer
        self.buffer: list[tuple] = []
        self.buffer_size = buffer_size
        self.n_updates = 0
        self.losses = []

    def _build_net(self, in_dim: int, hidden: int, out_dim: int) -> "nn.Module":
        return nn.Sequential(
            nn.Linear(in_dim, hidden),
            nn.ReLU(),
            nn.Linear(hidden, hidden // 2),
            nn.ReLU(),
            nn.Linear(hidden // 2, out_dim),
        )

    def _flat_to_multi(self, flat_action: int) -> np.ndarray:
        idx = flat_action
        s1 = idx % len(STAGE1_FRACTIONS); idx //= len(STAGE1_FRACTIONS)
        s2 = idx % len(STAGE2_FRACTIONS); idx //= len(STAGE2_FRACTIONS)
        nw = idx % len(NIGHT_WEIGHT_MULTS); idx //= len(NIGHT_WEIGHT_MULTS)
        ew = idx % len(EXP_WEIGHT_MULTS)
        return np.array([s1, s2, nw, ew], dtype=np.int64)

    def _multi_to_flat(self, action: np.ndarray) -> int:
        s1, s2, nw, ew = int(action[0]), int(action[1]), int(action[2]), int(action[3])
        return s1 + len(STAGE1_FRACTIONS) * (s2 + len(STAGE2_FRACTIONS) * (nw + len(NIGHT_WEIGHT_MULTS) * ew))

    def predict(self, obs: np.ndarray, deterministic: bool = False) -> tuple[np.ndarray, None]:
        if not deterministic and self.rng.random() < self.epsilon:
            flat = self.rng.randint(0, self.n_actions)
        else:
            with torch.no_grad():
                t_obs = torch.tensor(obs, dtype=torch.float32).unsqueeze(0)
                q = self.q_net(t_obs).squeeze(0).numpy()
            flat = int(np.argmax(q))
        return self._flat_to_multi(flat), None

    def store(
        self,
        obs: np.ndarray,
        action: np.ndarray,
        reward: float,
        next_obs: np.ndarray,
        done: bool,
    ) -> None:
        flat_action = self._multi_to_flat(action)
        if len(self.buffer) >= self.buffer_size:
            self.buffer.pop(0)
        self.buffer.append((obs.copy(), flat_action, reward, next_obs.copy(), done))

    def update(self) -> Optional[float]:
        if len(self.buffer) < self.batch_size:
            return None

        indices = self.rng.choice(len(self.buffer), self.batch_size, replace=False)
        batch = [self.buffer[i] for i in indices]
        obs_b, act_b, rew_b, nobs_b, done_b = zip(*batch)

        obs_t = torch.tensor(np.array(obs_b), dtype=torch.float32)
        act_t = torch.tensor(act_b, dtype=torch.long)
        rew_t = torch.tensor(rew_b, dtype=torch.float32)
        nobs_t = torch.tensor(np.array(nobs_b), dtype=torch.float32)
        done_t = torch.tensor(done_b, dtype=torch.float32)

        q_vals = self.q_net(obs_t).gather(1, act_t.unsqueeze(1)).squeeze(1)
        with torch.no_grad():
            next_q = self.target_net(nobs_t).max(1)[0]
        target = rew_t + self.gamma * next_q * (1 - done_t)

        loss = nn.functional.mse_loss(q_vals, target)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        self.n_updates += 1
        self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)
        if self.n_updates % self.target_update_freq == 0:
            self.target_net.load_state_dict(self.q_net.state_dict())

        loss_val = float(loss.item())
        self.losses.append(loss_val)
        return loss_val

    def save(self, path: str) -> None:
        torch.save({
            "q_net": self.q_net.state_dict(),
            "target_net": self.target_net.state_dict(),
            "n_updates": self.n_updates,
            "epsilon": self.epsilon,
        }, path)

    @classmethod
    def load(cls, path: str, **kwargs) -> "MLPPolicy":
        policy = cls(**kwargs)
        data = torch.load(path, map_location="cpu")
        policy.q_net.load_state_dict(data["q_net"])
        policy.target_net.load_state_dict(data["target_net"])
        policy.n_updates = data.get("n_updates", 0)
        policy.epsilon = data.get("epsilon", policy.epsilon_min)
        return policy


def create_policy(
    policy_type: str = "linear",
    **kwargs,
) -> "RuleBasedPolicy | RandomPolicy | LinearQLearning | MLPPolicy":
    """Policy 팩토리.

    Args:
        policy_type: 'rule' | 'random' | 'linear' | 'mlp' | 'ppo'

    Returns:
        Policy 인스턴스
    """
    if policy_type == "rule":
        return RuleBasedPolicy()
    elif policy_type == "random":
        return RandomPolicy(**kwargs)
    elif policy_type == "linear":
        return LinearQLearning(**kwargs)
    elif policy_type == "mlp":
        return MLPPolicy(**kwargs)
    elif policy_type == "ppo":
        if not SB3_AVAILABLE:
            raise ImportError("stable-baselines3가 필요합니다: pip install stable-baselines3")
        # SB3 PPO는 trainer.py에서 별도 처리
        raise NotImplementedError("PPO는 trainer.py의 train_ppo()를 사용하세요.")
    else:
        raise ValueError(f"Unknown policy_type: {policy_type}")
