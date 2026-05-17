"""Generation tools — trigger roster creation and check job status."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session
from sqlalchemy import desc

from db.models import RosterJob
from agents_v2.tools.narrative_formatter import format_infeasibility


def get_job_status(db: Session, job_id: str) -> dict | None:
    """Get the status of a roster generation job."""
    row = db.query(RosterJob).filter(RosterJob.job_id == job_id).first()
    if not row:
        return None
    return _job_dict(row)


def get_latest_job(
    db: Session,
    group_id: str,
    *,
    office_id: str | None = None,
) -> dict | None:
    """Get the most recent roster job for a group."""
    q = db.query(RosterJob).filter(RosterJob.group_id == group_id)
    if office_id:
        q = q.filter(RosterJob.office_id == office_id)
    row = q.order_by(desc(RosterJob.created_at)).first()
    if not row:
        return None
    return _job_dict(row)


def list_recent_jobs(
    db: Session,
    group_id: str,
    *,
    limit: int = 10,
    status_filter: str | None = None,
) -> list[dict]:
    """List recent roster generation jobs for a group."""
    q = db.query(RosterJob).filter(RosterJob.group_id == group_id)
    if status_filter:
        q = q.filter(RosterJob.status == status_filter)
    rows = q.order_by(desc(RosterJob.created_at)).limit(limit).all()
    return [_job_dict(r) for r in rows]


def create_generation_job(
    db: Session,
    job_id: str,
    group_id: str,
    office_id: str,
    nurse_id: str,
) -> dict:
    """Create a new roster generation job record (QUEUED status).

    Note: This only creates the DB record. The actual SQS dispatch
    is handled by the caller (agent executor or API layer).
    """
    job = RosterJob(
        job_id=job_id,
        office_id=office_id,
        group_id=group_id,
        nurse_id=nurse_id,
        status="QUEUED",
        progress=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _job_dict(job)


# ── private ──────────────────────────────────────────────


def _job_dict(row: RosterJob) -> dict:
    d = {
        "job_id": row.job_id,
        "office_id": row.office_id,
        "group_id": row.group_id,
        "nurse_id": row.nurse_id,
        "status": row.status,
        "progress": row.progress,
        "result_roster_id": row.result_roster_id,
        "error_message": row.error_message,
        "created_at": str(row.created_at) if row.created_at else None,
        "updated_at": str(row.updated_at) if row.updated_at else None,
    }
    # error_message 가 JSON 직렬화된 unrecoverable_payload 이면 narrative 추출.
    # worker.py 가 HTTPException.detail (dict) 를 JSON 으로 저장한다.
    em = row.error_message
    if em and em.lstrip().startswith("{"):
        try:
            payload = json.loads(em)
            narrative = format_infeasibility(payload)
            if narrative:
                d["infeasibility"] = narrative
        except (json.JSONDecodeError, ValueError):
            pass
    return d
