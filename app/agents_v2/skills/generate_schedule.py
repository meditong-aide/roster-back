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
        return {
            "error": "no_config",
            "message": "근무표 생성 전에 제약 설정을 먼저 완료해주세요.",
        }

    # Check for existing running jobs
    latest = generation_tools.get_latest_job(db, group_id)
    if latest and latest.get("status") in ("QUEUED", "RUNNING"):
        return {
            "error": "already_in_progress",
            "message": (
                f"{year}년 {month}월 근무표를 이미 생성 중이에요. "
                "끝나면 알려드릴게요."
            ),
            "_existing_job_status": latest.get("status"),
        }

    if preview_only:
        return {
            "preview": True,
            "year": year,
            "month": month,
            "message": (
                f"{year}년 {month}월 근무표 생성을 시작하려고 합니다. 진행할까요?"
            ),
            "_internal": {
                "group_id": group_id,
                "config_id": config.get("config_id"),
            },
        }

    # Create job record
    job_id = f"job-agent-{year:04d}{month:02d}-{uuid.uuid4().hex[:8]}"
    office_id = config.get("office_id", "")
    nurse_id = params.get("requester_nurse_id", "agent")

    job = generation_tools.create_generation_job(
        db, job_id, group_id, office_id, nurse_id,
    )

    # User-facing 응답은 message 만, 시스템 키(job_id/_sqs_*)는 _internal 로 격리.
    # (agent.py layer 가 _internal 을 읽어 SQS dispatch.)
    return {
        "year": year,
        "month": month,
        "status": "queued",
        "message": (
            f"{year}년 {month}월 근무표 생성을 시작했어요. "
            "완료되면 알려드릴게요. 진행 상황을 확인하시려면 '생성 어디까지?' 라고 물어보세요."
        ),
        "_internal": {
            "job_id": job.get("job_id"),
            "sqs_dispatch_required": True,
            "generation_params": {
                "year": year,
                "month": month,
                "config_id": config.get("config_id"),
            },
        },
    }
