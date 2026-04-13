"""Nurse data tools — search, filter, read nurse info."""

from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func

from db.models import Nurse, Team


def search_nurses_by_name(db: Session, group_id: str, name_query: str) -> list[dict]:
    """Search nurses in a group by name (partial match).

    Returns list of candidates with nurse_id, name, team, grade, experience.
    """
    rows = (
        db.query(Nurse)
        .filter(
            Nurse.group_id == group_id,
            Nurse.active == 1,
            Nurse.name.contains(name_query),
        )
        .order_by(Nurse.sequence)
        .all()
    )
    return [_nurse_summary(r) for r in rows]


def get_nurses_in_group(db: Session, group_id: str) -> list[dict]:
    """Return all active nurses in a group."""
    rows = (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .order_by(Nurse.sequence)
        .all()
    )
    return [_nurse_summary(r) for r in rows]


def get_nurse_by_id(db: Session, nurse_id: str) -> dict | None:
    """Get a single nurse by ID."""
    r = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    if not r:
        return None
    return _nurse_detail(r)


def filter_nurses(
    db: Session,
    group_id: str,
    *,
    grade: int | None = None,
    is_night_nurse: bool | None = None,
    team_id: int | None = None,
    has_preceptor: bool | None = None,
    joined_after: str | None = None,
    joined_before: str | None = None,
) -> list[dict]:
    """Filter nurses by various attributes."""
    q = db.query(Nurse).filter(Nurse.group_id == group_id, Nurse.active == 1)
    if grade is not None:
        q = q.filter(Nurse.grade == grade)
    if team_id is not None:
        q = q.filter(Nurse.team_id == team_id)
    if has_preceptor is True:
        q = q.filter(Nurse.preceptor_id.isnot(None))
    elif has_preceptor is False:
        q = q.filter(Nurse.preceptor_id.is_(None))
    if joined_after:
        q = q.filter(Nurse.joining_date >= joined_after)
    if joined_before:
        q = q.filter(Nurse.joining_date <= joined_before)
    rows = q.order_by(Nurse.sequence).all()
    result = [_nurse_summary(r) for r in rows]
    if is_night_nurse is not None:
        # is_night_nurse is JSON list; filter in Python
        result = [
            n for n in result
            if bool(n.get("is_night_nurse")) == is_night_nurse
        ]
    return result


def update_nurse_attribute(
    db: Session, nurse_id: str, attribute: str, value
) -> dict:
    """Update a single nurse attribute. Returns updated nurse summary."""
    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    if not nurse:
        return {"error": f"Nurse {nurse_id} not found"}
    if not hasattr(nurse, attribute):
        return {"error": f"Unknown attribute: {attribute}"}
    setattr(nurse, attribute, value)
    db.commit()
    db.refresh(nurse)
    return _nurse_summary(nurse)


# ── private helpers ──────────────────────────────────────────


def _nurse_summary(r: Nurse) -> dict:
    return {
        "nurse_id": r.nurse_id,
        "name": r.name,
        "grade": r.grade,
        "experience": r.experience,
        "role": r.role,
        "team_id": r.team_id,
        "is_head_nurse": bool(r.is_head_nurse),
        "is_night_nurse": r.is_night_nurse,
        "preceptor_id": r.preceptor_id,
        "fixed_shift": r.fixed_shift,
        "is_weekend_off": bool(r.is_weekend_off),
        "joining_date": str(r.joining_date) if r.joining_date else None,
        "work_shifts": r.work_shifts,
    }


def _nurse_detail(r: Nurse) -> dict:
    d = _nurse_summary(r)
    d.update({
        "account_id": r.account_id,
        "emp_num": r.emp_num,
        "office_id": r.office_id,
        "group_id": r.group_id,
        "level_": r.level_,
        "personal_off_adjustment": r.personal_off_adjustment,
        "weekly_off_enabled": bool(r.weekly_off_enabled),
        "weekly_off_weekday": r.weekly_off_weekday,
        "nurse_memo": r.nurse_memo,
        "active": r.active,
        "sequence": r.sequence,
        "enable_aide": bool(r.enable_aide) if r.enable_aide is not None else True,
        "wanted_max_requests": r.wanted_max_requests,
        "hn_auth": r.hn_auth,
    })
    return d
