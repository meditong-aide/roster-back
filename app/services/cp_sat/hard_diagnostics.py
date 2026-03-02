from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta


LEGACY_HARD_TYPES = {
    "shift_requirement",
    "night_consecutive",
    "consecutive_work",
    "night_nd",
    "night_ne",
    "eve_ed",
    "night_month_limit",
    "rec_3n2o",
    "rec_2n2o",
}


@dataclass
class GlobalErrorIndicator:
    is_feasible: bool
    error_summary: str
    primary_issues: list[str]
    recommendations: list[str]


@dataclass
class HardDiagnosticsResult:
    legacy_hard_count: int
    legacy_by_type: dict[str, int]
    expanded_hard_count: int
    expanded_by_type: dict[str, int]
    mismatch_by_type: dict[str, int]
    sample_rows: dict[str, list[str]]
    structural_coverage_hints: list[dict[str, int | str]]
    off_partition_counts: dict[str, int]
    off_regulation_counts: dict[str, int]
    global_error_indicator: GlobalErrorIndicator


def _day_to_date(rs, day_idx: int):
    return rs.target_month + timedelta(days=day_idx)


def _assigned_shift_idx(rs, nurse_idx: int, day_idx: int) -> int | None:
    row = rs.roster[nurse_idx, day_idx]
    on = [i for i, v in enumerate(row) if int(v) == 1]
    if len(on) != 1:
        return None
    return on[0]


def _to_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except Exception:
            return None
    return None


def _build_sample(nurse_idx: int | None, day_idx: int | None, label: str, rs) -> str:
    nurse_part = "nurse_idx=?"
    if isinstance(nurse_idx, int) and 0 <= nurse_idx < len(rs.nurses):
        nu = rs.nurses[nurse_idx]
        nurse_part = f"nurse_idx={nurse_idx}, id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}"
    day_part = "day=?"
    if isinstance(day_idx, int):
        day_part = f"day={day_idx + 1}"
    return f"{day_part}, {nurse_part}, {label}"


def _build_global_error_indicator(
    expanded_hard_count: int,
    expanded_by_type: dict[str, int],
    structural_coverage_hints: list[dict[str, int | str]],
    off_partition_counts: dict[str, int],
    off_regulation_counts: dict[str, int],
) -> GlobalErrorIndicator:
    issues: list[str] = []
    recommendations: list[str] = []

    partition_sum = int(off_partition_counts.get("V", 0)) + int(off_partition_counts.get("Wo", 0)) + int(
        off_partition_counts.get("O", 0)
    )
    off_total = int(off_partition_counts.get("off_total", 0))
    partition_mismatch = max(0, abs(off_total - partition_sum))

    if partition_mismatch > 0:
        issues.append(f"OFF partition mismatch: off_total={off_total}, V+Wo+O={partition_sum}")
        recommendations.append(
            "Check vacation/weekly-off source mapping so OFF cells are classified as V, Wo, O without leakage"
        )

    if structural_coverage_hints:
        top = structural_coverage_hints[0]
        issues.append(
            f"Structural coverage deficit on day={int(top.get('day', 0))}, deficit={int(top.get('deficit', 0))}"
        )
        recommendations.append(
            "Reduce fixed/forbidden pressure or relax daily required headcount for deficit day/shift"
        )

    if int(expanded_by_type.get("initial_forbidden", 0)) > 0:
        issues.append("Initial-forbidden conflict exists")
        recommendations.append("Review forbidden shift set and relax over-constrained nurse/day blocks")

    if int(expanded_by_type.get("off_max", 0)) > 0:
        issues.append("OFF upper-bound exceeded")
        recommendations.append("Increase max_extra_off_days or rebalance forced-O cells (Wo/weekend/fixed)")

    if int(expanded_by_type.get("off_min", 0)) > 0:
        issues.append("OFF lower-bound shortage exists")
        recommendations.append("Increase OFF allocation or reduce hard work/fixed assignments")

    weekend_issues = int(off_regulation_counts.get("weekend_off_weekday_natural_o", 0)) + int(
        off_regulation_counts.get("weekend_off_missing_weekend_o", 0)
    )
    if weekend_issues > 0:
        issues.append("Weekend-off policy violations detected")
        recommendations.append("Ensure weekend-off nurses have O on weekends and avoid weekday natural O for that cohort")

    is_feasible = expanded_hard_count == 0 and not structural_coverage_hints and partition_mismatch == 0
    if is_feasible:
        summary = "No hard-constraint diagnostic issue detected"
    else:
        summary = f"Detected {len(issues)} hard-constraint risk area(s)"

    dedup_recommendations: list[str] = []
    for item in recommendations:
        if item not in dedup_recommendations:
            dedup_recommendations.append(item)

    return GlobalErrorIndicator(
        is_feasible=is_feasible,
        error_summary=summary,
        primary_issues=issues,
        recommendations=dedup_recommendations,
    )


