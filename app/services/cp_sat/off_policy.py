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


def off_cap_semantics_label() -> str:
    return "nonvac_total_off"


def compute_off_bounds(
    *,
    source: Any,
    avail_days: int,
    vacation_cnt: int,
    reference_days: int | None = None,
    weekend_only: bool = False,
    weekend_slots_nonvac: int | None = None,
    extra_min_off: int = 0,
) -> dict[str, int | str]:
    effective_off_days, source_key = resolve_effective_off_days(source)
    active_nonvac_days = max(0, avail_days - max(0, vacation_cnt))
    eligible_off_days = (
        max(0, _as_int(weekend_slots_nonvac, 0))
        if weekend_only
        else active_nonvac_days
    )

    if weekend_only:
        min_off_required = eligible_off_days
    else:
        min_off_required = max(0, min(effective_off_days, eligible_off_days))
        ref_days = _as_int(reference_days, 0)
        if ref_days <= 0:
            ref_days = _as_int(_get_value(source, "off_days_ratio_reference_days", 0), 0)
        if ref_days <= 0:
            ref_days = max(1, avail_days)
        # 부분활동(partial-month) 안전 스케일: avail_days < ref_days인 경우 항상 적용
        # — 휴가는 무관, 활동 일수만 기준 (active_range_off_ratio_enable과 별개)
        if effective_off_days > 0 and avail_days > 0 and avail_days < ref_days:
            scaled_partial = int(round(effective_off_days * (avail_days / max(1, ref_days))))
            min_off_required = max(0, min(min_off_required, scaled_partial))
        # 휴가 비례 축소(legacy): 옵션 활성화 시에만 추가 적용
        ratio_enable = bool(_get_value(source, "active_range_off_ratio_enable", False))
        if ratio_enable and effective_off_days > 0 and eligible_off_days > 0:
            scaled_min = int(round(effective_off_days * (eligible_off_days / max(1, ref_days))))
            min_off_required = max(0, min(min_off_required, scaled_min))

    # ★ 보건휴가 대상자 등, 그 사람만 OFF 를 더 확보해야 하는 경우의 per-nurse 가산.
    #   순증이 아니다 — 후처리가 이 한 칸을 휴가코드로 바꾸면 countable_off 에서 빠져
    #   OFF 총량은 그대로고 근무일이 1 줄어든다.
    if not weekend_only and extra_min_off:
        min_off_required = max(0, min(min_off_required + int(extra_min_off), eligible_off_days))

    max_extra_off_days = resolve_max_extra_off_days(source, 0)
    if weekend_only:
        max_off_allowed = eligible_off_days
    else:
        max_off_allowed = min(min_off_required + max_extra_off_days, eligible_off_days)
    return {
        "effective_off_days": effective_off_days,
        "effective_off_days_source": source_key,
        "min_off_required": min_off_required,
        "max_off_allowed": max_off_allowed,
        "max_extra_off_days": max_extra_off_days,
        "eligible_off_days": eligible_off_days,
        "active_nonvac_days": active_nonvac_days,
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
