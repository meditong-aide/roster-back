"""generate-schedule skill — trigger roster generation (async via SQS)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import generation_tools, constraint_tools


@register("generate-schedule")
def generate_schedule(db: Session, params: dict) -> Any:
    """Trigger a new roster generation job.

    Note: This creates the job record only. Actual SQS dispatch
    must be handled by the calling layer (agent.py / router).
    """
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    preview_only = params.get("preview_only", False)

    # Verify config exists
    config = constraint_tools.get_roster_config(db, group_id)
    if not config:
        return {"error": "No roster config found — configure constraints first"}

    # Check for existing running jobs
    latest = generation_tools.get_latest_job(db, group_id)
    if latest and latest.get("status") in ("QUEUED", "RUNNING"):
        return {
            "error": "A generation job is already in progress",
            "existing_job": latest,
        }

    if preview_only:
        return {
            "preview": True,
            "group_id": group_id,
            "year": year,
            "month": month,
            "config_id": config.get("config_id"),
            "message": "Will create a new roster generation job",
        }

    # Create job record
    job_id = f"job-agent-{year:04d}{month:02d}-{uuid.uuid4().hex[:8]}"
    office_id = config.get("office_id", "")
    nurse_id = params.get("requester_nurse_id", "agent")

    job = generation_tools.create_generation_job(
        db, job_id, group_id, office_id, nurse_id,
    )

    # Return job info — the agent.py layer handles SQS dispatch
    job["_sqs_dispatch_required"] = True
    job["_generation_params"] = {
        "year": year,
        "month": month,
        "config_id": config.get("config_id"),
    }
    return job