def collect_hard_diagnostics(rs, sample_cap: int = 3) -> HardDiagnosticsResult:
    violations = rs._find_violations()
    legacy_by_type: dict[str, int] = defaultdict(int)
    for v in violations:
        v_type = str(v.get("type", "unknown"))
        if v_type in LEGACY_HARD_TYPES:
            legacy_by_type[v_type] += 1

    expanded_by_type: dict[str, int] = dict(legacy_by_type)
    sample_rows: dict[str, list[str]] = {}

    def add_violation(v_type: str, nurse_idx: int | None, day_idx: int | None, label: str):
        expanded_by_type[v_type] = expanded_by_type.get(v_type, 0) + 1
        rows = sample_rows.setdefault(v_type, [])
        if len(rows) < sample_cap:
            rows.append(_build_sample(nurse_idx, day_idx, label, rs))

    if rs.roster is not None:
        n_count = len(rs.nurses)
        d_count = rs.num_days
        s_count = rs.config.num_shifts

        for n in range(n_count):
            for d in range(d_count):
                assigned_cnt = int(sum(int(rs.roster[n, d, s]) for s in range(s_count)))
                if assigned_cnt != 1:
                    add_violation(
                        "exactly_one",
                        n,
                        d,
                        f"assigned_cnt={assigned_cnt}",
                    )

        initial_forbidden = getattr(rs, "initial_forbidden", {})
        if isinstance(initial_forbidden, dict):
            shift_types = list(getattr(rs.config, "shift_types", []) or [])
            for key, code_set in initial_forbidden.items():
                if not isinstance(key, tuple) or len(key) != 2:
                    continue
                n, d = key
                if not (isinstance(n, int) and isinstance(d, int)):
                    continue
                if n < 0 or n >= n_count or d < 0 or d >= d_count:
                    continue
                s_idx = _assigned_shift_idx(rs, n, d)
                if s_idx is None:
                    continue
                code = shift_types[s_idx] if 0 <= s_idx < len(shift_types) else str(s_idx)
                if str(code).strip().upper() in {str(c).strip().upper() for c in (code_set or set())}:
                    add_violation(
                        "initial_forbidden",
                        n,
                        d,
                        f"assigned={code}",
                    )

        if getattr(rs.config, "weekend_off_only_enable", True):
            off_idx = None
            shift_types = list(getattr(rs.config, "shift_types", []) or [])
            if "O" in shift_types:
                off_idx = shift_types.index("O")
            for n, nu in enumerate(rs.nurses):
                if not bool(getattr(nu, "is_weekend_off", False)):
                    continue
                for d in range(d_count):
                    wd = _day_to_date(rs, d).weekday()
                    s_idx = _assigned_shift_idx(rs, n, d)
                    if s_idx is None:
                        continue
                    if wd >= 5:
                        if off_idx is not None and s_idx != off_idx:
                            code = shift_types[s_idx] if 0 <= s_idx < len(shift_types) else str(s_idx)
                            add_violation("weekend_off_only_weekend", n, d, f"assigned={code}")
                    else:
                        if off_idx is not None and s_idx == off_idx:
                            add_violation("weekend_off_only_weekday", n, d, "assigned=O")

        shift_types = list(getattr(rs.config, "shift_types", []) or [])
        off_idx = shift_types.index("O") if "O" in shift_types else None
        if off_idx is not None:
            base_min = int(getattr(rs.config, "global_monthly_off_days", 0) + getattr(rs.config, "standard_personal_off_days", 0))
            extra_allowed = int(getattr(rs.config, "max_extra_off_days", 0) or 0)
            for n, nu in enumerate(rs.nurses):
                if bool(getattr(nu, "is_weekend_off", False)):
                    continue
                total_off = int(sum(int(rs.roster[n, d, off_idx]) for d in range(d_count)))
                min_off_required = max(0, min(base_min, d_count))
                max_off_allowed = min(min_off_required + max(0, extra_allowed), d_count)
                if total_off < min_off_required:
                    add_violation(
                        "off_min",
                        n,
                        None,
                        f"actual={total_off}, min={min_off_required}",
                    )
                if total_off > max_off_allowed:
                    add_violation(
                        "off_max",
                        n,
                        None,
                        f"actual={total_off}, max={max_off_allowed}",
                    )

        preceptee_follow = bool(getattr(rs.config, "preceptee_on", False))
        if preceptee_follow:
            id_to_idx = {getattr(nu, "db_id", None): i for i, nu in enumerate(rs.nurses)}
            for n, nu in enumerate(rs.nurses):
                pid = getattr(nu, "preceptor_id", None)
                p = id_to_idx.get(pid)
                if p is None:
                    continue
                for d in range(d_count):
                    s_n = _assigned_shift_idx(rs, n, d)
                    s_p = _assigned_shift_idx(rs, p, d)
                    if s_n is None or s_p is None:
                        continue
                    if s_n != s_p:
                        shift_types = list(getattr(rs.config, "shift_types", []) or [])
                        code_n = shift_types[s_n] if 0 <= s_n < len(shift_types) else str(s_n)
                        code_p = shift_types[s_p] if 0 <= s_p < len(shift_types) else str(s_p)
                        add_violation(
                            "preceptee_follow",
                            n,
                            d,
                            f"preceptee={code_n}, preceptor={code_p}",
                        )

    legacy_hard_count = sum(legacy_by_type.values())
    expanded_hard_count = sum(expanded_by_type.values())
    mismatch_by_type: dict[str, int] = {}
    all_types = set(expanded_by_type.keys()) | set(legacy_by_type.keys())
    for t in sorted(all_types):
        legacy_cnt = int(legacy_by_type.get(t, 0))
        expanded_cnt = int(expanded_by_type.get(t, 0))
        if legacy_cnt != expanded_cnt:
            mismatch_by_type[t] = expanded_cnt - legacy_cnt

    structural_coverage_hints: list[dict[str, int | str]] = []
    shift_types = list(getattr(rs.config, "shift_types", []) or [])
    fixed_cells = list(getattr(rs, "fixed_cells", []) or [])
    weekly_off_by_idx = getattr(rs, "weekly_off_by_idx", {})
    initial_forbidden = getattr(rs, "initial_forbidden", {})
    by_day_fixed: dict[int, list[dict[str, object]]] = defaultdict(list)
    for c in fixed_cells:
        d = _to_int_or_none(c.get("day_index"))
        if d is None:
            continue
        by_day_fixed[d].append(c)

    for d in range(int(getattr(rs, "num_days", 0) or 0)):
        if (
            hasattr(rs.config, "daily_shift_requirements_by_day")
            and isinstance(rs.config.daily_shift_requirements_by_day, list)
            and d < len(rs.config.daily_shift_requirements_by_day)
        ):
            need_map = rs.config.daily_shift_requirements_by_day[d] or {}
        else:
            need_map = getattr(rs.config, "daily_shift_requirements", {}) or {}

        total_required = 0
        for code, req in (need_map or {}).items():
            if code in shift_types:
                total_required += max(0, _to_int_or_none(req) or 0)

        fixed_rows = by_day_fixed.get(d, [])
        fixed_off = 0
        fixed_work = 0
        fixed_taken_nurses: set[int] = set()
        for c in fixed_rows:
            n = _to_int_or_none(c.get("nurse_index"))
            if n is None:
                continue
            shift = str(c.get("shift", "") or "")
            fixed_taken_nurses.add(n)
            if shift == "O":
                fixed_off += 1
            else:
                fixed_work += 1

        weekend_forced = 0
        day_wd = _day_to_date(rs, d).weekday()
        if day_wd >= 5 and getattr(rs.config, "weekend_off_only_enable", True):
            for n, nu in enumerate(rs.nurses):
                if n in fixed_taken_nurses:
                    continue
                if bool(getattr(nu, "is_weekend_off", False)):
                    weekend_forced += 1

        weekly_forced = 0
        if isinstance(weekly_off_by_idx, dict):
            for n, days in weekly_off_by_idx.items():
                n_idx = _to_int_or_none(n)
                if n_idx is None:
                    continue
                if n_idx in fixed_taken_nurses:
                    continue
                coerced_days: set[int] = set()
                for x in (days or []):
                    xx = _to_int_or_none(x)
                    if xx is not None:
                        coerced_days.add(xx)
                if d in coerced_days:
                    weekly_forced += 1

        lower_forced_off = fixed_off + weekend_forced + weekly_forced
        upper_assignable = max(0, len(rs.nurses) - lower_forced_off)
        if upper_assignable < total_required:
            structural_coverage_hints.append(
                {
                    "day": d + 1,
                    "required_total": total_required,
                    "upper_assignable": upper_assignable,
                    "deficit": total_required - upper_assignable,
                }
            )

        if isinstance(initial_forbidden, dict):
            for code, req in (need_map or {}).items():
                if code not in shift_types:
                    continue
                required_shift = max(0, _to_int_or_none(req) or 0)
                fixed_for_shift = 0
                blocked = 0
                for n in range(len(rs.nurses)):
                    if n in fixed_taken_nurses:
                        chosen = None
                        for c in fixed_rows:
                            n_fixed = _to_int_or_none(c.get("nurse_index"))
                            if n_fixed == n:
                                chosen = str(c.get("shift", "") or "")
                                break
                        if chosen == code:
                            fixed_for_shift += 1
                        elif chosen is not None:
                            blocked += 1
                        continue

                    forbid_set = initial_forbidden.get((n, d), set())
                    if str(code).strip().upper() in {str(x).strip().upper() for x in (forbid_set or set())}:
                        blocked += 1

                candidate_capacity = max(0, len(rs.nurses) - blocked)
                if candidate_capacity < required_shift:
                    structural_coverage_hints.append(
                        {
                            "day": d + 1,
                            "shift": str(code),
                            "required": required_shift,
                            "candidate_capacity": candidate_capacity,
                            "deficit": required_shift - candidate_capacity,
                        }
                    )

    structural_coverage_hints = sorted(
        structural_coverage_hints,
        key=lambda x: (-int(x.get("deficit", 0)), int(x.get("day", 0))),
    )

    off_partition_counts: dict[str, int] = {"V": 0, "Wo": 0, "O": 0, "off_total": 0}
    off_regulation_counts: dict[str, int] = {
        "weekend_off_weekday_natural_o": 0,
        "weekend_off_missing_weekend_o": 0,
        "off_partition_mismatch": 0,
    }
    if rs.roster is not None:
        shift_types = list(getattr(rs.config, "shift_types", []) or [])
        off_idx = shift_types.index("O") if "O" in shift_types else None
        vac_cells = set(getattr(rs, "_diag_vacation_off_cells", set()) or set())
        weekly_map = getattr(rs, "_diag_weekly_off_by_idx", None)
        if not isinstance(weekly_map, dict):
            weekly_map = getattr(rs, "weekly_off_by_idx", {})
        weekend_days = getattr(rs, "_diag_weekend_days", None)
        if not isinstance(weekend_days, set):
            weekend_days = {
                d for d in range(int(getattr(rs, "num_days", 0) or 0)) if _day_to_date(rs, d).weekday() >= 5
            }
        if off_idx is not None:
            for n, nu in enumerate(rs.nurses):
                weekly_days_raw = weekly_map.get(n, []) if isinstance(weekly_map, dict) else []
                weekly_days = {_to_int_or_none(x) for x in (weekly_days_raw or [])}
                weekly_days = {d for d in weekly_days if isinstance(d, int)}
                for d in range(int(getattr(rs, "num_days", 0) or 0)):
                    if int(rs.roster[n, d, off_idx]) != 1:
                        if bool(getattr(nu, "is_weekend_off", False)) and d in weekend_days and (n, d) not in vac_cells:
                            add_violation("weekend_off_missing_weekend_o", n, d, "expected=O")
                            off_regulation_counts["weekend_off_missing_weekend_o"] += 1
                        continue
                    off_partition_counts["off_total"] += 1
                    if (n, d) in vac_cells:
                        off_partition_counts["V"] += 1
                    elif d in weekly_days:
                        off_partition_counts["Wo"] += 1
                    else:
                        off_partition_counts["O"] += 1
                        if bool(getattr(nu, "is_weekend_off", False)) and d not in weekend_days:
                            add_violation("weekend_off_weekday_natural_o", n, d, "weekday natural O")
                            off_regulation_counts["weekend_off_weekday_natural_o"] += 1

    partition_sum = int(off_partition_counts.get("V", 0)) + int(off_partition_counts.get("Wo", 0)) + int(
        off_partition_counts.get("O", 0)
    )
    off_total = int(off_partition_counts.get("off_total", 0))
    off_regulation_counts["off_partition_mismatch"] = max(0, abs(off_total - partition_sum))

    global_error_indicator = _build_global_error_indicator(
        expanded_hard_count=expanded_hard_count,
        expanded_by_type=expanded_by_type,
        structural_coverage_hints=structural_coverage_hints,
        off_partition_counts=off_partition_counts,
        off_regulation_counts=off_regulation_counts,
    )

    return HardDiagnosticsResult(
        legacy_hard_count=legacy_hard_count,
        legacy_by_type=dict(sorted(legacy_by_type.items(), key=lambda x: (-x[1], x[0]))),
        expanded_hard_count=expanded_hard_count,
        expanded_by_type=dict(sorted(expanded_by_type.items(), key=lambda x: (-x[1], x[0]))),
        mismatch_by_type=mismatch_by_type,
        sample_rows=sample_rows,
        structural_coverage_hints=structural_coverage_hints[:10],
        off_partition_counts=off_partition_counts,
        off_regulation_counts=off_regulation_counts,
        global_error_indicator=global_error_indicator,
    )


