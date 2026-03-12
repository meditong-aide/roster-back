"""RL 환경: RosterSolvingEnv.

3-step MDP:
    Step 0 (reset → step 0):
        Obs:  문제 특성 벡터 (n_nurses, demands, prefs, ...)
        Act:  전체 시간 예산 분배 + 가중치 배수
        Eff:  Stage 1 파라미터 결정

    Step 1 (after Stage 1):
        Obs:  문제 특성 + Stage1 결과 (coverage_short, relax_level)
        Act:  Stage 2 시간 조정 + Stage 3 시간 조정
        Eff:  Stage 2 파라미터 결정

    Step 2 (after Stage 2):
        Obs:  문제 특성 + Stage1 결과 + Stage2 결과 (safety_sum)
        Act:  Stage 3 시간 미세 조정 + 가중치 최종 선택
        Eff:  Stage 3 파라미터 결정 → 최종 솔브 → Reward

왜 Contextual Bandit이 아닌 RL인가:
    - Step 1의 action은 Step 0 action 결과(Stage1 결과)에 의존한다.
    - Step 2의 action은 Step 0, Step 1 결과(Stage2 결과)에 의존한다.
    - 즉, 이전 결과를 보고 이후 strategy를 조정하는 구조이므로
      sequential MDP이며 RL의 명분이 충분하다.

사용 방법:
    env = RosterSolvingEnv(scenario_generator=..., time_limit_per_solve=30)
    obs, info = env.reset()
    while True:
        action = policy.predict(obs)
        obs, reward, done, truncated, info = env.step(action)
        if done:
            break
"""
from __future__ import annotations

import time
from typing import Any, Optional

import numpy as np

from services.rl.action_schema import (
    ACTION_NVEC,
    DEFAULT_ACTION,
    NIGHT_WEIGHT_MULTS,
    STAGE1_FRACTIONS,
    STAGE2_FRACTIONS,
    EXP_WEIGHT_MULTS,
    decode_action,
)
from services.rl.dataset import NurseScenario, generate_scenario
from services.rl.reward import (
    compute_night_fairness_score,
    compute_preference_satisfaction_score,
    compute_reward,
)
from services.rl.state_builder import (
    OBS_DIM,
    build_initial_state,
    extract_problem_features_from_scenario,
    update_state_after_stage1,
    update_state_after_stage2,
)

try:
    import gymnasium as gym
    from gymnasium import spaces
    GYM_AVAILABLE = True
except ImportError:
    try:
        import gym
        from gym import spaces
        GYM_AVAILABLE = True
    except ImportError:
        GYM_AVAILABLE = False


class SolverStageController:
    """RL action을 받아 solver stage callback을 생성하는 컨트롤러.

    동작:
        1. action을 decode → stage별 파라미터 계산
        2. rl_stage_callback으로 fallback_lex에 전달
        3. 각 stage 완료 후 state 업데이트
    """

    def __init__(self, action: np.ndarray, time_limit_seconds: int):
        params = decode_action(action)
        self.stage1_frac = params["stage1_frac"]
        self.stage2_frac = params["stage2_frac"]
        self.night_weight_mult = params["night_weight_mult"]
        self.exp_weight_mult = params["exp_weight_mult"]
        self.time_limit = time_limit_seconds

        # 시간 예산 계산
        self.tl1 = max(3, int(time_limit_seconds * self.stage1_frac))
        self.tl2 = max(3, int(time_limit_seconds * self.stage2_frac))
        self.tl3 = max(3, time_limit_seconds - self.tl1 - self.tl2)

        # 중간 결과 저장
        self.stage1_result: dict = {}
        self.stage2_result: dict = {}
        self.stage3_result: dict = {}

    def __call__(self, stage: int, state: dict) -> dict:
        """fallback_lex의 rl_stage_callback으로 전달될 함수."""
        if stage == 0:
            # 초기 파라미터 반환
            self.stage1_result["start_time"] = time.time()
            return {
                "stage1_seconds": self.tl1,
                "stage2_seconds": self.tl2,
                "stage3_seconds": self.tl3,
                "weight_multipliers": {
                    "night_deviation": self.night_weight_mult,
                    "experience": self.exp_weight_mult,
                },
            }
        elif stage == 1:
            # Stage 1 완료: 결과 저장, Stage 2/3 파라미터 조정
            self.stage1_result.update({
                "coverage_short": state.get("coverage_short", 0),
                "relax_level_used": state.get("relax_level_used", 0),
                "elapsed": time.time() - self.stage1_result.get("start_time", time.time()),
            })
            self.stage2_result["start_time"] = time.time()
            # Stage 1 결과가 좋으면 (short=0) Stage 3에 시간을 더 줌
            if state.get("coverage_short", 0) == 0:
                # 커버리지 완벽 → Stage 3 품질에 더 투자
                adjusted_tl2 = max(3, int(self.tl2 * 0.9))
                adjusted_tl3 = self.tl3 + (self.tl2 - adjusted_tl2)
            else:
                adjusted_tl2 = self.tl2
                adjusted_tl3 = self.tl3
            return {
                "stage2_seconds": adjusted_tl2,
                "stage3_seconds": adjusted_tl3,
                "weight_multipliers": {},
            }
        elif stage == 2:
            # Stage 2 완료: 결과 저장, Stage 3 파라미터 조정
            self.stage2_result.update({
                "safety_violation_sum": state.get("safety_violation_sum", 0),
                "elapsed": time.time() - self.stage2_result.get("start_time", time.time()),
            })
            self.stage3_result["start_time"] = time.time()
            # Stage 2 결과가 좋으면 Stage 3에 여유 시간 추가
            tl3_adjusted = state.get("tl3_budget", self.tl3)
            if state.get("safety_violation_sum", 0) == 0:
                tl3_adjusted = max(tl3_adjusted, self.tl3)
            return {
                "stage3_seconds": tl3_adjusted,
                "weight_multipliers": {
                    "preference": 1.0,
                },
            }
        return {}


