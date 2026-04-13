"""update-constraint skill — modify scheduling constraints (roster config, shift manage)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import constraint_tools


@register("update-constraint")
def update_constraint(db: Session, params: dict) -> Any:
    """Update scheduling constraint configuration."""
    group_id = params["group_id"]
    mutation = params.get("mutation", {})
    preview_only = params.get("preview_only", False)

    target_field = mutation.get("target_field", "")
    target_value = mutation.get("target_value")

    # Check if this is a shift_manage update (manpower)
    if target_field in ("manpower",) or params.get("nurse_class"):
        return _update_shift_manage(db, group_id, params, mutation, preview_only)

    # Otherwise, update roster config
    updates = {}
    if target_field and target_value is not None:
        updates[target_field] = target_value
    else:
        # Check for direct updates dict
        updates = params.get("updates", {})

    if not updates:
        # Read-only: return current config
        return constraint_tools.get_roster_config(db, group_id)

    return constraint_tools.update_roster_config(
        db, group_id, updates, preview_only=preview_only,
    )


def _update_shift_manage(db, group_id, params, mutation, preview_only):
    nurse_class = params.get("nurse_class", "RN")
    shift_slot = params.get("shift_slot", 1)
    new_manpower = mutation.get("target_value")

    if new_manpower is None:
        # Read-only
        return constraint_tools.get_shift_manage(db, group_id)

    return constraint_tools.update_shift_manage_manpower(
        db, group_id, nurse_class, shift_slot, new_manpower,
        preview_only=preview_only,
    )
