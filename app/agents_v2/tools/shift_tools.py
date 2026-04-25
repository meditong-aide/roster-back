"""Shift definition tools — read group-specific shift code definitions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import Shift
from agents_v2.tools.base import serialize_time


def read_shift_definitions(db: Session, group_id: str) -> list[dict]:
    """Return all shift codes for a group with time/category info.

    Each dict contains:
        shift_id, name, shift_gb (category bucket like 데이/이브닝/나이트/오프),
        type ('근무'/'오프'/'휴가' etc.), start_time, end_time,
        is_working (bool), is_weekly_off, auto_schedule, sequence
    """
    rows = (
        db.query(Shift)
        .filter(Shift.group_id == group_id)
        .order_by(Shift.sequence)
        .all()
    )
    return [
        {
            "shift_id": r.shift_id,
            "name": r.name,
            "shift_gb": r.shift_gb,
            "type": r.type,
            "start_time": serialize_time(r.start_time),
            "end_time": serialize_time(r.end_time),
            "is_working": r.type == "근무",
            "is_weekly_off": bool(r.is_weekly_off),
            "auto_schedule": bool(r.auto_schedule),
            "allday": bool(r.allday),
            "duration": r.duration,
            "sequence": r.sequence,
            "color": r.color,
            "default_shift": r.default_shift,
            "show_in_preference": bool(r.show_in_preference),
        }
        for r in rows
    ]
