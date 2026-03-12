"""RL-guided Solver Strategy Controller for Nurse Rostering.

Architecture:
    - The RL agent observes problem features and intermediate solver results
    - It makes sequential decisions at 3 points during the lexicographic optimization:
        Stage 0 (pre-solve): Choose time budget fractions for all 3 stages
        Stage 1 (post-Stage1): Adjust Stage 2/3 budgets based on coverage result
        Stage 2 (post-Stage2): Adjust Stage 3 budget based on safety violation result
    - The CP-SAT solver always guarantees hard constraint feasibility
    - RL only controls *strategy* (time allocation, weight priorities)

Research framing:
    "RL-guided Adaptive Time Budget Allocation for Lexicographic Nurse Rostering"

MDP:
    State:  problem features + intermediate solver results (dim=12)
    Action: MultiDiscrete [stage1_frac, stage2_frac, night_weight, exp_weight]
    Reward: weighted combination of coverage, safety, satisfaction, fairness
    Horizon: 3 steps per episode (one per solver stage)
"""
