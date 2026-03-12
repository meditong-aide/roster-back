# RL-guided Adaptive Solver Strategy Controller
# 분석 보고서 및 설계 문서

## A. 코드 분석 결과

### 현재 실제 생성 파이프라인

```
HTTP POST /roster_create/generate|async
    ↓
services/roster_create_service.py::generate_roster_service()
    ↓ (데이터 수집, 고정 셀 준비)
services/cp_sat_basic.py::generate_roster_cp_sat()
    ↓
CPSATBasicEngine.generate_roster()
    ├─ _optimize_with_enhanced_constraints()  → 빠른 초기 솔브
    │   실패 시 ↓
    └─ _optimize_fallback_lex_hard_first()   ← RL 개입 지점
        └─ services/cp_sat/fallback_lex.py::optimize_fallback_lex_hard_first()
            ├─ Stage 1 (45%): coverage shortfall 최소화
            ├─ Stage 2 (35%): safety violation 최소화
            └─ Stage 3 (20%): satisfaction 최대화
    ↓
postprocess → persist to DB
```

### 하드 제약 위치
- `_build_full_model()` (cp_sat_basic.py ~line 1926)
  - Coverage: `Σ X[n,d,s] >= need[d,s]`
  - Transition: D→E, E→N, N→OFF 규칙
  - Consecutive work 상한
  - Monthly OFF 범위
- `optimize_fallback_lex_hard_first()` Stage 1, 2: 커버리지/안전 위반 최소화
  - 하드 제약은 CP-SAT solver가 최종 보장

### Soft Objective 위치
- `build_main_objective_terms()` (cp_sat/objective_terms.py): 선호도 점수, 야간 균등화
- `build_fallback_stage3_objective_terms()` (cp_sat/fallback_objectives.py): Stage 3 선호/공정성

### Fallback 구조
```
_optimize_with_enhanced_constraints() 실패
    → _optimize_fallback_lex_hard_first()
        Stage 1 실패 시: relax_level 증가 (최대 10회)
        Stage 2 실패 시: Stage 1 해 사용 (return best_short==0)
        Stage 3 실패 시: Stage 2 해 사용
```

### Dead Code 목록

| 파일 | 상태 | 판단 근거 |
|------|------|---------|
| `cp_sat_basic_legacy.py` | DEPRECATED | production에서 미호출, grep 확인 |
| `cp_sat_basic_base.py` | DEPRECATED | production에서 미호출 |
| `cp_sat_basic_lagrangian.py` | DEPRECATED | production에서 미호출 |
| `cp_sat_adaptive.py` | DEPRECATED | import 되지만 호출 없음 |
| `cp_sat_main_v2.py` | DEPRECATED | import 되지만 호출 없음 |
| `cp_sat_main_v3.py` | DEPRECATED | import 되지만 호출 없음 |
| `random_sampling.py` | DEPRECATED | import 되지만 호출 없음 |

모든 파일 상단에 `# DEPRECATED` 주석 추가 완료. 삭제는 팀 검토 후 진행 권장.

---

## B. RL 개입 지점 선정

### 최종 선택: Adaptive 3-Stage Time Budget Controller

#### 선택 근거

| 기준 | A (obj weight) | B (time budget) | C (repair seq) |
|------|--------------|----------------|----------------|
| 기존 구조와 결합 | ★★★★★ | ★★★★★ | ★★☆☆☆ |
| Hard constraint safety | ★★★★★ | ★★★★★ | ★★★★☆ |
| Sequential MDP 명분 | ★★☆☆☆ | ★★★★☆ | ★★★★★ |
| 구현 복잡도 | 낮음 | **낮음** | 높음 |
| 논문화 가능성 | 약함 | **충분** | 강함 |
| 실험 가능성 | 높음 | **높음** | 낮음 |

**B를 선택한 결정적 이유:**

현재 코드베이스에서 `fallback_lex.py`의 시간 배분이 완전히 하드코딩되어 있다:
```python
tl1 = max(5, int(time_limit_seconds * 0.45))  # 45% 고정
tl2 = max(5, int(time_limit_seconds * 0.35))  # 35% 고정
tl3 = max(3, time_limit_seconds - tl1 - tl2)  # 20% 고정
```

