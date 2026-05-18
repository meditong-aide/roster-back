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
    action = params.get("action") or mutation.get("action", "")
    preview_only = params.get("preview_only", False)

    if scope == "wanted_adjustment":
        return _mutate_wanted_adjustments(db, params, mutation, preview_only)
    elif scope == "wanted_submissions" and action == "cancel":
        date = params.get("date")
        if date:
            return _delete_wanted_by_date(db, params, preview_only)
        return _cancel_wanted_request(db, params, preview_only)
    elif scope == "wanted_submissions" and action == "update_deadline":
        return _update_wanted_deadline(db, params, preview_only)
    elif scope == "wanted_submissions" and action == "add_shift":
        return _add_wanted_by_date(db, params, preview_only)
    elif scope == "wanted_submissions" and action == "change_shift":
        return _change_wanted_by_date(db, params, preview_only)
    elif scope in ("schedule", "draft_schedule", "published_schedule"):
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
        db, entry_ids, field, value, group_id=group_id, preview_only=preview_only,
    )


def _cancel_wanted_request(db, params, preview_only=False):
    nurse_ids = params.get("nurse_ids", [])
    year = params.get("year")
    month = params.get("month")
    group_id = params.get("group_id")

    if not nurse_ids:
        return {"error": "nurse_id required for cancel"}
    if not group_id:
        return {"error": "group_id required for cancel"}

    if preview_only:
        return {
            "preview_only": True,
            "action": "cancel_wanted",
            "nurse_ids": nurse_ids,
            "year": year,
            "month": month,
            "affected_count": len(nurse_ids),
        }

    results = []
    for nid in nurse_ids:
        result = wanted_tools.cancel_wanted_request(db, nid, group_id, year, month)
        results.append(result)

    if len(results) == 1:
        return results[0]
    return {"results": results, "affected_count": len(results)}


def _update_wanted_deadline(db, params, preview_only=False):
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    new_deadline = params.get("new_deadline")

    if not new_deadline:
        return {"error": "new_deadline (YYYY-MM-DD) required"}

    # Preview: show current + new deadline
    if preview_only:
        current = wanted_tools.get_wanted_status(db, group_id, year, month)
        if not current:
            return {"error": f"No wanted campaign found for {year}/{month}"}
        return {
            "preview_only": True,
            "action": "update_deadline",
            "current_deadline": current.get("exp_date"),
            "new_deadline": new_deadline,
            "year": year,
            "month": month,
        }

    return wanted_tools.update_wanted_deadline(db, group_id, year, month, new_deadline)


def _delete_wanted_by_date(db, params, preview_only=False):
    nurse_ids = params.get("nurse_ids", [])
    if not nurse_ids:
        return {"error": "nurse_id required"}
    return wanted_tools.delete_wanted_by_date(
        db,
        nurse_id=nurse_ids[0],
        group_id=params["group_id"],
        year=params.get("year"),
        month=params.get("month"),
        date=params["date"],
        preview_only=preview_only,
    )


def _add_wanted_by_date(db, params, preview_only=False):
    nurse_ids = params.get("nurse_ids", [])
    if not nurse_ids:
        return {"error": "nurse_id required"}
    shift_code = params.get("shift_codes", [None])[0] if params.get("shift_codes") else params.get("shift_name", "")
    if not shift_code:
        return {"error": "shift required for add_shift"}
    return wanted_tools.add_wanted_by_date(
        db,
        nurse_id=nurse_ids[0],
        group_id=params["group_id"],
        year=params.get("year"),
        month=params.get("month"),
        date=params.get("date", ""),
        shift_code=shift_code,
        comment=params.get("comment", ""),
        preview_only=preview_only,
    )


def _change_wanted_by_date(db, params, preview_only=False):
    nurse_ids = params.get("nurse_ids", [])
    if not nurse_ids:
        return {"error": "nurse_id required"}
    new_shift = params.get("new_shift_code") or (
        params.get("shift_codes", [None])[0] if params.get("shift_codes") else None
    )
    if not new_shift:
        return {"error": "new_shift required for change_shift"}
    return wanted_tools.modify_wanted_by_date(
        db,
        nurse_id=nurse_ids[0],
        group_id=params["group_id"],
        year=params.get("year"),
        month=params.get("month"),
        date=params.get("date", ""),
        new_shift_code=new_shift,
        comment=params.get("comment"),
        preview_only=preview_only,
    )


def _mutate_schedule_entries(db, params, mutation, preview_only):
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    nurse_ids = params.get("nurse_ids")
    date = params.get("date")
    # LLM sends flat params (new_shift_code via grounding), tests use nested mutation
    new_shift = (
        params.get("new_shift_code")
        or mutation.get("target_value")
    )

    # Resolve schedule
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        meta = schedule_tools.resolve_target_schedule(db, group_id, year, month)
        if not meta:
            return {"error": f"No schedule found for {year}/{month}"}
        schedule_id = meta["schedule_id"]

    # Single entry update
    if nurse_ids and len(nurse_ids) == 1 and date and new_shift:
        entry = schedule_tools.find_schedule_entry(db, schedule_id, nurse_ids[0], date, group_id)
        if not entry:
            return {"error": f"No entry found for nurse {nurse_ids[0]} on {date}"}
        if preview_only:
            return {
                "preview": True,
                "entry": entry,
                "new_shift_id": new_shift,
            }
        return schedule_tools.update_schedule_entry(db, entry["entry_id"], new_shift, group_id)

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
        result = schedule_tools.update_schedule_entry(db, e["entry_id"], new_shift, group_id)
        results.append(result)

    return {"affected_count": len(results), "results": results}
