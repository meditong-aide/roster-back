"""RL 학습 루프.

사용 가능한 학습 방법:
1. Linear Q-Learning (빠름, PyTorch 불필요)
2. DQN with MLP (보통, PyTorch 필요)
3. PPO via stable-baselines3 (표준, SB3 필요)

학습 순서:
    dataset.py → scenarios → env.py → policy.py → trainer.py

실행 예시:
    python -m services.rl.trainer \
        --policy linear \
        --n_episodes 500 \
        --time_limit 15 \
        --use_sim \
        --save_path /tmp/rl_policy.pkl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

import numpy as np

# 경로 설정 (직접 실행 시)
_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(os.path.dirname(_HERE))
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from services.rl.dataset import generate_dataset, generate_scenario
from services.rl.env import RosterSolvingEnv
from services.rl.policy import (
    LinearQLearning,
    MLPPolicy,
    RuleBasedPolicy,
    RandomPolicy,
    create_policy,
)


def train_linear_q(
    env: RosterSolvingEnv,
    policy: LinearQLearning,
    n_episodes: int = 200,
    log_interval: int = 20,
    save_path: Optional[str] = None,
) -> dict:
    """Linear Q-Learning 학습 루프.

    Args:
        env:          학습 환경
        policy:       LinearQLearning policy
        n_episodes:   총 에피소드 수
        log_interval: 로그 출력 주기
        save_path:    체크포인트 저장 경로

    Returns:
        학습 통계 dict
    """
    from services.rl.action_schema import N_ACTIONS_TOTAL, STAGE1_FRACTIONS, STAGE2_FRACTIONS, NIGHT_WEIGHT_MULTS, EXP_WEIGHT_MULTS

    rewards = []
    losses = []
    episode_infos = []

    print(f"[Trainer] Linear Q-Learning 시작: {n_episodes} episodes")
    print(f"[Trainer] 환경: time_limit={env.time_limit_per_solve}s, use_real_solver={env.use_real_solver}")

    t_start = time.time()
    for ep in range(n_episodes):
        obs, info = env.reset()
        done = False
        ep_reward = 0.0
        ep_loss = 0.0
        n_steps = 0

        while not done:
            action, _ = policy.predict(obs, deterministic=False)
            next_obs, reward, done, truncated, step_info = env.step(action)

            # action → flat index
            s1, s2, nw, ew = int(action[0]), int(action[1]), int(action[2]), int(action[3])
            flat = s1 + len(STAGE1_FRACTIONS) * (s2 + len(STAGE2_FRACTIONS) * (nw + len(NIGHT_WEIGHT_MULTS) * ew))

            loss = policy.update(obs, flat, reward, next_obs, done)
            obs = next_obs
            ep_reward += reward
            ep_loss += loss if loss is not None else 0.0
            n_steps += 1

        rewards.append(ep_reward)
        losses.append(ep_loss / max(1, n_steps))
        episode_infos.append(step_info)

        if (ep + 1) % log_interval == 0:
            mean_r = np.mean(rewards[-log_interval:])
            mean_l = np.mean(losses[-log_interval:])
            feasible_rate = sum(
                1 for inf in episode_infos[-log_interval:] if inf.get("feasible", False)
            ) / log_interval
            print(
                f"[Trainer] ep={ep+1}/{n_episodes} "
                f"mean_reward={mean_r:.2f} mean_loss={mean_l:.4f} "
                f"feasible_rate={feasible_rate:.1%} epsilon={policy.epsilon:.3f}"
            )

    elapsed = time.time() - t_start
    print(f"[Trainer] 학습 완료: {elapsed:.1f}s, final epsilon={policy.epsilon:.3f}")

    if save_path:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        policy.save(save_path)
        print(f"[Trainer] Policy 저장: {save_path}")

    return {
        "rewards": rewards,
        "losses": losses,
        "n_episodes": n_episodes,
        "elapsed": elapsed,
    }


def train_dqn(
    env: RosterSolvingEnv,
    policy: MLPPolicy,
    n_episodes: int = 500,
    log_interval: int = 50,
    warmup_episodes: int = 50,
    save_path: Optional[str] = None,
) -> dict:
    """DQN (MLP) 학습 루프.

    Args:
        env:               학습 환경
        policy:            MLPPolicy
        n_episodes:        총 에피소드 수
        log_interval:      로그 출력 주기
        warmup_episodes:   replay buffer warm-up 에피소드 수
        save_path:         체크포인트 저장 경로

    Returns:
        학습 통계 dict
    """
    rewards = []
    losses = []
    episode_infos = []

    print(f"[Trainer] DQN 학습 시작: {n_episodes} episodes (warmup={warmup_episodes})")

    t_start = time.time()
    for ep in range(n_episodes):
        obs, info = env.reset()
        done = False
        ep_reward = 0.0
        ep_loss_vals = []

        while not done:
            action, _ = policy.predict(obs, deterministic=False)
            next_obs, reward, done, truncated, step_info = env.step(action)
            policy.store(obs, action, reward, next_obs, done)

            if ep >= warmup_episodes:
                loss = policy.update()
                if loss is not None:
                    ep_loss_vals.append(loss)

            obs = next_obs
            ep_reward += reward

        rewards.append(ep_reward)
        losses.append(np.mean(ep_loss_vals) if ep_loss_vals else 0.0)
        episode_infos.append(step_info)

        if (ep + 1) % log_interval == 0:
            mean_r = np.mean(rewards[-log_interval:])
            feasible_rate = sum(
                1 for inf in episode_infos[-log_interval:] if inf.get("feasible", False)
            ) / log_interval
            print(
                f"[Trainer] ep={ep+1}/{n_episodes} "
                f"mean_reward={mean_r:.2f} "
                f"feasible_rate={feasible_rate:.1%} "
                f"epsilon={policy.epsilon:.3f}"
            )

    elapsed = time.time() - t_start
    print(f"[Trainer] DQN 완료: {elapsed:.1f}s")

    if save_path:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        policy.save(save_path)
        print(f"[Trainer] Policy 저장: {save_path}")

    return {"rewards": rewards, "losses": losses, "n_episodes": n_episodes, "elapsed": elapsed}


def train_ppo_sb3(
    env: RosterSolvingEnv,
    n_timesteps: int = 10000,
    save_path: Optional[str] = None,
) -> dict:
    """stable-baselines3 PPO 학습.

    MultiDiscrete action space를 PPO로 학습한다.
    SB3가 없으면 ImportError를 raise한다.
    """
    try:
        from stable_baselines3 import PPO
        from stable_baselines3.common.env_checker import check_env
    except ImportError:
        raise ImportError("stable-baselines3 필요: pip install stable-baselines3")

    print(f"[Trainer] PPO (SB3) 학습 시작: {n_timesteps} timesteps")

    model = PPO(
        "MlpPolicy",
        env,
        verbose=1,
        n_steps=256,
        batch_size=64,
        n_epochs=10,
        gamma=0.95,
        learning_rate=3e-4,
    )
    model.learn(total_timesteps=n_timesteps)

    if save_path:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        model.save(save_path)
        print(f"[Trainer] PPO 모델 저장: {save_path}")

    return {"policy": model, "n_timesteps": n_timesteps}


def main():
    parser = argparse.ArgumentParser(description="RL Solver Strategy Trainer")
    parser.add_argument("--policy", choices=["linear", "mlp", "ppo", "random"], default="linear")
    parser.add_argument("--n_episodes", type=int, default=200)
    parser.add_argument("--time_limit", type=int, default=15,
                        help="CP-SAT 솔버 시간 제한 (초)")
    parser.add_argument("--use_sim", action="store_true",
                        help="실제 솔버 대신 시뮬레이션 모드 사용 (빠른 테스트)")
    parser.add_argument("--scenario_types", nargs="+",
                        default=["small"],
                        choices=["small", "medium", "large", "stress", "n_only", "pref_heavy"])
    parser.add_argument("--save_path", type=str, default="/tmp/rl_roster_policy.pkl")
    parser.add_argument("--log_interval", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print("=" * 60)
    print("RL Solver Strategy Controller - Training")
    print("=" * 60)
    print(f"Policy:         {args.policy}")
    print(f"Episodes:       {args.n_episodes}")
    print(f"Time limit:     {args.time_limit}s")
    print(f"Mode:           {'Simulation' if args.use_sim else 'Real Solver'}")
    print(f"Scenario types: {args.scenario_types}")
    print("=" * 60)

    # 시나리오 생성
    dataset = generate_dataset(
        n_train=max(100, args.n_episodes),
        n_val=20,
        n_test=20,
        base_seed=args.seed,
        scenario_types=args.scenario_types,
    )

    # 환경 생성
    env = RosterSolvingEnv(
        scenarios=dataset["train"],
        time_limit_per_solve=args.time_limit,
        use_real_solver=not args.use_sim,
        verbose=True,
    )

    # 정책 학습
    if args.policy == "linear":
        policy = LinearQLearning(seed=args.seed)
        stats = train_linear_q(
            env, policy,
            n_episodes=args.n_episodes,
            log_interval=args.log_interval,
            save_path=args.save_path,
        )
    elif args.policy == "mlp":
        policy = MLPPolicy(seed=args.seed)
        stats = train_dqn(
            env, policy,
            n_episodes=args.n_episodes,
            log_interval=args.log_interval,
            save_path=args.save_path,
        )
    elif args.policy == "ppo":
        stats = train_ppo_sb3(
            env,
            n_timesteps=args.n_episodes * 10,
            save_path=args.save_path,
        )
        policy = stats.get("policy")
    elif args.policy == "random":
        policy = RandomPolicy(seed=args.seed)
        stats = {"n_episodes": 0}

    # 최종 통계
    print("\n[Trainer] 학습 완료 통계:")
    if "rewards" in stats:
        rewards = stats["rewards"]
        print(f"  Mean reward (last 20%): {np.mean(rewards[int(len(rewards)*0.8):]):.2f}")
        print(f"  Best reward:            {max(rewards):.2f}")
        print(f"  Worst reward:           {min(rewards):.2f}")

    # 결과 저장
    result_path = args.save_path.replace(".pkl", "_stats.json")
    try:
        with open(result_path, "w") as f:
            json.dump({
                "policy": args.policy,
                "n_episodes": args.n_episodes,
                "scenario_types": args.scenario_types,
                "time_limit": args.time_limit,
                "use_sim": args.use_sim,
                "mean_reward_last20pct": float(np.mean(stats.get("rewards", [0])[-20:])),
            }, f, indent=2)
        print(f"[Trainer] 통계 저장: {result_path}")
    except Exception as e:
        print(f"[Trainer] 통계 저장 실패: {e}")


if __name__ == "__main__":
    main()
