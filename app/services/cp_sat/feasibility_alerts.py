from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any

from services.cp_sat.allowed_shift_types import is_code_blocked_by_profile


CORE_CODES = ("D", "E", "N", "M")


def _load_off_policy_helpers():
    module = __import__("services.cp_sat.off_policy", fromlist=["*"])
    return (
        getattr(module, "build_off_partitions"),
        getattr(module, "compute_off_bounds"),
        getattr(module, "resolve_effective_off_days"),
        getattr(module, "resolve_max_extra_off_days"),
        getattr(module, "resolve_weekend_counts_toward_max"),
    )


def _normalize_shift_code(value: object) -> str:
    code = str(value or "").strip().upper()
    if code in {"OFF", "주"}:
        return "O"
    return code


def _daily_requirements(config_dict: dict[str, Any], num_days: int) -> list[dict[str, int]]:
    out: list[dict[str, int]] = []
    by_day = config_dict.get("daily_shift_requirements_by_day")
    base = config_dict.get("daily_shift_requirements") or {}
    for d in range(num_days):
        if isinstance(by_day, list) and d < len(by_day) and isinstance(by_day[d], dict):
            src = by_day[d]
        else:
            src = base
        row: dict[str, int] = {}
        for code in CORE_CODES:
            row[code] = max(0, int((src or {}).get(code, 0) or 0))
        out.append(row)
    return out


def _build_fixed_map(config_dict: dict[str, Any], num_days: int) -> dict[tuple[int, int], str]:
    fixed: dict[tuple[int, int], str] = {}
    for cell in (config_dict.get("fixed_cells") or []):
        try:
            n_idx = int(cell.get("nurse_index"))
            d_idx = int(cell.get("day_index"))
        except Exception:
            continue
        if d_idx < 0 or d_idx >= num_days:
            continue
        fixed[(n_idx, d_idx)] = _normalize_shift_code(cell.get("shift"))
    return fixed


def _build_constraint_maps(
    config_dict: dict[str, Any],
) -> tuple[dict[str, set[int]], dict[str, dict[int, set[str]]]]:
    initial = config_dict.get("initial_constraints") or {}
    forced_off_src = initial.get("forced_off") or {}
    forbidden_src = initial.get("forbidden") or {}

    forced_off: dict[str, set[int]] = {}
    for nurse_id, days in forced_off_src.items():
        key = str(nurse_id)
        forced_off[key] = set()
        for d in (days or []):
            try:
                forced_off[key].add(int(d))
            except Exception:
                continue

    forbidden: dict[str, dict[int, set[str]]] = {}
    for nurse_id, day_map in forbidden_src.items():
        key = str(nurse_id)
        forbidden[key] = {}
        for d, codes in (day_map or {}).items():
            try:
                day_idx = int(d)
            except Exception:
                continue
            forbidden[key][day_idx] = {str(c).strip().upper() for c in (codes or [])}
    return forced_off, forbidden


def _active_range(nurse, first_day: date, last_day: date) -> tuple[int, int] | None:
    raw_join = getattr(nurse, "joining_date", None)
    raw_leave = getattr(nurse, "resignation_date", None)
    join_src = raw_join.date() if isinstance(raw_join, datetime) else raw_join
    leave_src = raw_leave.date() if isinstance(raw_leave, datetime) else raw_leave
    if leave_src and leave_src < first_day:
        return None
    join_date = max(first_day, join_src or first_day)
    leave_date = min(last_day, leave_src or last_day)
    if join_date > leave_date:
        return None
    return (join_date - first_day).days, (leave_date - first_day).days


def _nurse_profile_blocks_code(nurse, code: str, use_mid: bool) -> bool:
    raw = getattr(nurse, "allowed_shifts", None)
    return is_code_blocked_by_profile(raw, code, use_mid=use_mid)


def _is_subset_sum_possible(target: int, sizes: list[int]) -> bool:
    if target == 0:
        return True
    possible = {0}
    for size in sizes:
        nxt = set(possible)
        for v in possible:
            nv = v + size
            if nv <= target:
                nxt.add(nv)
        possible = nxt
        if target in possible:
            return True
    return target in possible


