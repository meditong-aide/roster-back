"""RL Evaluator: Baseline 비교 실험 모듈.

다음 policy들을 비교한다:
    1. default_rule:   현재 45/35/20% 하드코딩 (production baseline)
    2. random:         무작위 action 선택
    3. rl_linear:      학습된 Linear Q-Learning
    4. rl_mlp:         학습된 MLP DQN (선택)
    5. rl_ppo:         학습된 PPO (선택)

비교 지표:
    - feasibility_rate:        커버리지 + 안전 위반 없는 비율
    - mean_coverage_short:     평균 커버리지 부족
    - mean_safety_violation:   평균 안전 위반 합
    - mean_preference_score:   평균 선호 만족도
    - mean_fairness_score:     평균 야간 공정성
    - mean_reward:             평균 reward
    - mean_solve_time:         평균 소요 시간

실행:
    python -m services.rl.evaluator \
        --policy_path /tmp/rl_roster_policy.pkl \
        --policy_type linear \
        --n_eval 50 \
        --time_limit 15 \
        --use_sim
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(os.path.dirname(_HERE))
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from services.rl.dataset import generate_dataset
from services.rl.env import RosterSolvingEnv
from services.rl.policy import LinearQLearning, MLPPolicy, RuleBasedPolicy, RandomPolicy


def evaluate_policy(
    policy,
    env: RosterSolvingEnv,
    n_episodes: int = 50,
    deterministic: bool = True,
    policy_name: str = "unknown",
) -> dict:
    """단일 policy를 n_episodes 동안 평가.

    Args:
        policy:       평가할 policy (predict() 메서드 필요)
        env:          평가 환경
        n_episodes:   평가 에피소드 수
        deterministic: 결정적 action 선택 여부
        policy_name:  로그용 이름

    Returns:
        평가 지표 dict
    """
    rewards = []
    coverage_shorts = []
    safety_violations = []
    pref_scores = []
    fairness_scores = []
    feasible_count = 0
    elapsed_times = []
    action_distributions = []

    print(f"[Evaluator] {policy_name} 평가 중 ({n_episodes} episodes)...")

    for ep in range(n_episodes):
        obs, info = env.reset(seed=ep * 1000)  # 재현 가능한 seed
        done = False
        ep_reward = 0.0
        t_ep = time.time()

        while not done:
            action, _ = policy.predict(obs, deterministic=deterministic)
            obs, reward, done, truncated, step_info = env.step(action)
            ep_reward += reward

        elapsed_times.append(time.time() - t_ep)
        rewards.append(ep_reward)
        coverage_shorts.append(step_info.get("coverage_short", 0))
        safety_violations.append(step_info.get("safety_violation_sum", 0))
        pref_scores.append(step_info.get("preference_score", 0.5))
        fairness_scores.append(step_info.get("night_fairness_score", 0.5))
        if step_info.get("feasible", False):
            feasible_count += 1
        action_distributions.append(step_info.get("action_params", {}))

    results = {
        "policy_name": policy_name,
        "n_episodes": n_episodes,
        "feasibility_rate": feasible_count / max(1, n_episodes),
        "mean_reward": float(np.mean(rewards)),
        "std_reward": float(np.std(rewards)),
        "mean_coverage_short": float(np.mean(coverage_shorts)),
        "mean_safety_violation": float(np.mean(safety_violations)),
        "mean_preference_score": float(np.mean(pref_scores)),
        "mean_fairness_score": float(np.mean(fairness_scores)),
        "mean_solve_time": float(np.mean(elapsed_times)),
        "rewards": rewards,
    }

    # action 분포 통계
    if action_distributions:
        stage1_fracs = [a.get("stage1_frac", 0.45) for a in action_distributions]
        stage2_fracs = [a.get("stage2_frac", 0.35) for a in action_distributions]
        results["action_stats"] = {
            "mean_stage1_frac": float(np.mean(stage1_fracs)),
            "mean_stage2_frac": float(np.mean(stage2_fracs)),
            "std_stage1_frac": float(np.std(stage1_fracs)),
        }

    print(
        f"[Evaluator] {policy_name}: "
        f"feasible={results['feasibility_rate']:.1%} "
        f"reward={results['mean_reward']:.2f}±{results['std_reward']:.2f} "
        f"cov_short={results['mean_coverage_short']:.2f} "
        f"safety={results['mean_safety_violation']:.2f} "
        f"pref={results['mean_preference_score']:.3f}"
    )
    return results


def compare_policies(
    policies: dict,
    env: RosterSolvingEnv,
    n_episodes: int = 50,
    deterministic: bool = True,
) -> dict[str, dict]:
    """여러 policy를 동일한 환경에서 비교.

    Args:
        policies:     {name: policy} dict
        env:          공유 평가 환경 (동일 시나리오 순서)
        n_episodes:   각 policy당 평가 에피소드 수
        deterministic: 결정적 action 여부

    Returns:
        {policy_name: metrics_dict}
    """
    all_results = {}
    for name, policy in policies.items():
        results = evaluate_policy(
            policy, env, n_episodes=n_episodes,
            deterministic=deterministic, policy_name=name,
        )
        all_results[name] = results

    return all_results


def print_comparison_table(results: dict[str, dict]) -> None:
    """비교 결과를 표 형식으로 출력."""
    print("\n" + "=" * 90)
    print("RL vs Baseline 비교 결과")
    print("=" * 90)

    headers = [
        "Policy",
        "Feasible%",
        "Reward(μ±σ)",
        "CovShort",
        "Safety",
        "Pref",
        "Fair",
        "Time(s)",
    ]
    print(f"{'Policy':<20} {'Feasible%':>10} {'Reward':>14} {'CovShort':>10} {'Safety':>8} {'Pref':>7} {'Fair':>7} {'Time(s)':>8}")
    print("-" * 90)

    # baseline 성능 기준
    baseline = results.get("default_rule", {})
    baseline_reward = baseline.get("mean_reward", 0)

    for name, r in results.items():
        improvement = ""
        if name != "default_rule" and baseline_reward != 0:
            pct = (r["mean_reward"] - baseline_reward) / abs(baseline_reward) * 100
            improvement = f" ({pct:+.1f}%)"

        reward_str = f"{r['mean_reward']:.1f}±{r['std_reward']:.1f}{improvement}"
        print(
            f"{name:<20} "
            f"{r['feasibility_rate']:>9.1%} "
            f"{reward_str:>14} "
            f"{r['mean_coverage_short']:>10.2f} "
            f"{r['mean_safety_violation']:>8.2f} "
            f"{r['mean_preference_score']:>7.3f} "
            f"{r['mean_fairness_score']:>7.3f} "
            f"{r['mean_solve_time']:>8.1f}"
        )

    print("=" * 90)

    # 개선도 요약
    if "default_rule" in results and len(results) > 1:
        print("\n[개선도 요약] vs default_rule (45/35/20% 고정 배분):")
        for name, r in results.items():
            if name == "default_rule":
                continue
            delta_r = r["mean_reward"] - baseline.get("mean_reward", 0)
            delta_f = r["feasibility_rate"] - baseline.get("feasibility_rate", 0)
            delta_p = r["mean_preference_score"] - baseline.get("mean_preference_score", 0)
            print(
                f"  {name}: reward {delta_r:+.2f}, "
                f"feasible {delta_f:+.1%}, "
                f"pref {delta_p:+.3f}"
            )


def main():
    parser = argparse.ArgumentParser(description="RL Policy 평가 및 Baseline 비교")
    parser.add_argument("--policy_path", type=str, default=None,
                        help="학습된 policy 파일 경로")
    parser.add_argument("--policy_type", choices=["linear", "mlp"], default="linear")
    parser.add_argument("--n_eval", type=int, default=50)
    parser.add_argument("--time_limit", type=int, default=15)
    parser.add_argument("--use_sim", action="store_true")
    parser.add_argument("--scenario_types", nargs="+", default=["small"])
    parser.add_argument("--save_results", type=str, default="/tmp/rl_eval_results.json")
    parser.add_argument("--seed", type=int, default=99)
    args = parser.parse_args()

    print("=" * 60)
    print("RL Policy 평가 및 Baseline 비교")
    print("=" * 60)

    # 테스트 시나리오
    dataset = generate_dataset(
        n_train=10, n_val=10,
        n_test=max(args.n_eval, 50),
        base_seed=args.seed,
        scenario_types=args.scenario_types,
    )

    env = RosterSolvingEnv(
        scenarios=dataset["test"],
        time_limit_per_solve=args.time_limit,
        use_real_solver=not args.use_sim,
        verbose=False,
    )

    # Baseline policy들
    policies = {
        "default_rule": RuleBasedPolicy(),
        "random": RandomPolicy(seed=args.seed),
    }

    # 학습된 RL policy 로드
    if args.policy_path and os.path.exists(args.policy_path):
        try:
            if args.policy_type == "linear":
                rl_policy = LinearQLearning.load(args.policy_path)
            elif args.policy_type == "mlp":
                rl_policy = MLPPolicy.load(args.policy_path)
            policies[f"rl_{args.policy_type}"] = rl_policy
            print(f"[Evaluator] RL policy 로드: {args.policy_path}")
        except Exception as e:
            print(f"[Evaluator] RL policy 로드 실패: {e}")
    else:
        print("[Evaluator] RL policy 없음 - baseline만 비교합니다.")

    # 비교 실행
    results = compare_policies(
        policies=policies,
        env=env,
        n_episodes=args.n_eval,
        deterministic=True,
    )

    # 결과 출력
    print_comparison_table(results)

    # 결과 저장
    try:
        save_data = {
            k: {kk: vv for kk, vv in v.items() if kk != "rewards"}
            for k, v in results.items()
        }
        save_data["_meta"] = {
            "n_eval": args.n_eval,
            "time_limit": args.time_limit,
            "scenario_types": args.scenario_types,
            "use_sim": args.use_sim,
        }
        with open(args.save_results, "w") as f:
            json.dump(save_data, f, indent=2)
        print(f"\n[Evaluator] 결과 저장: {args.save_results}")
    except Exception as e:
        print(f"[Evaluator] 결과 저장 실패: {e}")


if __name__ == "__main__":
    main()
