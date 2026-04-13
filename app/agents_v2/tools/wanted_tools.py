"""Wanted (shift preference request) tools."""

from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy import desc

from db.models import (
    Wanted,
    WantedRequest,
    NurseShiftRequest,
    FixedWantedEntry,
    ShiftPreference,
)


def _wanted_request_month_str(year: int, month: int) -> str:
    """WantedRequest.month is CHAR(7) format 'YYYY-MM'."""
    return f"{year}-{month:02d}"


def get_wanted_status(
    db: Session, group_id: str, year: int, month: int
) -> dict | None:
    """Get the wanted campaign status for a group/month."""
    row = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == group_id,
            Wanted.year == year,
            Wanted.month == month,
        )
        .first()
    )
    if not row:
        return None
    return {
        "group_id": row.group_id,
        "year": row.year,
        "month": row.month,
        "exp_date": str(row.exp_date) if row.exp_date else None,
        "status": row.status,
    }


def get_wanted_submissions(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    *,
    nurse_id: str | None = None,
    submitted_only: bool = True,
) -> list[dict]:
    """Get shift request detail rows for a group/month."""
    month_str = _wanted_request_month_str(year, month)
    q = (
        db.query(NurseShiftRequest)
        .join(WantedRequest, NurseShiftRequest.request_id == WantedRequest.request_id)
        .filter(WantedRequest.month == month_str)
    )
    if nurse_id:
        q = q.filter(NurseShiftRequest.nurse_id == nurse_id)
    if submitted_only:
        q = q.filter(WantedRequest.is_submitted == True)
    rows = q.all()
    return [
        {
            "nurse_id": r.nurse_id,
            "request_id": r.request_id,
            "shift_date": str(r.shift_date) if r.shift_date else None,
            "shift": r.shift,
            "shifts_table_id": r.shifts_table_id,
            "score": r.score,
            "comment": r.comment,
        }
        for r in rows
    ]


def get_wanted_adjustments(
    db: Session,
    group_id: str,
    year: int,
    month: int,
) -> list[dict]:
    """Get fixed wanted (adjustment) entries for a group/month.

    These are the post-submission adjustments with is_applied flags.
    """
    rows = (
        db.query(FixedWantedEntry)
        .filter(
            FixedWantedEntry.group_id == group_id,
            FixedWantedEntry.year == year,
            FixedWantedEntry.month == month,
        )
        .all()
    )
    return [
        {
            "entry_id": r.id,
            "group_id": r.group_id,
            "nurse_id": r.nurse_id,
            "year": r.year,
            "month": r.month,
            "work_date": str(r.shift_date) if r.shift_date else None,
            "shift_id": r.shift_id,
            "is_applied": bool(r.is_applied) if hasattr(r, "is_applied") else True,
        }
        for r in rows
    ]


def get_submission_status(
    db: Session, group_id: str, year: int, month: int
) -> dict:
    """Get submission status summary — who submitted, who didn't."""
    from db.models import Nurse

    month_str = _wanted_request_month_str(year, month)
    all_nurses = (
        db.query(Nurse.nurse_id, Nurse.name)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )
    submitted_ids = set(
        r[0]
        for r in db.query(WantedRequest.nurse_id)
        .filter(WantedRequest.month == month_str, WantedRequest.is_submitted == True)
        .all()
    )
    submitted = [{"nurse_id": n.nurse_id, "name": n.name} for n in all_nurses if n.nurse_id in submitted_ids]
    not_submitted = [{"nurse_id": n.nurse_id, "name": n.name} for n in all_nurses if n.nurse_id not in submitted_ids]
    return {
        "total": len(all_nurses),
        "submitted_count": len(submitted),
        "not_submitted_count": len(not_submitted),
        "submitted": submitted,
        "not_submitted": not_submitted,
    }


def bulk_update_wanted_adjustments(
    db: Session,
    entry_ids: list[str],
    field: str,
    value,
    *,
    preview_only: bool = False,
) -> dict:
    """Bulk update fixed wanted entries. Returns affected count."""
    rows = (
        db.query(FixedWantedEntry)
        .filter(FixedWantedEntry.id.in_(entry_ids))
        .all()
    )
    if preview_only:
        return {
            "preview": True,
            "affected_count": len(rows),
            "entry_ids": [r.id for r in rows],
        }
    for row in rows:
        setattr(row, field, value)
    db.commit()
    return {
        "preview": False,
        "affected_count": len(rows),
        "entry_ids": [r.id for r in rows],
    }


def update_wanted_deadline(
    db: Session, group_id: str, year: int, month: int, new_deadline: str
) -> dict:
    """Update wanted campaign deadline."""
    row = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == group_id,
            Wanted.year == year,
            Wanted.month == month,
        )
        .first()
    )
    if not row:
        return {"error": "Wanted campaign not found"}
    old_deadline = str(row.exp_date) if row.exp_date else None
    row.exp_date = new_deadline
    db.commit()
    return {
        "group_id": group_id,
        "year": year,
        "month": month,
        "old_deadline": old_deadline,
        "new_deadline": new_deadline,
    }


def cancel_wanted_request(
    db: Session, nurse_id: str, year: int, month: int, request_id: int | None = None
) -> dict:
    """Cancel (retract) a wanted request submission."""
    month_str = _wanted_request_month_str(year, month)
    q = db.query(WantedRequest).filter(
        WantedRequest.nurse_id == nurse_id,
        WantedRequest.month == month_str,
        WantedRequest.is_submitted == True,
    )
    if request_id:
        q = q.filter(WantedRequest.request_id == request_id)
    row = q.order_by(desc(WantedRequest.submitted_at)).first()
    if not row:
        return {"error": "No submitted request found"}
    row.is_submitted = False
    row.submitted_at = None
    db.commit()
    return {
        "nurse_id": nurse_id,
        "request_id": row.request_id,
        "month_str": month_str,
        "status": "retracted",
    }