def run_preflight_feasibility_alerts(
    *,
    nurses_in_group: list[Any],
    config_dict: dict[str, Any],
    year: int,
    month: int,
    logger_prefix: str = "[PreflightFeasibility]",
) -> list[str]:
    alerts: list[str] = []
    try:
        (
            build_off_partitions,
            compute_off_bounds,
            resolve_effective_off_days,
            resolve_max_extra_off_days,
            resolve_weekend_counts_toward_max,
        ) = _load_off_policy_helpers()
        num_days = calendar.monthrange(int(year), int(month))[1]
        first_day = date(int(year), int(month), 1)
        last_day = first_day + timedelta(days=num_days - 1)

        fixed = _build_fixed_map(config_dict, num_days)
        forced_off, forbidden = _build_constraint_maps(config_dict)
        req_by_day = _daily_requirements(config_dict, num_days)
        use_mid = bool(config_dict.get("use_mid", False))
        weekend_only_enable = bool(config_dict.get("weekend_off_only_enable", True))
        effective_off_days, effective_source = resolve_effective_off_days(config_dict)
        effective_extra = resolve_max_extra_off_days(config_dict, 0)
        weekend_counts_toward_max = resolve_weekend_counts_toward_max(config_dict)
        print(
            f"{logger_prefix} off_policy effective_off_days={effective_off_days}"
            f" source={effective_source} max_extra_off_days={effective_extra}"
            f" weekend_counts_toward_max={int(weekend_counts_toward_max)}"
        )

        nurse_ids = [str(getattr(n, "nurse_id", "") or "") for n in (nurses_in_group or [])]
        active_ranges = [_active_range(n, first_day, last_day) for n in (nurses_in_group or [])]
        fixed_off_cells = {(n, d) for (n, d), code in fixed.items() if code == "O"}
        fixed_non_off_cells = {(n, d) for (n, d), code in fixed.items() if code != "O"}
        off_exception_cells = {
            (int(n_idx), int(d_idx))
            for (n_idx, d_idx) in (config_dict.get("off_exception_cells") or [])
        }
        off_exception_vacation_cells = {
            (int(n_idx), int(d_idx))
            for (n_idx, d_idx) in (config_dict.get("off_exception_vacation_cells") or [])
        }
        join = [r[0] if r is not None else 1 for r in active_ranges]
        leave = [r[1] if r is not None else 0 for r in active_ranges]
        partition = build_off_partitions(
            nurses=list(nurses_in_group or []),
            num_days=num_days,
            first_day=first_day,
            fixed_off_cells=fixed_off_cells,
            fixed_vacation_off_cells=set(),
            off_exception_cells=off_exception_cells,
            off_exception_vacation_cells=off_exception_vacation_cells,
            weekly_off_by_idx=None,
            weekend_off_only_enable=weekend_only_enable,
            include_off_exception_cells=True,
            include_weekly_off_cells=False,
            include_weekend_off_cells=True,
            weekend_within_active_range=True,
            join=join,
            leave=leave,
            fixed_non_off_cells=fixed_non_off_cells,
            # 일자별 비가동 팀도 강제 OFF 다. 빼면 경보가 실제보다 여유 있게 나온다.
            daily_active_teams_by_day=(config_dict or {}).get(
                "daily_active_teams_by_day"
            ),
        )
        structural_off_cells = set(partition["structural_off_cells"])
        vacation_off_cells = set(partition["vacation_off_cells"])
        weekend_days = set(partition["weekend_days"])

        for d in range(num_days):
            req_map = req_by_day[d]
            day_codes = ["D", "E", "N", "M"] if use_mid else ["D", "E", "N"]
            fixed_by_code = {c: 0 for c in day_codes}
            var_cap = {c: 0 for c in day_codes}
            free_workers = 0
            fixed_workers = 0
            for n_idx, nurse in enumerate(nurses_in_group or []):
                r = active_ranges[n_idx]
                if r is None or d < r[0] or d > r[1]:
                    continue
                nurse_id = nurse_ids[n_idx]
                fixed_code = fixed.get((n_idx, d))
                if fixed_code is not None:
                    if fixed_code in day_codes:
                        fixed_by_code[fixed_code] += 1
                        fixed_workers += 1
                    elif fixed_code != "O":
                        fixed_workers += 1
                    continue
                if d in forced_off.get(nurse_id, set()):
                    continue
                free_workers += 1
                forbid_set = forbidden.get(nurse_id, {}).get(d, set())
                for code in day_codes:
                    if code in forbid_set:
                        continue
                    if _nurse_profile_blocks_code(nurse, code, use_mid):
                        continue
                    var_cap[code] += 1

            total_req = sum(req_map.get(c, 0) for c in day_codes)
            if total_req > fixed_workers + free_workers:
                alerts.append(
                    f"day={d+1}: total demand({total_req}) > worker capacity({fixed_workers + free_workers})"
                )
            for code in day_codes:
                req_val = req_map.get(code, 0)
                cap = fixed_by_code[code] + var_cap[code]
                if req_val > cap:
                    alerts.append(
                        f"day={d+1} code={code}: demand({req_val}) > capacity({cap}) [fixed={fixed_by_code[code]}, variable={var_cap[code]}]"
                    )

        if weekend_only_enable:
            weekend_days = {
                d for d in range(num_days) if (first_day + timedelta(days=d)).weekday() >= 5
            }
            for n_idx, nurse in enumerate(nurses_in_group or []):
                if not bool(getattr(nurse, "is_weekend_off", False)):
                    continue
                r = active_ranges[n_idx]
                if r is None:
                    continue
                t0, t1 = r
                avail_days = t1 - t0 + 1
                if avail_days <= 0:
                    continue
                weekend_slots = sum(1 for d in weekend_days if t0 <= d <= t1)
                vacation_cnt = sum(
                    1 for d in range(t0, t1 + 1) if (n_idx, d) in vacation_off_cells
                )
                weekend_slots_nonvac = sum(
                    1 for d in weekend_days if t0 <= d <= t1 and (n_idx, d) not in vacation_off_cells
                )
                bounds = compute_off_bounds(
                    source=config_dict,
                    avail_days=avail_days,
                    vacation_cnt=vacation_cnt,
                    reference_days=num_days,
                    weekend_only=True,
                    weekend_slots_nonvac=weekend_slots_nonvac,
                )
                min_off_required = int(bounds["min_off_required"])
                if min_off_required > weekend_slots:
                    alerts.append(
                        f"nurse={getattr(nurse, 'name', '?')}({nurse_ids[n_idx]}): weekend_only min_off conflict min={min_off_required}, weekend_slots={weekend_slots}"
                    )

        preceptee_on = bool(config_dict.get("preceptee_on", False))
        if preceptee_on and use_mid:
            id_to_idx = {nurse_ids[i]: i for i in range(len(nurse_ids))}
            # 멤버십: nurse_preceptee_period 맵 있으면 그 달 active 만(종료자 제외), 없으면 캐시. 설계 §6.
            _pte_auth_f = bool(config_dict.get("preceptee_period_authoritative"))
            _pte_map = config_dict.get("preceptee_period_by_nurse_id")
            _active_pte = ({str(k) for k, v in (_pte_map or {}).items()
                            if (v.get("days") if isinstance(v, dict) else v)} if _pte_auth_f else None)
            preceptor_to_members: dict[str, list[int]] = {}
            for idx, nurse in enumerate(nurses_in_group or []):
                pid = str(getattr(nurse, "preceptor_id", "") or "")
                if not pid or pid not in id_to_idx:
                    continue
                if _active_pte is not None and str(nurse_ids[idx]) not in _active_pte:
                    continue  # 권위 모드: 그 달 프리셉티 아님 → M 피저빌리티 그룹 제외
                preceptor_to_members.setdefault(pid, [id_to_idx[pid]])
                if idx not in preceptor_to_members[pid]:
                    preceptor_to_members[pid].append(idx)

            covered = set()
            units: list[list[int]] = []
            for members in preceptor_to_members.values():
                uniq_members = sorted(set(members))
                units.append(uniq_members)
                covered.update(uniq_members)
            for idx in range(len(nurse_ids)):
                if idx not in covered:
                    units.append([idx])

            for d in range(num_days):
                req_m = req_by_day[d].get("M", 0)
                if req_m <= 0:
                    continue
                fixed_m = 0
                unit_sizes: list[int] = []
                for unit in units:
                    unit_possible = True
                    unit_fixed_m = 0
                    for idx in unit:
                        r = active_ranges[idx]
                        if r is None or d < r[0] or d > r[1]:
                            unit_possible = False
                            break
                        nurse_id = nurse_ids[idx]
                        fixed_code = fixed.get((idx, d))
                        if fixed_code is not None:
                            if fixed_code == "M":
                                unit_fixed_m += 1
                            else:
                                unit_possible = False
                                break
                        elif d in forced_off.get(nurse_id, set()):
                            unit_possible = False
                            break
                        elif "M" in forbidden.get(nurse_id, {}).get(d, set()):
                            unit_possible = False
                            break
                        elif _nurse_profile_blocks_code(nurses_in_group[idx], "M", use_mid):
                            unit_possible = False
                            break
                    if unit_fixed_m > 0:
                        fixed_m += unit_fixed_m
                    elif unit_possible:
                        unit_sizes.append(len(unit))

                remaining = max(0, req_m - fixed_m)
                if remaining > 0 and not _is_subset_sum_possible(remaining, unit_sizes):
                    alerts.append(
                        f"day={d+1} M: preceptee unit sizes={sorted(unit_sizes)} cannot meet remaining demand={remaining} (req={req_m}, fixedM={fixed_m})"
                    )

        for n_idx, nurse in enumerate(nurses_in_group or []):
            r = active_ranges[n_idx]
            if r is None:
                continue
            t0, t1 = r
            avail_days = t1 - t0 + 1
            if avail_days <= 0:
                continue
            nurse_id = nurse_ids[n_idx]

            forced_nonvac: set[int] = {
                d
                for d in range(t0, t1 + 1)
                if (n_idx, d) in structural_off_cells and (n_idx, d) not in vacation_off_cells
            }
            for d in forced_off.get(nurse_id, set()):
                if t0 <= d <= t1 and (n_idx, d) not in vacation_off_cells:
                    forced_nonvac.add(d)
            if (
                weekend_counts_toward_max
                and weekend_only_enable
                and bool(getattr(nurse, "is_weekend_off", False))
            ):
                forced_nonvac.update({d for d in weekend_days if t0 <= d <= t1})

            vacation_cnt = sum(1 for d in range(t0, t1 + 1) if (n_idx, d) in vacation_off_cells)
            weekend_slots_nonvac = sum(
                1 for d in weekend_days if t0 <= d <= t1 and (n_idx, d) not in vacation_off_cells
            )
            bounds = compute_off_bounds(
                source=config_dict,
                avail_days=avail_days,
                vacation_cnt=vacation_cnt,
                reference_days=num_days,
                weekend_only=bool(getattr(nurse, "is_weekend_off", False)) and weekend_only_enable,
                weekend_slots_nonvac=weekend_slots_nonvac,
            )
            max_off_allowed = int(bounds["max_off_allowed"])
            if len(forced_nonvac) > max_off_allowed:
                alerts.append(
                    f"nurse={getattr(nurse, 'name', '?')}({nurse_id}): forced_nonvac_off={len(forced_nonvac)} > max_off_allowed={max_off_allowed}"
                )

    except Exception as exc:
        print(f"{logger_prefix} checker failure: {exc}")
        return []

    if alerts:
        print(f"{logger_prefix} alerts={len(alerts)}")
        for msg in alerts[:80]:
            print(f"{logger_prefix} {msg}")
        if len(alerts) > 80:
            print(f"{logger_prefix} ... truncated {len(alerts) - 80} additional alerts")
    else:
        print(f"{logger_prefix} no obvious hard-feasibility alerts")
    return alerts
