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
    elif scope in ("schedule", "draft_schedule", "published_schedule"):
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

    # include_cancelled=true → also show retracted/unsubmitted requests
    include_cancelled = params.get("include_cancelled", False)
    submitted_only = not include_cancelled

    # submitted_date_range → submitted_after / submitted_before
    submitted_after = None
    submitted_before = None
    submitted_date_range = params.get("submitted_date_range")
    if submitted_date_range and "~" in str(submitted_date_range):
        parts = str(submitted_date_range).split("~", 1)
        submitted_after = parts[0].strip()
        submitted_before = parts[1].strip()

    rows = wanted_tools.get_wanted_submissions(
        db, group_id, year, month,
        nurse_id=nurse_id,
        submitted_only=submitted_only,
        submitted_after=submitted_after,
        submitted_before=submitted_before,
    )

    # Apply date_range filter on shift_date
    date_range = params.get("date_range")
    if date_range and "~" in str(date_range):
        start, end = str(date_range).split("~", 1)
        rows = [r for r in rows if r.get("shift_date") and start <= r["shift_date"] <= end]
    elif year and month:
        # Default: filter to the requested month even without explicit date_range
        month_prefix = f"{year}-{month:02d}"
        rows = [r for r in rows if r.get("shift_date", "").startswith(month_prefix)]

    # Apply shift code filter if present
    shift_codes = params.get("shift_codes")
    if shift_codes:
        rows = [r for r in rows if r.get("shift") in shift_codes or r.get("shifts_table_id") in shift_codes]

    return rows


def _query_wanted_adjustments(db, group_id, year, month, params):
    nurse_ids = params.get("nurse_ids")
    single_nurse = nurse_ids[0] if nurse_ids and len(nurse_ids) == 1 else None

    date_range_param = params.get("date_range")
    date_range_tuple = None
    if date_range_param and "~" in str(date_range_param):
        start, end = str(date_range_param).split("~", 1)
        date_range_tuple = (start.strip(), end.strip())

    source_types = params.get("source_types")
    is_applied = params.get("is_applied")

    rows = wanted_tools.get_wanted_adjustments(
        db, group_id, year, month,
        nurse_id=single_nurse,
        date_range=date_range_tuple,
        source_types=source_types,
        is_applied=is_applied,
    )

    if nurse_ids and not single_nurse:
        rows = [r for r in rows if r.get("nurse_id") in nurse_ids]

    shift_codes = params.get("shift_codes")
    if shift_codes:
        rows = [r for r in rows if r.get("shift_id") in shift_codes]

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

    # ── Enrich with nurse names, shift names, and grade ──
    entries = _enrich_entries(db, group_id, entries)

    # ── Grade filter (post-enrich, since grade comes from nurse map) ──
    grade = params.get("grade")
    if grade is not None:
        entries = [e for e in entries if e.get("grade") == int(grade)]

    # ── Auto-summarize large unfiltered results ──
    has_filter = any(params.get(k) for k in (
        "nurse_ids", "nurse_name", "date", "date_range",
        "date_range_start", "shift_codes", "shift_name",
    ))
    if not has_filter and len(entries) > 60:
        return _summarize_entries(entries, year, month)

    return entries


def _enrich_entries(db, group_id, entries: list[dict]) -> list[dict]:
    """Add nurse_name and shift_name to each schedule entry.

    Builds lookup maps once, avoids N+1 queries.
    """
    if not entries:
        return entries

    # Build nurse_id → {name, grade} map
    all_nurses = nurse_tools.get_nurses_in_group(db, group_id)
    nurse_map = {n["nurse_id"]: {"name": n["name"], "grade": n.get("grade")} for n in all_nurses}

    # Build shift_id → display info map
    all_shifts = shift_tools.read_shift_definitions(db, group_id)
    shift_map = {
        s["shift_id"]: {"name": s["name"], "shift_gb": s.get("shift_gb", ""), "type": s.get("type", "")}
        for s in all_shifts
    }

    for e in entries:
        nid = e.get("nurse_id", "?")
        nurse_info = nurse_map.get(nid, {})
        e["nurse_name"] = nurse_info.get("name", nid) if isinstance(nurse_info, dict) else nid
        e["grade"] = nurse_info.get("grade") if isinstance(nurse_info, dict) else None
        sid = e.get("shift_id", "")
        shift_info = shift_map.get(sid, {})
        e["shift_name"] = shift_info.get("name", sid)
        e["shift_category"] = shift_info.get("shift_gb") or shift_info.get("name", sid)

    return entries


def _summarize_entries(entries: list[dict], year: int, month: int) -> dict:
    """Summarize a large schedule into a structured overview.

    Returns per-nurse shift counts and daily staffing summary
    instead of raw 800+ entry list.
    """
    from collections import Counter, defaultdict

    nurse_shifts: dict[str, Counter] = defaultdict(Counter)
    daily_staffing: dict[str, Counter] = defaultdict(Counter)
    nurses_set: dict[str, str] = {}  # nurse_id → name

    for e in entries:
        nid = e.get("nurse_id", "?")
        name = e.get("nurse_name", nid)
        cat = e.get("shift_category") or e.get("shift_name") or e.get("shift_id", "?")
        date = e.get("work_date", "?")

        nurses_set[nid] = name
        nurse_shifts[name][cat] += 1
        daily_staffing[date][cat] += 1

    # Per-nurse summary sorted by name
    per_nurse = []
    for name in sorted(nurse_shifts.keys()):
        counts = dict(nurse_shifts[name])
        total_working = sum(v for k, v in counts.items() if k not in ("오프", "OFF", "휴가", "주휴"))
        per_nurse.append({
            "nurse_name": name,
            "shifts": counts,
            "total_working_days": total_working,
        })

    return {
        "summary": True,
        "year": year,
        "month": month,
        "total_nurses": len(nurses_set),
        "total_entries": len(entries),
        "per_nurse": per_nurse,
        "hint": "특정 간호사, 날짜, 시프트로 상세 조회 가능합니다. 예: nurse_name='김민지' 또는 date='2026-04-15'",
    }


def _query_nurses(db, group_id, params):
    nurse_ids = params.get("nurse_ids")
    if nurse_ids and len(nurse_ids) == 1:
        return nurse_tools.get_nurse_by_id(db, nurse_ids[0], group_id)
    if nurse_ids:
        return [nurse_tools.get_nurse_by_id(db, nid, group_id) for nid in nurse_ids]
    return nurse_tools.get_nurses_in_group(db, group_id)
