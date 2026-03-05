from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any


def _get_value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def resolve_effective_off_days(source: Any) -> tuple[int, str]:
    raw_off_days = _get_value(source, "off_days", None)
    if raw_off_days is not None:
        return max(0, _as_int(raw_off_days, 8)), "off_days"

    legacy_standard = _get_value(source, "standard_personal_off_days", None)
    legacy_global = _get_value(source, "global_monthly_off_days", None)
    if legacy_standard is not None or legacy_global is not None:
        return max(0, _as_int(legacy_standard, 0) + _as_int(legacy_global, 0)), "legacy_standard_plus_global"

    return 8, "default_8"


def resolve_max_extra_off_days(source: Any, default: int = 0) -> int:
    return max(0, _as_int(_get_value(source, "max_extra_off_days", default), default))


def resolve_weekend_counts_toward_max(source: Any) -> bool:
    return bool(_get_value(source, "weekend_off_counts_toward_max_off", False))


def compute_off_bounds(*, source: Any, avail_days: int, vacation_cnt: int) -> dict[str, int | str]:
    effective_off_days, source_key = resolve_effective_off_days(source)
    min_off_required = max(0, min(effective_off_days, max(0, avail_days - max(0, vacation_cnt))))
    max_extra_off_days = resolve_max_extra_off_days(source, 0)
    max_off_allowed = min(min_off_required + max_extra_off_days, max(0, avail_days))
    return {
        "effective_off_days": effective_off_days,
        "effective_off_days_source": source_key,
        "min_off_required": min_off_required,
        "max_off_allowed": max_off_allowed,
        "max_extra_off_days": max_extra_off_days,
    }


def build_off_partitions(
    *,
    nurses: list[Any],
    num_days: int,
    first_day: date,
    fixed_off_cells: set[tuple[int, int]],
    fixed_vacation_off_cells: set[tuple[int, int]],
    off_exception_cells: set[tuple[int, int]] | None = None,
    off_exception_vacation_cells: set[tuple[int, int]] | None = None,
    weekly_off_by_idx: dict[int, list[int]] | None = None,
    weekend_off_only_enable: bool = True,
    include_off_exception_cells: bool = True,
    include_weekly_off_cells: bool = True,
    include_weekend_off_cells: bool = True,
    weekend_within_active_range: bool = False,
    join: list[int] | None = None,
    leave: list[int] | None = None,
    fixed_non_off_cells: set[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    vacation_off_cells: set[tuple[int, int]] = set(off_exception_vacation_cells or set())
    vacation_off_cells.update(fixed_vacation_off_cells)
    structural_off_cells: set[tuple[int, int]] = set()
    structural_off_cells.update({cell for cell in fixed_off_cells if cell not in vacation_off_cells})
    if include_off_exception_cells:
        structural_off_cells.update({cell for cell in (off_exception_cells or set()) if cell not in vacation_off_cells})
    if include_weekly_off_cells:
        for n_idx, days in (weekly_off_by_idx or {}).items():
            for d in (days or []):
                try:
                    d_idx = int(d)
                except Exception:
                    continue
                structural_off_cells.add((int(n_idx), d_idx))

    weekend_days = {d for d in range(num_days) if (first_day + timedelta(days=d)).weekday() >= 5}
    if include_weekend_off_cells and weekend_off_only_enable:
        for n_idx, nurse in enumerate(nurses):
            if not bool(getattr(nurse, "is_weekend_off", False)):
                continue
            for d_idx in weekend_days:
                if weekend_within_active_range and join is not None and leave is not None:
                    if n_idx >= len(join) or n_idx >= len(leave):
                        continue
                    if not (join[n_idx] <= d_idx <= leave[n_idx]):
                        continue
                if fixed_non_off_cells and (n_idx, d_idx) in fixed_non_off_cells:
                    continue
                structural_off_cells.add((n_idx, d_idx))

    natural_off_cells = {
        (n, d)
        for n in range(len(nurses))
        for d in range(num_days)
        if (n, d) not in structural_off_cells and (n, d) not in vacation_off_cells
    }
    return {
        "vacation_off_cells": vacation_off_cells,
        "structural_off_cells": structural_off_cells,
        "natural_off_cells": natural_off_cells,
        "weekend_days": weekend_days,
    }
