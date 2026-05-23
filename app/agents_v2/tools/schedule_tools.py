"""Schedule tools — read/modify roster data."""

from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy import desc

from db.models import Schedule, ScheduleEntry, Shift, IssuedRoster


def resolve_target_schedule(
    db: Session, group_id: str, year: int, month: int
) -> dict | None:
    """Find the best schedule for a group/month.

    Priority: published (issued) > latest version.
    Returns schedule metadata dict or None.
    """
    # Check for published (issued) schedule
    issued = (
        db.query(IssuedRoster)
        .filter(
            IssuedRoster.group_id == group_id,
            IssuedRoster.is_active == True,
        )
        .order_by(desc(IssuedRoster.issued_at))
        .first()
    )
    if issued:
        sched = db.query(Schedule).filter(Schedule.schedule_id == issued.schedule_id).first()
        if sched and sched.year == year and sched.month == month:
            return _schedule_meta(sched, is_published=True)

    # Fall back to latest version for this month
    sched = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,
        )
        .order_by(desc(Schedule.version))
        .first()
    )
    if sched:
        return _schedule_meta(sched, is_published=False)
    return None


def get_schedule_entries(
    db: Session,
    schedule_id: str,
    *,
    nurse_ids: list[str] | None = None,
    date_str: str | None = None,
    date_range_start: str | None = None,
    date_range_end: str | None = None,
) -> list[dict]:
    """Read schedule entries with optional filters."""
    q = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id)
    if nurse_ids:
        q = q.filter(ScheduleEntry.nurse_id.in_(nurse_ids))
    if date_str:
        # Parse to date object for cross-DB compat (SQLite stores datetime, MSSQL stores date)
        from datetime import date as _date, datetime as _dt
        try:
            d = _dt.strptime(date_str, "%Y-%m-%d").date() if isinstance(date_str, str) else date_str
            # Match both date and datetime columns
            q = q.filter(
                ScheduleEntry.work_date >= _dt.combine(d, _dt.min.time()),
                ScheduleEntry.work_date < _dt.combine(d, _dt.min.time()) + __import__('datetime').timedelta(days=1),
            )
        except (ValueError, TypeError):
            q = q.filter(ScheduleEntry.work_date == date_str)
    if date_range_start:
        from datetime import datetime as _dt
        try:
            d = _dt.strptime(date_range_start, "%Y-%m-%d") if isinstance(date_range_start, str) else date_range_start
            q = q.filter(ScheduleEntry.work_date >= d)
        except (ValueError, TypeError):
            q = q.filter(ScheduleEntry.work_date >= date_range_start)
    if date_range_end:
        from datetime import datetime as _dt, timedelta
        try:
            d = _dt.strptime(date_range_end, "%Y-%m-%d") if isinstance(date_range_end, str) else date_range_end
            q = q.filter(ScheduleEntry.work_date < d + timedelta(days=1))
        except (ValueError, TypeError):
            q = q.filter(ScheduleEntry.work_date <= date_range_end)
    rows = q.order_by(ScheduleEntry.work_date, ScheduleEntry.nurse_id).all()
    return [
        {
            "entry_id": r.entry_id,
            "schedule_id": r.schedule_id,
            "nurse_id": r.nurse_id,
            "work_date": str(r.work_date.date()) if hasattr(r.work_date, "date") else str(r.work_date),
            "shift_id": r.shift_id,
        }
        for r in rows
    ]


def get_schedule_versions(
    db: Session, group_id: str, year: int, month: int
) -> list[dict]:
    """List all schedule versions for a group/month."""
    rows = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,
        )
        .order_by(desc(Schedule.version))
        .all()
    )
    return [_schedule_meta(r) for r in rows]


def update_schedule_entry(
    db: Session,
    entry_id: str,
    new_shift_id: str,
    group_id: str,
) -> dict:
    """Update a single schedule entry's shift code.

    group_id-scoped: ScheduleEntry → Schedule join 으로 group_id 격리 검증
    (cross-group mutation 차단, defense-in-depth).

    Returns the updated entry and its previous value.
    """
    entry = (
        db.query(ScheduleEntry)
        .join(Schedule, Schedule.schedule_id == ScheduleEntry.schedule_id)
        .filter(
            ScheduleEntry.entry_id == entry_id,
            Schedule.group_id == group_id,
        )
        .first()
    )
    if not entry:
        return {"error": f"Entry {entry_id} not found in group {group_id}"}
    old_shift = entry.shift_id
    entry.shift_id = new_shift_id
    db.commit()
    db.refresh(entry)
    return {
        "entry_id": entry.entry_id,
        "nurse_id": entry.nurse_id,
        "work_date": str(entry.work_date.date()) if hasattr(entry.work_date, "date") else str(entry.work_date),
        "old_shift_id": old_shift,
        "new_shift_id": entry.shift_id,
    }


def find_schedule_entry(
    db: Session,
    schedule_id: str,
    nurse_id: str,
    date_str: str,
    group_id: str,
) -> dict | None:
    """Find a specific entry by schedule + nurse + date.

    group_id-scoped: Schedule join 으로 RBAC 격리.
    """
    q = (
        db.query(ScheduleEntry)
        .join(Schedule, Schedule.schedule_id == ScheduleEntry.schedule_id)
        .filter(
            ScheduleEntry.schedule_id == schedule_id,
            ScheduleEntry.nurse_id == nurse_id,
            Schedule.group_id == group_id,
        )
    )
    # Cross-DB date comparison: SQLite stores datetime, MSSQL stores date strings
    from datetime import datetime as _dt, timedelta
    if isinstance(date_str, str):
        try:
            d = _dt.strptime(date_str, "%Y-%m-%d").date()
            q = q.filter(
                ScheduleEntry.work_date >= _dt.combine(d, _dt.min.time()),
                ScheduleEntry.work_date < _dt.combine(d, _dt.min.time()) + timedelta(days=1),
            )
        except (ValueError, TypeError):
            q = q.filter(ScheduleEntry.work_date == date_str)
    else:
        q = q.filter(ScheduleEntry.work_date == date_str)
    entry = q.first()
    if not entry:
        return None
    return {
        "entry_id": entry.entry_id,
        "schedule_id": entry.schedule_id,
        "nurse_id": entry.nurse_id,
        "work_date": str(entry.work_date.date()) if hasattr(entry.work_date, "date") else str(entry.work_date),
        "shift_id": entry.shift_id,
    }


# ── private ──────────────────────────────────────────────


def _schedule_meta(sched: Schedule, is_published: bool = False) -> dict:
    return {
        "schedule_id": sched.schedule_id,
        "group_id": sched.group_id,
        "year": sched.year,
        "month": sched.month,
        "version": sched.version,
        "name": sched.name,
        "status": sched.status,
        "is_published": is_published,
        "config_id": sched.config_id,
        "created_at": str(sched.created_at) if sched.created_at else None,
    }
