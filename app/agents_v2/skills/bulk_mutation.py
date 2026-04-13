"""bulk-mutation skill — batch modifications on wanted adjustments, schedule entries, etc."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import schedule_tools, wanted_tools


@register("bulk-mutation")
def bulk_mutation(db: Session, params: dict) -> Any:
    """Perform batch modifications based on scope and mutation params."""
    scope = params.get("scope", "")
    mutation = params.get("mutation", {})
    action = mutation.get("action", "")
    preview_only = params.get("preview_only", False)

    if scope == "wanted_adjustment":
        return _mutate_wanted_adjustments(db, params, mutation, preview_only)
    elif scope == "wanted_submissions" and action == "cancel":
        return _cancel_wanted_request(db, params)
    elif scope in ("draft_schedule", "published_schedule"):
        return _mutate_schedule_entries(db, params, mutation, preview_only)
    else:
        return {"error": f"Unsupported mutation scope: {scope}"}


def _mutate_wanted_adjustments(db, params, mutation, preview_only):
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")

    # First, get the entries to update
    rows = wanted_tools.get_wanted_adjustments(db, group_id, year, month)

    # Apply filters
    nurse_ids = params.get("nurse_ids")
    if nurse_ids:
        rows = [r for r in rows if r.get("nurse_id") in nurse_ids]

    shift_codes = params.get("shift_codes")
    if shift_codes:
        rows = [r for r in rows if r.get("shift_id") in shift_codes]

    # Apply predicate filter
    predicate = params.get("predicate", {})
    if predicate.get("name") == "off" and predicate.get("arguments", {}).get("shift_ids"):
        off_ids = set(predicate["arguments"]["shift_ids"])
        rows = [r for r in rows if r.get("shift_id") in off_ids]

    entry_ids = [r["entry_id"] for r in rows if "entry_id" in r]

    if not entry_ids:
        return {"affected_count": 0, "message": "No matching entries found"}

    field = mutation.get("target_field", "is_applied")
    value = mutation.get("target_value")

    return wanted_tools.bulk_update_wanted_adjustments(
        db, entry_ids, field, value, preview_only=preview_only,
    )


def _cancel_wanted_request(db, params):
    nurse_ids = params.get("nurse_ids", [])
    year = params.get("year")
    month = params.get("month")

    if not nurse_ids:
        return {"error": "nurse_id required for cancel"}

    results = []
    for nid in nurse_ids:
        result = wanted_tools.cancel_wanted_request(db, nid, year, month)
        results.append(result)

    if len(results) == 1:
        return results[0]
    return {"results": results, "affected_count": len(results)}


def _mutate_schedule_entries(db, params, mutation, preview_only):
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    nurse_ids = params.get("nurse_ids")
    date = params.get("date")
    new_shift = mutation.get("target_value")

    # Resolve schedule
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        meta = schedule_tools.resolve_target_schedule(db, group_id, year, month)
        if not meta:
            return {"error": f"No schedule found for {year}/{month}"}
        schedule_id = meta["schedule_id"]

    # Single entry update
    if nurse_ids and len(nurse_ids) == 1 and date and new_shift:
        entry = schedule_tools.find_schedule_entry(db, schedule_id, nurse_ids[0], date)
        if not entry:
            return {"error": f"No entry found for nurse {nurse_ids[0]} on {date}"}
        if preview_only:
            return {
                "preview": True,
                "entry": entry,
                "new_shift_id": new_shift,
            }
        return schedule_tools.update_schedule_entry(db, entry["entry_id"], new_shift)

    # Bulk: get all matching entries and update
    entries = schedule_tools.get_schedule_entries(
        db, schedule_id,
        nurse_ids=nurse_ids or None,
        date_str=date,
    )

    shift_codes = params.get("shift_codes")
    if shift_codes:
        entries = [e for e in entries if e.get("shift_id") in shift_codes]

    if preview_only:
        return {
            "preview": True,
            "affected_count": len(entries),
            "entries": entries[:20],
        }

    if not new_shift:
        return {"error": "target shift value required for bulk update"}

    results = []
    for e in entries:
        result = schedule_tools.update_schedule_entry(db, e["entry_id"], new_shift)
        results.append(result)

    return {"affected_count": len(results), "results": results}
