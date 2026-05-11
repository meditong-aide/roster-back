"""repair-schedule skill — fix, adjust, or rebalance an existing schedule."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import schedule_tools, analysis_tools, shift_tools


@register("repair-schedule")
def repair_schedule(db: Session, params: dict) -> Any:
    """Identify and suggest repairs for schedule issues.

    Finds imbalances or violations and proposes swaps.
    Does NOT auto-apply — returns suggestions for approval.
    """
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")

    # Resolve schedule
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        meta = schedule_tools.resolve_target_schedule(db, group_id, year, month)
        if not meta:
            return {"error": f"No schedule found for {year}/{month}"}
        schedule_id = meta["schedule_id"]

    shifts = shift_tools.read_shift_definitions(db, group_id)
    working_shift_ids = [s["shift_id"] for s in shifts if s.get("is_working")]
    night_shift_ids = [s["shift_id"] for s in shifts if s.get("shift_gb") == "나이트"]

    suggestions = []

    # Analyze night shift balance
    for nid in night_shift_ids:
        stats = analysis_tools.shift_count_variance(db, schedule_id, nid)
        if stats.get("variance", 0) > 1.5:
            nurse_counts = stats.get("nurse_counts", {})
            if nurse_counts:
                max_nurse = max(nurse_counts, key=nurse_counts.get)
                min_nurse = min(nurse_counts, key=nurse_counts.get)
                diff = nurse_counts[max_nurse] - nurse_counts[min_nurse]
                if diff >= 2:
                    suggestions.append({
                        "type": "rebalance_nights",
                        "shift_id": nid,
                        "from_nurse": max_nurse,
                        "from_count": nurse_counts[max_nurse],
                        "to_nurse": min_nurse,
                        "to_count": nurse_counts[min_nurse],
                        "suggested_swaps": diff // 2,
                    })

    # Analyze daily headcount gaps
    headcounts = analysis_tools.daily_headcount(db, schedule_id, group_id)
    # Group by date to find understaffed dates
    by_date: dict[str, dict[str, int]] = {}
    for h in headcounts:
        d = h["work_date"]
        if d not in by_date:
            by_date[d] = {}
        by_date[d][h["shift_id"]] = h["count"]

    # Check for dates with zero working nurses on a shift
    for date, shift_counts in by_date.items():
        for sid in working_shift_ids:
            count = shift_counts.get(sid, 0)
            if count == 0:
                suggestions.append({
                    "type": "understaffed",
                    "work_date": date,
                    "shift_id": sid,
                    "current_count": 0,
                    "suggestion": "Assign at least one nurse to this shift",
                })

    return {
        "schedule_id": schedule_id,
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
    }
