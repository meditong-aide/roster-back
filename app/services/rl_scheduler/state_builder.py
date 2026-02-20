from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class LNSState:
    vector: list[float]
    features: dict[str, Any]


def _safe_rate(num: float, den: float) -> float:
    if den <= 0:
        return 0.0
    return float(num) / float(den)


def _collect_shortage_by_day(roster_system) -> list[int]:
    cfg = roster_system.config
    shift_types = getattr(cfg, "shift_types", [])
    req = getattr(cfg, "daily_shift_requirements", {}) or {}
    day_count = int(getattr(roster_system, "num_days", 0))
    nurse_count = len(getattr(roster_system, "nurses", []))
    shortages = [0 for _ in range(day_count)]
    if day_count <= 0 or nurse_count <= 0:
        return shortages
    for d in range(day_count):
        day_short = 0
        for code, target in req.items():
            if code not in shift_types:
                continue
            s_idx = shift_types.index(code)
            assigned = int(np.sum(roster_system.roster[:, d, s_idx]))
            day_short += max(0, int(target) - assigned)
        shortages[d] = day_short
    return shortages


def build_lns_state(roster_system, lns_metrics: dict | None = None) -> LNSState:
    lns_metrics = lns_metrics or {}
    violations = roster_system._find_violations()
    hard_count = len(violations)
    day_count = int(getattr(roster_system, "num_days", 0))
    nurse_count = len(getattr(roster_system, "nurses", []))

    shortages = _collect_shortage_by_day(roster_system)
    shortage_total = sum(shortages)
    shortage_max = max(shortages) if shortages else 0
    shortage_mean = float(np.mean(shortages)) if shortages else 0.0

    avg_satisfaction = 0.0
    total_req = 0
    total_sat = 0
    try:
        ind = roster_system.calculate_individual_satisfaction()
        if ind:
            avg_satisfaction = float(np.mean([float(v.get("overall_satisfaction", 0.0)) for v in ind.values()]))
            total_req = int(sum(int(v.get("total_requests", 0)) for v in ind.values()))
            total_sat = int(sum(int(v.get("satisfied_requests", 0)) for v in ind.values()))
    except Exception:
        avg_satisfaction = 0.0

    req_rate = _safe_rate(total_sat, total_req)
    iter_done = int(lns_metrics.get("iter_executed", 0) or 0)
    max_iter = int(lns_metrics.get("max_iter", 0) or 0)
    iter_progress = _safe_rate(iter_done, max_iter if max_iter > 0 else 1)
    ok_rate = float(lns_metrics.get("ok_rate", 0.0) or 0.0)
    improve_rate = float(lns_metrics.get("improve_rate", 0.0) or 0.0)

    vector = [
        float(hard_count),
        float(shortage_total),
        float(shortage_max),
        float(shortage_mean),
        float(avg_satisfaction / 100.0),
        float(req_rate),
        float(iter_progress),
        float(ok_rate),
        float(improve_rate),
        float(nurse_count),
        float(day_count),
    ]

    features = {
        "violation_count": hard_count,
        "shortages_by_day": shortages,
        "shortage_total": shortage_total,
        "shortage_max": shortage_max,
        "avg_satisfaction": avg_satisfaction,
        "requested_total": total_req,
        "satisfied_total": total_sat,
        "request_satisfaction_rate": req_rate,
    }
    return LNSState(vector=vector, features=features)
