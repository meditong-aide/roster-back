from __future__ import annotations

from ortools.sat.python import cp_model


def _team_members(rs) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for idx, nurse in enumerate(rs.nurses):
        team_id = getattr(nurse, "team_id", None)
        if team_id in (None, "", 0):
            continue
        out.setdefault(str(team_id), []).append(idx)
    return out


def _target_shift_codes(cfg) -> list[str]:
    focus_codes = getattr(cfg, "team_balance_focus_shifts", None)
    if focus_codes:
        codes = [c for c in focus_codes if c in cfg.daily_shift_requirements.keys()]
    else:
        codes = list(cfg.daily_shift_requirements.keys())
    return [c for c in codes if c in cfg.shift_types and c != "O"]


def _avg_surplus_ratio(cfg, join: list[int], leave: list[int], num_days: int, shift_indices: list[int], shift_codes: list[str]) -> float:
    ds_by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
    req_map = getattr(cfg, "daily_shift_requirements", {}) or {}
    ratios: list[float] = []
    for d in range(num_days):
        available = sum(1 for n in range(len(join)) if join[n] <= d <= leave[n])
        if available <= 0:
            continue
        if isinstance(ds_by_day, list) and d < len(ds_by_day) and isinstance(ds_by_day[d], dict):
            day_req = ds_by_day[d]
        else:
            day_req = req_map
        required = 0
        for code, _ in zip(shift_codes, shift_indices):
            required += max(0, int((day_req or {}).get(code, req_map.get(code, 0)) or 0))
        surplus = max(0, available - required)
        ratios.append(float(surplus) / float(max(1, available)))
    if not ratios:
        return 0.0
    return sum(ratios) / len(ratios)


def add_team_balance_objective_terms_v2(m: cp_model.CpModel, rs, X, join, leave) -> list:
    cfg = rs.config
    obj_terms: list = []

    if not getattr(cfg, "team_balance_enable", False):
        return obj_terms

    base_weight = int(getattr(cfg, "team_balance_weight", 0) or 0)
    if base_weight <= 0:
        return obj_terms

    team_members = _team_members(rs)
    if not team_members:
        return obj_terms

    shift_codes = _target_shift_codes(cfg)
    if not shift_codes:
        return obj_terms

    shift_indices = [cfg.shift_types.index(c) for c in shift_codes]

    avg_surplus_ratio = _avg_surplus_ratio(cfg, join, leave, rs.num_days, shift_indices, shift_codes)
    if avg_surplus_ratio >= 0.55:
        cohesion_mult = 0.45
    elif avg_surplus_ratio >= 0.40:
        cohesion_mult = 0.6
    elif avg_surplus_ratio >= 0.25:
        cohesion_mult = 0.8
    else:
        cohesion_mult = 1.0

    mismatch_weight = max(1, int(round(base_weight * 1.0 * cohesion_mult)))
    step_weight = max(1, int(round(base_weight * 0.22 * cohesion_mult)))
    max_step_k = int(getattr(cfg, "team_balance_step_cap", 3) or 3)
    max_step_k = max(2, min(6, max_step_k))

    for _, members in team_members.items():
        if not members:
            continue
        team_size = len(members)
        for d in range(rs.num_days):
            work_expr_terms = []
            shift_count_expr = []
            for s_idx in shift_indices:
                c_terms = []
                for n in members:
                    if join[n] <= d <= leave[n]:
                        c_terms.append(X(n, d, s_idx))
                        work_expr_terms.append(X(n, d, s_idx))
                shift_count_expr.append(sum(c_terms) if c_terms else 0)

            if not work_expr_terms:
                continue

            max_same = m.NewIntVar(0, team_size, f"tb2_max_same_{d}_{members[0]}")
            for c_expr in shift_count_expr:
                m.Add(max_same >= c_expr)

            disagree = m.NewIntVar(0, team_size, f"tb2_disagree_{d}_{members[0]}")
            work_expr = sum(work_expr_terms)
            m.Add(disagree == work_expr - max_same)
            obj_terms.append(-mismatch_weight * disagree)

            step_upper = min(max_step_k, team_size)
            for k in range(2, step_upper + 1):
                at_least_k = m.NewBoolVar(f"tb2_ge_{k}_{d}_{members[0]}")
                m.Add(max_same >= k).OnlyEnforceIf(at_least_k)
                m.Add(max_same <= k - 1).OnlyEnforceIf(at_least_k.Not())
                obj_terms.append(step_weight * at_least_k)

    return obj_terms