def log_hard_diagnostics(result: HardDiagnosticsResult, logger_prefix: str, stage: str):
    line = (
        f"{logger_prefix} [HardDiagV2] stage={stage}, "
        f"legacy_hard={result.legacy_hard_count}, expanded_hard={result.expanded_hard_count}"
    )
    print(line)
    if result.legacy_by_type:
        print(f"{logger_prefix} [HardDiagV2] legacy_by_type={result.legacy_by_type}")
    if result.expanded_by_type:
        print(f"{logger_prefix} [HardDiagV2] expanded_by_type={result.expanded_by_type}")
    if result.mismatch_by_type:
        print(f"{logger_prefix} [HardDiagV2] mismatch_by_type={result.mismatch_by_type}")
    if result.structural_coverage_hints:
        print(
            f"{logger_prefix} [HardDiagV2] structural_coverage_hints="
            f"{result.structural_coverage_hints}"
        )
    print(f"{logger_prefix} [HardDiagV2] off_partition_counts={result.off_partition_counts}")
    print(f"{logger_prefix} [HardDiagV2] off_regulation_counts={result.off_regulation_counts}")
    print(
        f"{logger_prefix} [HardDiagV2] global_error="
        f"{{'is_feasible': {result.global_error_indicator.is_feasible}, "
        f"'summary': {result.global_error_indicator.error_summary!r}, "
        f"'issues': {result.global_error_indicator.primary_issues}, "
        f"'recommendations': {result.global_error_indicator.recommendations}}}"
    )
    for v_type, rows in sorted(result.sample_rows.items()):
        if rows:
            print(f"{logger_prefix} [HardDiagV2] sample[{v_type}]={rows}")
