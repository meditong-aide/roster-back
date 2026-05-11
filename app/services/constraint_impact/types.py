from __future__ import annotations

from typing import Literal

ConstraintFamily = Literal[
    "coverage",
    "team_min",
    "grade_min",
    "grade_max",
    "handoff",
    "preceptee_sync",
    "transition_ban",
    "consecutive_work",
    "consecutive_night",
    "recovery_2n2o",
    "recovery_3n2o",
    "monthly_night_cap",
    "monthly_off_min",
    "monthly_off_max",
    "weekend_only",
    "cross_month_4off",
    "initial_forbidden",
    "off_window",
]

ConstraintMode = Literal[
    "enforced",
    "soft_fallback",
    "skipped_by_capacity",
    "bypassed_by_fixed",
    "inactive",
]

AtomSource = Literal[
    "solver",
    "fixed_wanted",
    "special_fixed",
    "weekly_off",
    "manual_fixed",
    "agent_action",
    "forced_by_rule",
    "preceptee_sync",
]

SolveAttemptLabel = Literal["primary", "grade_max_retry"]

SimulationSeverity = Literal["ok", "warning", "hard_violation"]
