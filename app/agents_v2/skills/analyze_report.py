"""analyze-report skill — fairness analysis, distribution reports, comparisons."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import (
    schedule_tools,
    analysis_tools,
    shift_tools,
    nurse_tools,
    wanted_tools,
)


@register("analyze-report")
def analyze_report(db: Session, params: dict) -> Any:
    """Generate analytical reports on schedules or wanted data."""
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    operation = params.get("operation", "summarize")
    scope = params.get("scope", "draft_schedule")

    if scope in ("wanted_submissions", "wanted_adjustment"):
        return _analyze_wanted(db, group_id, year, month, params)

    if operation == "compare":
        return _compare_schedules(db, group_id, year, month, params)

    return _analyze_schedule(db, group_id, year, month, params)


def _analyze_schedule(db, group_id, year, month, params):
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        meta = schedule_tools.resolve_target_schedule(db, group_id, year, month)
        if not meta:
            return {"error": f"No schedule found for {year}/{month}"}
        schedule_id = meta["schedule_id"]

    shifts = shift_tools.read_shift_definitions(db, group_id)
    shift_codes = params.get("shift_codes")
    target_shifts = shift_codes or [s["shift_id"] for s in shifts if s.get("is_working")]

    # Per-nurse shift counts
    counts = analysis_tools.count_shifts_per_nurse(db, schedule_id, shift_ids=target_shifts)

    # Variance per shift
    variance_reports = []
    for sid in target_shifts:
        stats = analysis_tools.shift_count_variance(db, schedule_id, sid)
        variance_reports.append(stats)

    # Daily headcount
    headcount = analysis_tools.daily_headcount(db, schedule_id, group_id)

    return {
        "schedule_id": schedule_id,
        "per_nurse_counts": counts,
        "variance_reports": variance_reports,
        "daily_headcount": headcount,
    }


def _analyze_wanted(db, group_id, year, month, params):
    status = wanted_tools.get_submission_status(db, group_id, year, month)
    submissions = wanted_tools.get_wanted_submissions(db, group_id, year, month)

    # Count requests per shift type
    from collections import Counter
    shift_distribution = Counter(r.get("shift") or r.get("shifts_table_id") for r in submissions)

    return {
        "submission_status": status,
        "total_requests": len(submissions),
        "shift_distribution": dict(shift_distribution),
    }


def _compare_schedules(db, group_id, year, month, params):
    versions = schedule_tools.get_schedule_versions(db, group_id, year, month)
    if len(versions) < 2:
        return {"error": "Need at least 2 schedule versions to compare"}

    v1 = versions[0]
    v2 = versions[1]

    entries_v1 = schedule_tools.get_schedule_entries(db, v1["schedule_id"])
    entries_v2 = schedule_tools.get_schedule_entries(db, v2["schedule_id"])

    # Build lookup for comparison
    lookup_v1 = {(e["nurse_id"], e["work_date"]): e["shift_id"] for e in entries_v1}
    lookup_v2 = {(e["nurse_id"], e["work_date"]): e["shift_id"] for e in entries_v2}

    all_keys = set(lookup_v1.keys()) | set(lookup_v2.keys())
    diffs = []
    for key in sorted(all_keys):
        s1 = lookup_v1.get(key)
        s2 = lookup_v2.get(key)
        if s1 != s2:
            diffs.append({
                "nurse_id": key[0],
                "work_date": key[1],
                f"v{v1['version']}_shift": s1,
                f"v{v2['version']}_shift": s2,
            })

    return {
        "version_1": v1,
        "version_2": v2,
        "diff_count": len(diffs),
        "diffs": diffs[:100],
    }