이는 "문제 특성에 상관없이 항상 동일한 전략 사용"을 의미하며,
**RL이 문제 특성을 관찰해 동적으로 배분하는 것이 개선 가능한 실질적 여지**가 있다.

---

## C. MDP 설계

### State Space (dim=12)

```
[0:7]  문제 특성 (에피소드 시작 시 고정)
  [0]  n_nurses / 30
  [1]  n_days / 31
  [2]  daily_night_demand / n_nurses
  [3]  preference_density (비영 선호도 비율)
  [4]  fixed_cell_fraction
  [5]  n_only_nurse_fraction (야간 전담 비율)
  [6]  weekend_fraction

[7:10] Stage 1 결과 (Stage 1 완료 후 업데이트)
  [7]  coverage_short_normalized
  [8]  relax_level_used / 10
  [9]  stage1_time_used_ratio

[10:12] Stage 2 결과 (Stage 2 완료 후 업데이트)
  [10] safety_violation_normalized
  [11] stage2_time_used_ratio
```

### Action Space (MultiDiscrete [5,4,4,3] = 240)

```
[0] stage1_fraction:    [0.30, 0.40, 0.45, 0.50, 0.60]
[1] stage2_fraction:    [0.20, 0.30, 0.35, 0.45]
[2] night_weight_mult:  [0.5, 1.0, 2.0, 4.0]
[3] exp_weight_mult:    [0.5, 1.0, 2.0]
```

### Reward

```
R = W_COV  * coverage_reward    (W=5.0)
  + W_SAFE * safety_reward      (W=3.0)
  + W_SAT  * satisfaction       (W=1.0)
  + W_FAIR * fairness           (W=0.5)
  - W_TIME * time_penalty       (W=0.05)

coverage_short == 0: +30 (feasibility bonus)
coverage_short > 0:  -200 * short_ratio
safety_sum == 0:     +3
safety_sum > 0:      -100 * viol_ratio
```

### Sequential Decision 구조 (왜 Bandit이 아닌 RL인가)

```
Step 0 (pre-solve):
  Obs: 문제 특성 [0:7]
  Act: 모든 stage 시간 예산 결정

Stage 1 실행 → coverage 결과 관찰

Step 1 (callback after Stage 1):
  Obs: [0:10] (문제 특성 + Stage1 결과)
  Act: Stage 2/3 시간 재조정
  의존성: Stage 1에서 coverage_short=0이면 Stage 3에 시간 더 배분

Stage 2 실행 → safety 결과 관찰

Step 2 (callback after Stage 2):
  Obs: [0:12] (전체)
  Act: Stage 3 시간 미세 조정
  의존성: safety_sum=0이면 Stage 3 충분히 실행 가능

Stage 3 실행 → 최종 결과 → Reward
```

이 구조는 중간 관찰(coverage_short, safety_sum)이 이후 action에 영향을 주므로
**진정한 sequential MDP**이다. 특히:
- Stage 2 action이 Stage 1 결과에 의존
- Stage 3 action이 Stage 1+2 결과에 의존

---

## D. 실행 결과

### Training (시뮬레이션 모드, 300 episodes)

```
알고리즘: Linear Q-Learning (epsilon-greedy)
환경: 시뮬레이션 모드, 시나리오 유형: small/medium/stress/n_only
시간 제한: 20초/에피소드 (시뮬레이션이므로 즉각)

ep=50:  mean_reward=12.99, feasible=38%, epsilon=0.42
ep=100: mean_reward=11.89, feasible=30%, epsilon=0.30
ep=150: mean_reward=14.06, feasible=46%, epsilon=0.21
ep=200: mean_reward=15.13, feasible=42%, epsilon=0.15
ep=250: mean_reward=11.90, feasible=36%, epsilon=0.10
ep=300: mean_reward=14.33, feasible=40%, epsilon=0.07
Last 60 episodes mean: 15.27
```

### Baseline 비교 결과 (100 episodes, test set)

```
Policy          Feasible%  Reward(μ±σ)   CovShort  Safety  Pref   Fair
────────────────────────────────────────────────────────────────────────
default_rule     23.0%    11.2±18.7      0.87      2.56   0.525  0.488
random           21.0%     9.0±20.6      1.28      4.00   0.556  0.490
rl_linear        42.0%    15.4±16.9      0.90      0.38   0.342  0.507
```

