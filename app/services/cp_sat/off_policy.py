from __future__ import annotations

from collections.abc import Mapping
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
