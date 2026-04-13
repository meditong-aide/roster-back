"""query-schedule skill — read-only data retrieval across all scopes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import (
    schedule_tools,
    wanted_tools,
    nurse_tools,
    shift_tools,
    constraint_tools,
    generation_tools,
)


@register("query-schedule")
def query_schedule(db: Session, params: dict) -> Any:
    """Retrieve data based on scope and filters."""
    scope = params.get("scope", "")
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")

    if scope == "wanted_submissions":
        return _query_wanted_submissions(db, group_id, year, month, params)
    elif scope == "wanted_adjustment":
        return _query_wanted_adjustments(db, group_id, year, month, params)
    elif scope in ("draft_schedule", "published_schedule"):
        return _query_schedule_entries(db, group_id, year, month, params)
    elif scope == "shift_definitions":
        return shift_tools.read_shift_definitions(db, group_id)
    elif scope == "nurse_info":
        return _query_nurses(db, group_id, params)
    elif scope == "constraint_config":
        return constraint_tools.get_roster_config(db, group_id)
    elif scope == "generation_job":
        return generation_tools.get_latest_job(db, group_id)
    else:
        # Fallback: try schedule
        return _query_schedule_entries(db, group_id, year, month, params)


def _query_wanted_submissions(db, group_id, year, month, params):
    nurse_ids = params.get("nurse_ids")
    nurse_id = nurse_ids[0] if nurse_ids and len(nurse_ids) == 1 else None

    operation = params.get("operation", "list")
    if operation == "count":
        status = wanted_tools.get_submission_status(db, group_id, year, month)
        return status

    rows = wanted_tools.get_wanted_submissions(
        db, group_id, year, month, nurse_id=nurse_id,
    )

    # Apply shift code filter if present
    shift_codes = params.get("shift_codes")
    if shift_codes:
        rows = [r for r in rows if r.get("shift") in shift_codes or r.get("shifts_table_id") in shift_codes]

    return rows


def _query_wanted_adjustments(db, group_id, year, month, params):
    rows = wanted_tools.get_wanted_adjustments(db, group_id, year, month)

    nurse_ids = params.get("nurse_ids")
    if nurse_ids:
        rows = [r for r in rows if r.get("nurse_id") in nurse_ids]

    shift_codes = params.get("shift_codes")
    if shift_codes:
        rows = [r for r in rows if r.get("shift_id") in shift_codes]

    # Filter by predicate
    predicate = params.get("predicate", {})
    if predicate.get("name") == "off" and predicate.get("arguments", {}).get("shift_ids"):
        off_ids = set(predicate["arguments"]["shift_ids"])
        rows = [r for r in rows if r.get("shift_id") in off_ids]

    return rows


def _query_schedule_entries(db, group_id, year, month, params):
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        meta = schedule_tools.resolve_target_schedule(db, group_id, year, month)
        if not meta:
            return {"error": f"No schedule found for {year}/{month}"}
        schedule_id = meta["schedule_id"]

    entries = schedule_tools.get_schedule_entries(
        db,
        schedule_id,
        nurse_ids=params.get("nurse_ids") or None,
        date_str=params.get("date"),
        date_range_start=params.get("date_range_start"),
        date_range_end=params.get("date_range_end"),
    )

    shift_codes = params.get("shift_codes")
    if shift_codes:
        entries = [e for e in entries if e.get("shift_id") in shift_codes]

    return entries


def _query_nurses(db, group_id, params):
    nurse_ids = params.get("nurse_ids")
    if nurse_ids and len(nurse_ids) == 1:
        return nurse_tools.get_nurse_by_id(db, nurse_ids[0])
    if nurse_ids:
        return [nurse_tools.get_nurse_by_id(db, nid) for nid in nurse_ids]
    return nurse_tools.get_nurses_in_group(db, group_id)
