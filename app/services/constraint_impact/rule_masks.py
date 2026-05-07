from __future__ import annotations

from dataclasses import dataclass

from services.constraint_impact.rule_primitives import PrimitiveRuleBundle
from services.constraint_impact.snapshot import SemanticsSnapshot


@dataclass(slots=True)
class CompiledRuleMasks:
    disallowed_shift_indices: dict[tuple[int, int], set[int]]
    required_shift_indices: dict[tuple[int, int], tuple[set[int], int]]
    shift_count_bounds: dict[tuple[int, str], dict[str, object]]


def build_rule_masks(*, snapshot: SemanticsSnapshot, bundle: PrimitiveRuleBundle) -> CompiledRuleMasks:
    shift_code_to_index = {code: idx for idx, code in enumerate(snapshot.shift_types)}
    nurse_id_to_index = {n.nurse_id: n.nurse_index for n in snapshot.nurses}
    disallowed_shift_indices: dict[tuple[int, int], set[int]] = {}
    required_shift_indices: dict[tuple[int, int], tuple[set[int], int]] = {}
    shift_count_bounds: dict[tuple[int, str], dict[str, object]] = {}

    for rule in bundle.disallowed:
        n_idx = nurse_id_to_index.get(rule.nurse_id)
        if n_idx is None:
            continue
        shift_indices = {shift_code_to_index[c] for c in rule.shift_codes if c in shift_code_to_index}
        if not shift_indices:
            continue
        for d in rule.day_indices:
            disallowed_shift_indices.setdefault((n_idx, d), set()).update(shift_indices)

    for rule in bundle.required:
        n_idx = nurse_id_to_index.get(rule.nurse_id)
        if n_idx is None:
            continue
        shift_indices = {shift_code_to_index[c] for c in rule.shift_codes if c in shift_code_to_index}
        if not shift_indices:
            continue
        for d in rule.day_indices:
            prev = required_shift_indices.get((n_idx, d))
            if prev is None:
                required_shift_indices[(n_idx, d)] = (set(shift_indices), int(rule.min_required))
            else:
                merged_set = set(prev[0]) | set(shift_indices)
                merged_req = max(int(prev[1]), int(rule.min_required))
                required_shift_indices[(n_idx, d)] = (merged_set, merged_req)

    for rule in bundle.count_bounds:
        n_idx = nurse_id_to_index.get(rule.nurse_id)
        if n_idx is None:
            continue
        key = (n_idx, rule.shift_code)
        prev = shift_count_bounds.get(key, {"min_count": None, "max_count": None, "exact_count": None, "scope_days": None})
        scope_days = set(rule.scope_day_indices) if rule.scope_day_indices is not None else set(snapshot.active_days_by_nurse.get(n_idx, set()))
        if rule.exact_count is not None:
            prev["exact_count"] = int(rule.exact_count)
        if rule.min_count is not None:
            prev["min_count"] = int(rule.min_count) if prev.get("min_count") is None else max(int(prev["min_count"]), int(rule.min_count))
        if rule.max_count is not None:
            prev["max_count"] = int(rule.max_count) if prev.get("max_count") is None else min(int(prev["max_count"]), int(rule.max_count))
        prev["scope_days"] = scope_days if prev.get("scope_days") is None else set(prev["scope_days"]) & scope_days
        shift_count_bounds[key] = prev

    return CompiledRuleMasks(
        disallowed_shift_indices=disallowed_shift_indices,
        required_shift_indices=required_shift_indices,
        shift_count_bounds=shift_count_bounds,
    )
