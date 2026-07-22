from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from typing import Any

from services.constraint_impact.rule_primitives import (
    DisallowedShiftRule,
    PrimitiveRuleBundle,
    RequiredShiftRule,
    ShiftCountBoundRule,
)
from services.constraint_impact.snapshot import AssignmentWindowFact, SemanticsSnapshot


VALID_SHIFT_CODES = {"D", "E", "N", "O", "M"}
WEEKDAY_NAME_TO_INDEX = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


@dataclass(slots=True)
class RuleCompileContext:
    snapshot: SemanticsSnapshot
    weekday_to_days: dict[int, set[int]]
    all_days: set[int]
    active_days_by_nurse_id: dict[str, set[int]]
    full_shift_codes: set[str] = field(default_factory=lambda: set(VALID_SHIFT_CODES))


def _weekday_to_days(snapshot: SemanticsSnapshot) -> dict[int, set[int]]:
    num_days = max((max(days) for days in snapshot.active_days_by_nurse.values() if days), default=-1) + 1
    if num_days <= 0:
        num_days = calendar.monthrange(snapshot.year, snapshot.month)[1]
    out: dict[int, set[int]] = {i: set() for i in range(7)}
    for d in range(num_days):
        wd = calendar.weekday(snapshot.year, snapshot.month, d + 1)
        out[wd].add(d)
    return out


def build_rule_compile_context(snapshot: SemanticsSnapshot) -> RuleCompileContext:
    weekday_to_days = _weekday_to_days(snapshot)
    all_days = set().union(*weekday_to_days.values()) if weekday_to_days else set()
    active_by_id = {n.nurse_id: set(snapshot.active_days_by_nurse.get(n.nurse_index, set())) for n in snapshot.nurses}
    return RuleCompileContext(snapshot=snapshot, weekday_to_days=weekday_to_days, all_days=all_days, active_days_by_nurse_id=active_by_id)


def _resolve_day_indices(ctx: RuleCompileContext, nurse_id: str, *, weekdays: set[int] | None = None, explicit_day_indices: set[int] | None = None) -> set[int]:
    active_days = set(ctx.active_days_by_nurse_id.get(nurse_id, set()))
    resolved = active_days
    if weekdays is not None:
        weekday_days = set()
        for wd in weekdays:
            weekday_days.update(ctx.weekday_to_days.get(wd, set()))
        resolved &= weekday_days
    if explicit_day_indices is not None:
        resolved &= set(explicit_day_indices)
    return {d for d in resolved if d in active_days}


