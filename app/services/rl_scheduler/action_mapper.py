from __future__ import annotations

import random


def _sample_unique(values: list[int], k: int) -> list[int]:
    if not values:
        return []
    if len(values) <= k:
        return list(dict.fromkeys(values))
    return random.sample(list(dict.fromkeys(values)), k)


def _all_nurse_indices(roster_system) -> list[int]:
    return list(range(len(getattr(roster_system, "nurses", []))))


def _all_day_indices(roster_system) -> list[int]:
    return list(range(int(getattr(roster_system, "num_days", 0))))


def _violation_focus(roster_system, k_n: int, k_d: int) -> tuple[list[int], list[int]]:
    nurse_candidates: list[int] = []
    day_candidates: list[int] = []
    for v in roster_system._find_violations():
        n_idx = v.get("nurse_index")
        d_idx = v.get("day_index")
        if isinstance(n_idx, int):
            nurse_candidates.append(n_idx)
        if isinstance(d_idx, int):
            day_candidates.append(d_idx)
    all_n = _all_nurse_indices(roster_system)
    all_d = _all_day_indices(roster_system)
    if not nurse_candidates:
        nurse_candidates = all_n
    if not day_candidates:
        day_candidates = all_d
    return _sample_unique(nurse_candidates, k_n), _sample_unique(day_candidates, k_d)


def _shortage_focus(roster_system, k_n: int, k_d: int) -> tuple[list[int], list[int]]:
    cfg = roster_system.config
    shift_types = getattr(cfg, "shift_types", [])
    req = getattr(cfg, "daily_shift_requirements", {}) or {}
    day_count = int(getattr(roster_system, "num_days", 0))
    day_shortages: list[tuple[int, int]] = []
    for d in range(day_count):
        shortage = 0
        for code, target in req.items():
            if code not in shift_types:
                continue
            s_idx = shift_types.index(code)
            assigned = int((roster_system.roster[:, d, s_idx]).sum())
            shortage += max(0, int(target) - assigned)
        day_shortages.append((d, shortage))
    day_shortages.sort(key=lambda x: x[1], reverse=True)
    top_days = [d for d, s in day_shortages if s > 0][:k_d]
    if not top_days:
        top_days = _sample_unique(_all_day_indices(roster_system), k_d)
    nurses = _sample_unique(_all_nurse_indices(roster_system), k_n)
    return nurses, top_days


def _satisfaction_focus(roster_system, k_n: int, k_d: int) -> tuple[list[int], list[int]]:
    candidates: list[tuple[int, float]] = []
    by_nurse = roster_system.calculate_individual_satisfaction()
    id_to_idx = {str(n.db_id): i for i, n in enumerate(roster_system.nurses)}
    for nurse_id, info in by_nurse.items():
        idx = id_to_idx.get(str(nurse_id))
        if idx is None:
            continue
        sat = float(info.get("overall_satisfaction", 100.0))
        candidates.append((idx, sat))
    candidates.sort(key=lambda x: x[1])
    n_sel = [idx for idx, _ in candidates[:k_n]]
    if not n_sel:
        n_sel = _sample_unique(_all_nurse_indices(roster_system), k_n)
    d_sel = _sample_unique(_all_day_indices(roster_system), k_d)
    return n_sel, d_sel


class LNSActionMapper:
    def __init__(self):
        self._operators = [
            "random",
            "violation_focus",
            "shortage_focus",
            "satisfaction_focus",
        ]

    def action_space(self) -> int:
        return len(self._operators)

    def action_name(self, action_id: int) -> str:
        if action_id < 0 or action_id >= len(self._operators):
            return "random"
        return self._operators[action_id]

    def to_neighborhood(self, roster_system, obs, action_id: int, *, k_n: int, k_d: int) -> tuple[list[int], list[int], dict]:
        op = self.action_name(action_id)
        if op == "violation_focus":
            n_sel, d_sel = _violation_focus(roster_system, k_n, k_d)
        elif op == "shortage_focus":
            n_sel, d_sel = _shortage_focus(roster_system, k_n, k_d)
        elif op == "satisfaction_focus":
            n_sel, d_sel = _satisfaction_focus(roster_system, k_n, k_d)
        else:
            n_sel = _sample_unique(_all_nurse_indices(roster_system), k_n)
            d_sel = _sample_unique(_all_day_indices(roster_system), k_d)
        if not n_sel:
            n_sel = _sample_unique(_all_nurse_indices(roster_system), k_n)
        if not d_sel:
            d_sel = _sample_unique(_all_day_indices(roster_system), k_d)
        return n_sel, d_sel, {"operator": op, "action_id": int(action_id)}
