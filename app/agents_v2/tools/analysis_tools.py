"""Analysis tools — schedule statistics and pattern detection."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import groupby
from operator import itemgetter

from sqlalchemy.orm import Session

from db.models import ScheduleEntry, Shift, Nurse


def count_shifts_per_nurse(
    db: Session,
    schedule_id: str,
    *,
    shift_ids: list[str] | None = None,
) -> list[dict]:
    """Count how many times each nurse works each shift type in a schedule.

    If shift_ids is given, only count those specific shifts.
    Returns list of {nurse_id, shift_id, count} sorted by nurse then shift.
    """
    q = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id)
    if shift_ids:
        q = q.filter(ScheduleEntry.shift_id.in_(shift_ids))
    rows = q.all()

    counter: dict[tuple[str, str], int] = Counter()
    for r in rows:
        counter[(r.nurse_id, r.shift_id)] += 1

    result = [
        {"nurse_id": nid, "shift_id": sid, "count": cnt}
        for (nid, sid), cnt in sorted(counter.items())
    ]
    return result


def shift_count_variance(
    db: Session,
    schedule_id: str,
    shift_id: str,
) -> dict:
    """Calculate variance in how evenly a shift is distributed across nurses.

    Returns {shift_id, nurse_counts: {nurse_id: count}, min, max, mean, variance}.
    """
    rows = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == schedule_id,
            ScheduleEntry.shift_id == shift_id,
        )
        .all()
    )
    nurse_counts: dict[str, int] = Counter(r.nurse_id for r in rows)
    counts = list(nurse_counts.values())
    if not counts:
        return {"shift_id": shift_id, "nurse_counts": {}, "min": 0, "max": 0, "mean": 0, "variance": 0}

    mean = sum(counts) / len(counts)
    variance = sum((c - mean) ** 2 for c in counts) / len(counts)
    return {
        "shift_id": shift_id,
        "nurse_counts": nurse_counts,
        "min": min(counts),
        "max": max(counts),
        "mean": round(mean, 2),
        "variance": round(variance, 2),
    }


def detect_consecutive_pattern(
    db: Session,
    schedule_id: str,
    shift_id: str,
    *,
    min_streak: int = 3,
    nurse_ids: list[str] | None = None,
) -> list[dict]:
    """Detect nurses with consecutive runs of the same shift >= min_streak.

    Returns list of {nurse_id, shift_id, streak_length, start_date, end_date}.
    """
    q = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == schedule_id,
            ScheduleEntry.shift_id == shift_id,
        )
    )
    if nurse_ids:
        q = q.filter(ScheduleEntry.nurse_id.in_(nurse_ids))
    rows = q.order_by(ScheduleEntry.nurse_id, ScheduleEntry.work_date).all()

    results = []
    for nurse_id, group in groupby(rows, key=lambda r: r.nurse_id):
        entries = list(group)
        dates = [
            r.work_date.date() if hasattr(r.work_date, "date") else r.work_date
            for r in entries
        ]
        # Find consecutive date runs
        streak_start = 0
        for i in range(1, len(dates)):
            delta = (dates[i] - dates[i - 1]).days if hasattr(dates[i] - dates[i - 1], "days") else 1
            if delta != 1:
                _check_streak(results, nurse_id, shift_id, dates, streak_start, i, min_streak)
                streak_start = i
        _check_streak(results, nurse_id, shift_id, dates, streak_start, len(dates), min_streak)

    return results


def dates_missing_grade(
    db: Session,
    schedule_id: str,
    group_id: str,
    shift_id: str,
    min_grade: int,
) -> list[dict]:
    """Find dates where no nurse of at least min_grade is assigned to a shift.

    Returns list of {work_date, shift_id, assigned_nurse_ids, max_grade_present}.
    """
    entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == schedule_id,
            ScheduleEntry.shift_id == shift_id,
        )
        .order_by(ScheduleEntry.work_date)
        .all()
    )
    # Build nurse grade lookup
    nurse_ids = list({e.nurse_id for e in entries})
    if not nurse_ids:
        return []
    nurses = (
        db.query(Nurse.nurse_id, Nurse.grade)
        .filter(Nurse.nurse_id.in_(nurse_ids))
        .all()
    )
    grade_map = {n.nurse_id: n.grade for n in nurses}

    # Group by date
    by_date: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        d = str(e.work_date.date()) if hasattr(e.work_date, "date") else str(e.work_date)
        by_date[d].append(e.nurse_id)

    results = []
    for work_date, assigned_ids in sorted(by_date.items()):
        grades = [grade_map.get(nid, 0) or 0 for nid in assigned_ids]
        max_grade = max(grades) if grades else 0
        if max_grade < min_grade:
            results.append({
                "work_date": work_date,
                "shift_id": shift_id,
                "assigned_nurse_ids": assigned_ids,
                "max_grade_present": max_grade,
                "required_grade": min_grade,
            })

    return results


def daily_headcount(
    db: Session,
    schedule_id: str,
    group_id: str,
    *,
    date_range_start: str | None = None,
    date_range_end: str | None = None,
) -> list[dict]:
    """Get per-date, per-shift headcount for a schedule.

    Returns list of {work_date, shift_id, count}.
    """
    q = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id)
    if date_range_start:
        q = q.filter(ScheduleEntry.work_date >= date_range_start)
    if date_range_end:
        q = q.filter(ScheduleEntry.work_date <= date_range_end)
    rows = q.order_by(ScheduleEntry.work_date).all()

    counter: dict[tuple[str, str], int] = Counter()
    for r in rows:
        d = str(r.work_date.date()) if hasattr(r.work_date, "date") else str(r.work_date)
        counter[(d, r.shift_id)] += 1

    return [
        {"work_date": d, "shift_id": sid, "count": cnt}
        for (d, sid), cnt in sorted(counter.items())
    ]


# ── private ──────────────────────────────────────────────


def _check_streak(results, nurse_id, shift_id, dates, start, end, min_streak):
    length = end - start
    if length >= min_streak:
        results.append({
            "nurse_id": nurse_id,
            "shift_id": shift_id,
            "streak_length": length,
            "start_date": str(dates[start]),
            "end_date": str(dates[end - 1]),
        })