class RosterSolvingEnv:
    """근무표 생성 전략을 학습하는 RL 환경 (gymnasium 호환).

    이 환경은 gymnasium 인터페이스를 따르지만,
    gymnasium이 설치되지 않은 경우에도 동작한다.

    Attributes:
        observation_space: shape=(12,) float32 [-inf, inf]
        action_space:      MultiDiscrete([5, 4, 4, 3])
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        scenarios: list[NurseScenario] | None = None,
        scenario_types: list[str] | None = None,
        time_limit_per_solve: int = 30,
        max_episode_steps: int = 3,
        use_real_solver: bool = True,
        verbose: bool = False,
    ):
        """
        Args:
            scenarios:             시나리오 리스트 (None이면 자동 생성)
            scenario_types:        자동 생성 시 사용할 유형 목록
            time_limit_per_solve:  CP-SAT 솔버 시간 제한 (초)
            max_episode_steps:     에피소드당 최대 스텝 수 (3-step MDP)
            use_real_solver:       실제 CP-SAT 솔버 사용 여부
                                   False면 빠른 시뮬레이션 모드 사용
            verbose:               상세 로그 출력
        """
        self.scenarios = scenarios
        self.scenario_types = scenario_types or ["small", "medium"]
        self.time_limit_per_solve = time_limit_per_solve
        self.max_episode_steps = max_episode_steps
        self.use_real_solver = use_real_solver
        self.verbose = verbose

        # 공간 정의
        if GYM_AVAILABLE:
            self.observation_space = spaces.Box(
                low=-np.inf, high=np.inf, shape=(OBS_DIM,), dtype=np.float32
            )
            self.action_space = spaces.MultiDiscrete(ACTION_NVEC)
        else:
            self.observation_space = None
            self.action_space = None

        # 에피소드 상태
        self._current_scenario: Optional[NurseScenario] = None
        self._current_obs: np.ndarray = np.zeros(OBS_DIM, dtype=np.float32)
        self._step_count: int = 0
        self._episode_count: int = 0
        self._controller: Optional[SolverStageController] = None

        # 누적 통계
        self._episode_rewards: list[float] = []
        self._episode_info: list[dict] = []

    def reset(
        self,
        seed: int | None = None,
        options: dict | None = None,
    ) -> tuple[np.ndarray, dict]:
        """에피소드 초기화. 새 시나리오를 선택하고 초기 관찰을 반환."""
        self._episode_count += 1
        self._step_count = 0
        self._controller = None

        # 시나리오 선택
        if self.scenarios:
            rng = np.random.RandomState(seed if seed is not None else self._episode_count)
            idx = rng.randint(0, len(self.scenarios))
            self._current_scenario = self.scenarios[idx]
        else:
            stype_rng = np.random.RandomState(seed if seed is not None else self._episode_count)
            stype = self.scenario_types[stype_rng.randint(0, len(self.scenario_types))]
            scenario_seed = int(stype_rng.randint(0, 100000))
            self._current_scenario = generate_scenario(stype, seed=scenario_seed)

        # 초기 관찰 구성
        sc = self._current_scenario
        features = extract_problem_features_from_scenario({
            "n_nurses": sc.n_nurses,
            "n_days": sc.n_days,
            "daily_requirements": sc.daily_requirements,
            "preference_matrix": sc.preference_matrix,
            "fixed_cells": sc.fixed_cells,
            "nurses": sc.nurses,
            "weekend_count": sc.weekend_count,
        })
        self._current_obs = build_initial_state(**features)
        self._stage1_result = {}
        self._stage2_result = {}

        info = {
            "scenario_id": sc.scenario_id,
            "scenario_type": sc.scenario_type,
            "n_nurses": sc.n_nurses,
            "n_days": sc.n_days,
            "expected_difficulty": sc.expected_difficulty,
        }
        return self._current_obs.copy(), info

    def step(self, action: np.ndarray) -> tuple[np.ndarray, float, bool, bool, dict]:
        """환경 스텝. 이 환경은 단일 스텝으로 전체 solve를 실행한다.

        3-step MDP를 구현하되, 실용성을 위해 단일 step에서 전체 solve를 실행하고
        내부적으로 3회 callback을 통해 sequential decision을 재현한다.
        이는 solver가 외부에서 중단 불가능한 블로킹 호출이기 때문이다.

        Args:
            action: MultiDiscrete([5,4,4,3]) 또는 flattened integer

        Returns:
            (obs, reward, done, truncated, info)
        """
        if self._current_scenario is None:
            raise RuntimeError("환경이 초기화되지 않았습니다. reset()을 먼저 호출하세요.")

        sc = self._current_scenario
        self._step_count += 1

        # Controller 생성 (callback 역할)
        controller = SolverStageController(
            action=np.array(action) if not isinstance(action, np.ndarray) else action,
            time_limit_seconds=self.time_limit_per_solve,
        )

        # 솔버 실행
        t_start = time.time()
        result = self._run_solver(sc, controller)
        elapsed = time.time() - t_start

        # Stage 결과 수집
        coverage_short = result.get("coverage_short", 0)
        safety_sum = result.get("safety_violation_sum", 0)
        pref_score = result.get("preference_score", 0.5)
        fairness_score = result.get("night_fairness_score", 0.5)

        # Stage 1/2 결과로 관찰 업데이트 (정보 제공용)
        obs = update_state_after_stage1(
            self._current_obs,
            coverage_short=coverage_short,
            relax_level_used=controller.stage1_result.get("relax_level_used", 0),
            n_nurses=sc.n_nurses,
            n_days=sc.n_days,
            tl1_used_ratio=min(1.0, controller.stage1_result.get("elapsed", controller.tl1) / max(1, controller.tl1)),
        )
        obs = update_state_after_stage2(
            obs,
            safety_violation_sum=safety_sum,
            n_nurses=sc.n_nurses,
            n_days=sc.n_days,
            tl2_used_ratio=min(1.0, controller.stage2_result.get("elapsed", controller.tl2) / max(1, controller.tl2)),
        )
        self._current_obs = obs

        # Reward 계산
        reward, components = compute_reward(
            coverage_short=coverage_short,
            safety_violation_sum=safety_sum,
            preference_score=pref_score,
            night_fairness_score=fairness_score,
            solve_time_ratio=min(1.0, elapsed / max(1, self.time_limit_per_solve)),
            n_nurses=sc.n_nurses,
            n_days=sc.n_days,
        )

        done = True  # 단일 solve로 에피소드 완료
        truncated = False

        info = {
            "scenario_id": sc.scenario_id,
            "coverage_short": coverage_short,
            "safety_violation_sum": safety_sum,
            "preference_score": pref_score,
            "night_fairness_score": fairness_score,
            "elapsed_seconds": elapsed,
            "reward_components": components,
            "action_params": decode_action(action),
            "feasible": (coverage_short == 0 and safety_sum == 0),
            "stage1_result": controller.stage1_result,
            "stage2_result": controller.stage2_result,
        }

        self._episode_rewards.append(reward)
        self._episode_info.append(info)

        if self.verbose:
            print(
                f"[Env] Episode {self._episode_count}, Step {self._step_count}: "
                f"reward={reward:.2f}, coverage_short={coverage_short}, "
                f"safety_sum={safety_sum}, pref={pref_score:.3f}, "
                f"elapsed={elapsed:.1f}s"
            )

        return obs.copy(), float(reward), done, truncated, info

    def _run_solver(self, sc: NurseScenario, controller: SolverStageController) -> dict:
        """실제 CP-SAT 솔버 또는 시뮬레이션 실행.

        use_real_solver=True이면 실제 CP-SAT 솔버를 호출한다.
        False이면 빠른 시뮬레이션 결과를 반환한다 (학습 초기 테스트용).
        """
        if self.use_real_solver:
            return self._run_real_solver(sc, controller)
        else:
            return self._simulate_solver(sc, controller)

    def _run_real_solver(self, sc: NurseScenario, controller: SolverStageController) -> dict:
        """실제 CP-SAT 솔버 호출.

        synthetic scenario를 실제 solver가 이해하는 형식으로 변환 후 실행.
        """
        try:
            from services.rl.solver_bridge import run_synthetic_scenario
            return run_synthetic_scenario(sc, controller, self.time_limit_per_solve)
        except Exception as e:
            if self.verbose:
                print(f"[Env] Solver 실행 오류: {e}")
            # 실패 시 최악 결과 반환
            return {
                "coverage_short": sc.n_nurses,
                "safety_violation_sum": sc.n_nurses * sc.n_days,
                "preference_score": 0.0,
                "night_fairness_score": 0.0,
                "solver_failed": True,
            }

    def _simulate_solver(self, sc: NurseScenario, controller: SolverStageController) -> dict:
        """빠른 시뮬레이션 모드 (실제 솔버 없이 결과 추정).

        Stage별 시간 예산에 따라 결과 품질이 달라지는 것을 모사한다.
        이는 학습 초기 단계나 단위 테스트용으로만 사용한다.

        Note:
            이 시뮬레이션은 매우 단순화된 것으로, 실제 연구 결과에는
            반드시 real solver를 사용해야 한다.
        """
        rng = np.random.RandomState(sc.seed + self._episode_count)

        # Stage 1 시뮬레이션: 시간이 많을수록 커버리지 달성 확률 증가
        stage1_time_ratio = controller.tl1 / max(1, self.time_limit_per_solve)
        coverage_quality = np.clip(stage1_time_ratio * 2.0 + rng.normal(0, 0.1), 0, 1)

        # Stage 2 시뮬레이션
        stage2_time_ratio = controller.tl2 / max(1, self.time_limit_per_solve)
        safety_quality = np.clip(stage2_time_ratio * 2.5 + rng.normal(0, 0.15), 0, 1)

        # Stage 3 시뮬레이션
        stage3_time_ratio = controller.tl3 / max(1, self.time_limit_per_solve)
        pref_quality = np.clip(stage3_time_ratio * 3.0 + rng.normal(0, 0.2), 0, 1)

        # 난이도에 따른 보정
        diff = sc.expected_difficulty
        coverage_short = max(0, int((1 - coverage_quality) * sc.n_nurses * diff * 2))
        safety_sum = max(0, int((1 - safety_quality) * sc.n_nurses * diff * 3))
        pref_score = float(pref_quality * (1 - diff * 0.3))

        # Stage 결과 업데이트 (RL 관찰용)
        controller.stage1_result.update({
            "coverage_short": coverage_short,
            "relax_level_used": 0 if coverage_short == 0 else rng.randint(1, 4),
            "elapsed": controller.tl1,
        })
        controller.stage2_result.update({
            "safety_violation_sum": safety_sum,
            "elapsed": controller.tl2,
        })

        return {
            "coverage_short": coverage_short,
            "safety_violation_sum": safety_sum,
            "preference_score": pref_score,
            "night_fairness_score": float(0.5 + rng.uniform(-0.2, 0.2)),
        }

    def get_episode_stats(self) -> dict:
        """지금까지의 에피소드 통계 반환."""
        if not self._episode_rewards:
            return {}
        return {
            "n_episodes": len(self._episode_rewards),
            "mean_reward": float(np.mean(self._episode_rewards)),
            "std_reward": float(np.std(self._episode_rewards)),
            "min_reward": float(np.min(self._episode_rewards)),
            "max_reward": float(np.max(self._episode_rewards)),
        }
