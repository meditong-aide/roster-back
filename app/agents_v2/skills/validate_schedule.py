"""validate-schedule skill — check a schedule for constraint violations."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import (
    schedule_tools,
    analysis_tools,
    constraint_tools,
    shift_tools,
)


@register("validate-schedule")
def validate_schedule(db: Session, params: dict) -> Any:
    """Validate a schedule against configured constraints."""
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

    # Load config
    config = constraint_tools.get_roster_config(db, group_id)
    shifts = shift_tools.read_shift_definitions(db, group_id)

    violations = []

    # Find night shift codes
    night_shift_ids = [s["shift_id"] for s in shifts if s.get("shift_gb") == "나이트"]

    # Check: consecutive night shifts
    if night_shift_ids and config:
        max_allowed = 3 if config.get("three_seq_nig") else 2
        for nid in night_shift_ids:
            streaks = analysis_tools.detect_consecutive_pattern(
                db, schedule_id, nid, min_streak=max_allowed + 1,
            )
            for s in streaks:
                violations.append({
                    "type": "consecutive_night_exceeded",
                    "nurse_id": s["nurse_id"],
                    "shift_id": nid,
                    "streak": s["streak_length"],
                    "max_allowed": max_allowed,
                    "dates": f"{s['start_date']}~{s['end_date']}",
                })

    # Check: max consecutive work days
    if config and config.get("max_conseq_work"):
        working_shift_ids = [s["shift_id"] for s in shifts if s.get("is_working")]
        for wid in working_shift_ids:
            streaks = analysis_tools.detect_consecutive_pattern(
                db, schedule_id, wid, min_streak=config["max_conseq_work"] + 1,
            )
            for s in streaks:
                violations.append({
                    "type": "max_consecutive_work_exceeded",
                    "nurse_id": s["nurse_id"],
                    "shift_id": wid,
                    "streak": s["streak_length"],
                    "max_allowed": config["max_conseq_work"],
                    "dates": f"{s['start_date']}~{s['end_date']}",
                })

    # Check: grade coverage per shift
    if config and config.get("min_exp_per_shift"):
        for sid in [s["shift_id"] for s in shifts if s.get("is_working")]:
            missing = analysis_tools.dates_missing_grade(
                db, schedule_id, group_id, sid, config["min_exp_per_shift"],
            )
            for m in missing:
                violations.append({
                    "type": "grade_coverage_missing",
                    "work_date": m["work_date"],
                    "shift_id": sid,
                    "required_grade": m["required_grade"],
                    "max_grade_present": m["max_grade_present"],
                })

    # Enrich violations with nurse names
    if violations:
        from agents_v2.tools import nurse_tools
        all_nurses = nurse_tools.get_nurses_in_group(db, group_id)
        nurse_map = {n["nurse_id"]: n["name"] for n in all_nurses}
        for v in violations:
            if v.get("nurse_id"):
                v["nurse_name"] = nurse_map.get(v["nurse_id"], v["nurse_id"])

    return {
        "schedule_id": schedule_id,
        "violation_count": len(violations),
        "violations": violations,
        "status": "pass" if not violations else "fail",
    }