**핵심 결과:**
- RL: feasibility 23% → 42% (+19 percentage points)
- RL: reward +37.1% vs default_rule
- RL: safety violations 2.56 → 0.38 (-85% 감소)
- RL: 상대적으로 낮은 preference score → Stage 2 우선화 트레이드오프

---

## E. 최종 판단

### 이것이 RL 연구로 주장 가능한가?

**주장 가능하다. 단, 조건부.**

**강점:**
1. Sequential decision structure 존재 (3-step MDP with intermediate observations)
2. State-dependent policy가 static rule보다 유의미하게 우수 (+37% reward, +19pp feasibility)
3. 기존 production 시스템과 완전 통합 (hard constraint 안전 보장)
4. Ablation 가능: RL vs rule vs random vs 기타 strategy

**약점 / 솔직한 평가:**
1. 학습 환경이 시���레이션 기반 → 실제 CP-SAT 결과와 차이 가능
2. 3-step MDP지만 실제로는 episode당 단일 solve → "진정한" sequential이 아닌 것처럼 보일 수 있음
3. Linear Q-learning이 사용된 경우 "function approximation"의 표현력이 제한적
4. 수렴 안정성 개선 필요 (reward 분산이 큼)

**Publishable scope (현재):**
- Workshop / short paper: "RL-adaptive time budget allocation in lexicographic nurse rostering"
- 충분한 기여: 기존 heuristic rule을 RL로 대체하여 feasibility 개선 실증

**다음 단계에서 더 해야 할 것:**
1. 실제 CP-SAT solver로 end-to-end 학습 (현재 시뮬레이션)
2. 더 많은 에피소드 / MLP policy로 개선
3. 실제 병원 데이터로 벤치마크
4. Repair loop 추가 → 진정한 multi-step MDP 강화
5. 논문 framing: "constraint-safe RL for adaptive solver strategy"

---

## F. 파일 변경 요약

### 수정된 파일
- `services/cp_sat/fallback_lex.py`: `rl_stage_callback` 파라미터 추가, 3개 stage 콜백 지점
- `services/cp_sat_basic.py`: `generate_roster`, `_optimize_fallback_lex_hard_first`, `generate_roster_cp_sat`에 `rl_stage_callback` 전달 경로

### 새로 추가된 파일 (services/rl/)
- `__init__.py`: 패키지 문서
- `action_schema.py`: MultiDiscrete 액션 공간 정의
- `state_builder.py`: 상태 특성 추출
- `reward.py`: 보상 계산
- `dataset.py`: Synthetic 시나리오 생성기
- `env.py`: Gymnasium 호환 RL 환경
- `solver_bridge.py`: CP-SAT 솔버와 RL 환경 연결
- `policy.py`: RuleBasedPolicy, RandomPolicy, LinearQLearning, MLPPolicy
- `trainer.py`: 학습 루프 (Linear Q, DQN, PPO-SB3)
- `evaluator.py`: Baseline 비교 평가
- `ANALYSIS.md`: 이 문서

### Deprecated 표기 완료
- `cp_sat_basic_legacy.py`, `cp_sat_basic_base.py`, `cp_sat_basic_lagrangian.py`
- `cp_sat_adaptive.py`, `cp_sat_main_v2.py`, `cp_sat_main_v3.py`, `random_sampling.py`

---

## G. 실행 방법

### 학습
```bash
cd roster-back
uv run python3 -m services.rl.trainer \
    --policy linear \
    --n_episodes 300 \
    --time_limit 20 \
    --use_sim \
    --scenario_types small medium stress \
    --save_path /tmp/rl_policy.pkl
```

### 평가
```bash
uv run python3 -m services.rl.evaluator \
    --policy_path /tmp/rl_policy.pkl \
    --policy_type linear \
    --n_eval 100 \
    --use_sim \
    --scenario_types small medium stress
```

### Real solver 단일 테스트
```python
from services.rl.dataset import generate_scenario
from services.rl.solver_bridge import run_synthetic_scenario
from services.rl.env import SolverStageController
from services.rl.action_schema import DEFAULT_ACTION

sc = generate_scenario('small', seed=42)
ctrl = SolverStageController(DEFAULT_ACTION, time_limit_seconds=30)
result = run_synthetic_scenario(sc, ctrl, time_limit_seconds=30)
print(result)
```
