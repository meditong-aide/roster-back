from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.client2 import get_db
from services.job_status_service import get_job_record

router = APIRouter(tags=["jobs"])


class JobStatusResponse(BaseModel):
    """
    Job 상태 응답 모델.

    인자:
        job_id: 작업 ID.
        status: QUEUED/RUNNING/SUCCESS/FAILED.
        progress: 진행률(0~100).
        result_roster_id: 생성된 근무표 ID.
        error_message: 실패 메시지.
    반환:
        BaseModel: 상태 정보.
    예시:
        status="RUNNING", progress=50 → 진행 중 응답.
    """

    job_id: str
    status: str
    progress: Optional[int] = None
    result_roster_id: Optional[str] = None
    error_message: Optional[str] = None


@router.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
def get_job_status(job_id: str, db: Session = Depends(get_db)):
    """
    Job 상태를 조회한다.

    인자:
        job_id: 작업 ID.
        db: DB 세션.
    반환:
        JobStatusResponse: 현재 상태 정보.
    예외:
        404: 존재하지 않는 job_id.
    예시:
        /jobs/job-123/status → RUNNING, progress=40.
    """

    job = get_job_record(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id를 찾을 수 없습니다.")

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        result_roster_id=job.result_roster_id,
        error_message=job.error_message,
    )