def _normalize_shift_codes(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        items = [raw]
    else:
        items = list(raw)
    out: set[str] = set()
    for item in items:
        code = str(item).strip().upper()
        if code in VALID_SHIFT_CODES:
            out.add(code)
    return out


def _normalize_weekdays(raw: Any) -> set[int]:
    if raw is None:
        return set()
    if isinstance(raw, (int, str)):
        items = [raw]
    else:
        items = list(raw)
    out: set[int] = set()
    for item in items:
        if isinstance(item, int):
            if 0 <= item <= 6:
                out.add(item)
            continue
        key = str(item).strip().lower()
        if key in WEEKDAY_NAME_TO_INDEX:
            out.add(WEEKDAY_NAME_TO_INDEX[key])
    return out


def _compile_profile_rules(ctx: RuleCompileContext) -> PrimitiveRuleBundle:
    bundle = PrimitiveRuleBundle()
    for nurse in ctx.snapshot.nurses:
        active_days = ctx.active_days_by_nurse_id.get(nurse.nurse_id, set())
        disallowed = (ctx.full_shift_codes - {"O"}) - set(nurse.allowed_shift_codes)
        if disallowed:
            bundle.disallowed.append(
                DisallowedShiftRule(
                    nurse_id=nurse.nurse_id,
                    day_indices=set(active_days),
                    shift_codes=set(disallowed),
                    source="profile",
                    reason="profile_allowed_shift_codes",
                )
            )
    return bundle


def _compile_weekend_off_bundle(ctx: RuleCompileContext) -> PrimitiveRuleBundle:
    bundle = PrimitiveRuleBundle()
    if not bool(ctx.snapshot.config_payload.get("weekend_off_only_enable", True)):
        return bundle
    weekday_days = set().union(*(ctx.weekday_to_days.get(wd, set()) for wd in range(5)))
    weekend_days = set(ctx.weekday_to_days.get(5, set())) | set(ctx.weekday_to_days.get(6, set()))
    for nurse in ctx.snapshot.nurses:
        # TODO(weekend-period): period as-of 로 전환 필요(호출측 db 주입)
        if not nurse.is_weekend_off:
            continue
        active_days = ctx.active_days_by_nurse_id.get(nurse.nurse_id, set())
        weekday_active = active_days & weekday_days
        weekend_active = active_days & weekend_days
        if weekday_active:
            bundle.disallowed.append(
                DisallowedShiftRule(
                    nurse_id=nurse.nurse_id,
                    day_indices=weekday_active,
                    shift_codes={"O"},
                    source="generated",
                    reason="weekend_off_only: weekday O disallowed",
                )
            )
        if weekend_active:
            bundle.required.append(
                RequiredShiftRule(
                    nurse_id=nurse.nurse_id,
                    day_indices=weekend_active,
                    shift_codes={"O"},
                    min_required=1,
                    source="generated",
                    reason="weekend_off_only: weekend O required",
                )
            )
    return bundle


def _compile_assignment_window_rules(ctx: RuleCompileContext) -> PrimitiveRuleBundle:
    bundle = PrimitiveRuleBundle()
    for window in ctx.snapshot.assignment_windows:
        active_days = set(window.active_day_indices)
        inactive_days = set(window.inactive_day_indices)
        if inactive_days:
            bundle.disallowed.append(
                DisallowedShiftRule(
                    nurse_id=window.nurse_id,
                    day_indices=inactive_days,
                    shift_codes=set(ctx.full_shift_codes),
                    source="transfer_overlay",
                    reason=f"assignment_window_inactive:{window.direction}:{window.reason}",
                    metadata={"source_group_id": window.source_group_id, "target_group_id": window.target_group_id},
                )
            )
        if window.allowed_shift_codes:
            disallowed = ctx.full_shift_codes - {"O"} - set(window.allowed_shift_codes)
            if disallowed and active_days:
                bundle.disallowed.append(
                    DisallowedShiftRule(
                        nurse_id=window.nurse_id,
                        day_indices=active_days,
                        shift_codes=set(disallowed),
                        source="transfer_overlay",
                        reason=f"assignment_window_allowed_shift_restriction:{window.direction}:{window.reason}",
                        metadata={"allowed_shift_codes": sorted(window.allowed_shift_codes)},
                    )
                )
    return bundle


def _compile_shift_count_rules(ctx: RuleCompileContext, user_rules: list[dict[str, Any]]) -> PrimitiveRuleBundle:
    bundle = PrimitiveRuleBundle()
    for raw in user_rules or []:
        if str(raw.get("type") or "").strip().lower() != "shift_count_bound":
            continue
        nurse_id = str(raw.get("nurse_id") or "")
        if not nurse_id:
            continue
        shift_code = str(raw.get("shift_code") or "").strip().upper()
        if shift_code not in VALID_SHIFT_CODES:
            continue
        scope_days = _resolve_day_indices(
            ctx,
            nurse_id,
            weekdays=_normalize_weekdays(raw.get("weekdays")) if raw.get("weekdays") is not None else None,
            explicit_day_indices=set(raw.get("day_indices") or []) if raw.get("day_indices") is not None else None,
        )
        bundle.count_bounds.append(
            ShiftCountBoundRule(
                nurse_id=nurse_id,
                shift_code=shift_code,
                min_count=raw.get("min_count"),
                max_count=raw.get("max_count"),
                exact_count=raw.get("exact_count"),
                scope_day_indices=scope_days if scope_days else None,
                source="personal_rule",
                reason=str(raw.get("reason") or "shift_count_bound"),
            )
        )
    return bundle


def _compile_disallowed_rules(ctx: RuleCompileContext, user_rules: list[dict[str, Any]]) -> PrimitiveRuleBundle:
    bundle = PrimitiveRuleBundle()
    for raw in user_rules or []:
        if str(raw.get("type") or "").strip().lower() != "disallowed_shift":
            continue
        nurse_id = str(raw.get("nurse_id") or "")
        if not nurse_id:
            continue
        shift_codes = _normalize_shift_codes(raw.get("shift_codes"))
        weekdays = _normalize_weekdays(raw.get("weekdays")) if raw.get("weekdays") is not None else None
        explicit_days = set(raw.get("day_indices") or []) if raw.get("day_indices") is not None else None
        resolved_days = _resolve_day_indices(ctx, nurse_id, weekdays=weekdays, explicit_day_indices=explicit_days)
        if shift_codes and resolved_days:
            bundle.disallowed.append(
                DisallowedShiftRule(
                    nurse_id=nurse_id,
                    day_indices=resolved_days,
                    shift_codes=shift_codes,
                    source="personal_rule",
                    reason=str(raw.get("reason") or "disallowed_shift"),
                )
            )
    return bundle


def _compile_required_rules(ctx: RuleCompileContext, user_rules: list[dict[str, Any]]) -> PrimitiveRuleBundle:
    bundle = PrimitiveRuleBundle()
    for raw in user_rules or []:
        if str(raw.get("type") or "").strip().lower() != "required_shift":
            continue
        nurse_id = str(raw.get("nurse_id") or "")
        if not nurse_id:
            continue
        shift_codes = _normalize_shift_codes(raw.get("shift_codes"))
        weekdays = _normalize_weekdays(raw.get("weekdays")) if raw.get("weekdays") is not None else None
        explicit_days = set(raw.get("day_indices") or []) if raw.get("day_indices") is not None else None
        resolved_days = _resolve_day_indices(ctx, nurse_id, weekdays=weekdays, explicit_day_indices=explicit_days)
        if shift_codes and resolved_days:
            bundle.required.append(
                RequiredShiftRule(
                    nurse_id=nurse_id,
                    day_indices=resolved_days,
                    shift_codes=shift_codes,
                    min_required=int(raw.get("min_required", 1) or 1),
                    source="personal_rule",
                    reason=str(raw.get("reason") or "required_shift"),
                )
            )
    return bundle


def compile_personal_rules(*, snapshot: SemanticsSnapshot, user_rules: list[dict[str, Any]]) -> PrimitiveRuleBundle:
    ctx = build_rule_compile_context(snapshot)
    bundle = PrimitiveRuleBundle()
    bundle.extend(_compile_disallowed_rules(ctx, user_rules))
    bundle.extend(_compile_required_rules(ctx, user_rules))
    bundle.extend(_compile_shift_count_rules(ctx, user_rules))
    return bundle


def compile_builtin_rules(*, snapshot: SemanticsSnapshot) -> PrimitiveRuleBundle:
    ctx = build_rule_compile_context(snapshot)
    bundle = PrimitiveRuleBundle()
    bundle.extend(_compile_profile_rules(ctx))
    bundle.extend(_compile_weekend_off_bundle(ctx))
    bundle.extend(_compile_assignment_window_rules(ctx))
    return bundle


def merge_rule_bundles(*, bundles: list[PrimitiveRuleBundle]) -> PrimitiveRuleBundle:
    merged = PrimitiveRuleBundle()
    for bundle in bundles:
        merged.extend(bundle)
    return merged
