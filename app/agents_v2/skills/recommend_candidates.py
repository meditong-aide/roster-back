"""recommend-candidates skill — suggest replacement/substitute nurses."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import schedule_tools, nurse_tools, shift_tools


@register("recommend-candidates")
def recommend_candidates(db: Session, params: dict) -> Any:
    """Recommend candidate nurses for a shift on a specific date.

    Finds nurses who are off (or have a lighter load) on the target date
    and are qualified for the requested shift.
    """
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    date = params.get("date")
    shift_codes = params.get("shift_codes", [])
    exclude_ids = params.get("nurse_ids", [])  # nurses to exclude (already assigned)

    if not date:
        return {"error": "date required for candidate recommendation"}

    # Resolve schedule
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        meta = schedule_tools.resolve_target_schedule(db, group_id, year, month)
        if not meta:
            return {"error": f"No schedule found for {year}/{month}"}
        schedule_id = meta["schedule_id"]

    # Get all entries for the target date
    entries = schedule_tools.get_schedule_entries(db, schedule_id, date_str=date)

    # Get shift definitions to identify off/non-working shifts
    shifts = shift_tools.read_shift_definitions(db, group_id)
    off_shift_ids = {s["shift_id"] for s in shifts if not s.get("is_working")}

    # Find nurses who are off on that date
    off_nurses = [
        e["nurse_id"] for e in entries
        if e["shift_id"] in off_shift_ids
    ]

    # Exclude specified nurses
    exclude_set = set(exclude_ids)
    candidates = [nid for nid in off_nurses if nid not in exclude_set]

    # Enrich with nurse details
    enriched = []
    for nid in candidates:
        nurse = nurse_tools.get_nurse_by_id(db, nid, group_id)
        if not nurse:
            continue

        # Check if nurse can work the target shift
        work_shifts = nurse.get("work_shifts") or []
        if shift_codes and work_shifts:
            if not any(sc in work_shifts for sc in shift_codes):
                continue

        enriched.append({
            "nurse_id": nid,
            "name": nurse.get("name"),
            "grade": nurse.get("grade"),
            "experience": nurse.get("experience"),
            "team_id": nurse.get("team_id"),
            "current_shift": next(
                (e["shift_id"] for e in entries if e["nurse_id"] == nid), None
            ),
        })

    # Sort by grade descending (higher grade = more experienced)
    enriched.sort(key=lambda x: x.get("grade") or 0, reverse=True)

    return {
        "date": date,
        "target_shifts": shift_codes,
        "candidate_count": len(enriched),
        "candidates": enriched,
    }
