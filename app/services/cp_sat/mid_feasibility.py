from __future__ import annotations

import calendar
from typing import Any

from services.cp_sat.shift_normalizer import build_shift_normalizer, normalize_shift_code


def validate_mid_hard_feasibility(
    nurses_in_group: list[Any],
    config_dict: dict[str, Any],
    year: int,
    month: int,
) -> str | None:
    if not bool(config_dict.get("use_mid", False)):
        return None

    num_days = calendar.monthrange(int(year), int(month))[1]
    req_by_day: list[int] = []
    ds_by_day = config_dict.get("daily_shift_requirements_by_day")
    if isinstance(ds_by_day, list) and ds_by_day:
        for d in range(num_days):
            if d < len(ds_by_day) and isinstance(ds_by_day[d], dict):
                req_by_day.append(max(0, int((ds_by_day[d] or {}).get("M", 0) or 0)))
            else:
                req_by_day.append(0)
    else:
        base_map = config_dict.get("daily_shift_requirements") or {}
        base_req = max(0, int((base_map or {}).get("M", 0) or 0))
        req_by_day = [base_req for _ in range(num_days)]

    shift_id_to_main, _ = build_shift_normalizer(config_dict.get("shift_definitions"))
    shift_id_to_main.setdefault("OFF", "O")
    shift_id_to_main.setdefault("O", "O")
    shift_id_to_main.setdefault("주", "O")

    fixed_by_day_nurse: dict[tuple[int, int], str] = {}
    for cell in (config_dict.get("fixed_cells") or []):
        try:
            n_idx = int(cell.get("nurse_index"))
            d_idx = int(cell.get("day_index"))
        except Exception:
            continue
        if d_idx < 0 or d_idx >= num_days:
            continue
        raw = str(cell.get("shift") or "-").strip()
        normalized = normalize_shift_code(raw, shift_id_to_main)
        if normalized:
            fixed_by_day_nurse[(n_idx, d_idx)] = normalized

    initial_constraints = config_dict.get("initial_constraints") or {}
    forced_off_src = initial_constraints.get("forced_off") or {}
    forbidden_src = initial_constraints.get("forbidden") or {}

    forced_off: dict[str, set[int]] = {}
    for nurse_id, days in (forced_off_src or {}).items():
        key = str(nurse_id)
        forced_off[key] = set()
        for d in (days or []):
            try:
                forced_off[key].add(int(d))
            except Exception:
                continue

    forbidden: dict[str, dict[int, set[str]]] = {}
    for nurse_id, day_map in (forbidden_src or {}).items():
        key = str(nurse_id)
        forbidden[key] = {}
        for d, codes in (day_map or {}).items():
            try:
                d_idx = int(d)
            except Exception:
                continue
            forbidden[key][d_idx] = {str(c).strip().upper() for c in (codes or [])}

    nurse_ids = [str(getattr(n, "nurse_id", "") or "") for n in (nurses_in_group or [])]

    adjusted_days: list[tuple[int, int, int]] = []
    for d in range(num_days):
        req_m = req_by_day[d]
        fixed_m = 0
        variable_capacity = 0
        for n_idx, nurse_id in enumerate(nurse_ids):
            fixed_code = fixed_by_day_nurse.get((n_idx, d))
            if fixed_code is not None:
                if fixed_code == "M":
                    fixed_m += 1
                continue

            if d in forced_off.get(nurse_id, set()):
                continue
            if "M" in forbidden.get(nurse_id, {}).get(d, set()):
                continue
            variable_capacity += 1

        min_possible = fixed_m
        max_possible = fixed_m + variable_capacity
        if req_m < min_possible:
            req_by_day[d] = min_possible
            adjusted_days.append((d, req_m, min_possible))
            req_m = min_possible
        if req_m > max_possible:
            return (
                f"M 하드 제약 불가능: {d + 1}일 M 요구={req_m}, "
                f"가능 범위=[{min_possible},{max_possible}]"
            )

    if adjusted_days:
        if isinstance(ds_by_day, list):
            normalized_req = list(ds_by_day)
        else:
            normalized_req = []
        base_map = config_dict.get("daily_shift_requirements")
        for d in range(num_days):
            if d < len(normalized_req) and isinstance(normalized_req[d], dict):
                day_map = dict(normalized_req[d])
            elif isinstance(base_map, dict):
                day_map = dict(base_map)
            else:
                day_map = {}
            day_map["M"] = int(req_by_day[d])
            if d < len(normalized_req):
                normalized_req[d] = day_map
            else:
                normalized_req.append(day_map)
        config_dict["daily_shift_requirements_by_day"] = normalized_req
        try:
            details = ", ".join(
                f"{d + 1}일:{old_req}->{new_req}" for d, old_req, new_req in adjusted_days
            )
            print(
                "[M-Feasibility] fixed M 반영으로 일별 M 요구치를 상향 조정: "
                f"{details}"
            )
        except Exception:
            pass

    return None
