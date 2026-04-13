"""Base utilities shared across all tool modules."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def rows_to_dicts(rows: list[Any], columns: list[str] | None = None) -> list[dict]:
    """Convert SQLAlchemy model instances or row tuples to list[dict]."""
    if not rows:
        return []
    first = rows[0]
    # SQLAlchemy ORM model
    if hasattr(first, "__table__"):
        cols = [c.key for c in first.__table__.columns]
        return [{c: getattr(r, c) for c in cols} for r in rows]
    # Named tuple / Row
    if hasattr(first, "_fields"):
        return [r._asdict() for r in rows]
    # Plain tuple with explicit columns
    if columns:
        return [dict(zip(columns, r)) for r in rows]
    return [{"value": r} for r in rows]


def serialize_time(val: Any) -> str | None:
    """Convert datetime.time or similar to HH:MM string."""
    if val is None:
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%H:%M")
    return str(val)
