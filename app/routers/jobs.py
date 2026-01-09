from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.job_status_service import get_latest_job_record

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


# @router.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
# def get_job_status(job_id: str, db: Session = Depends(get_db)):
#     """
#     Job 상태를 조회한다.

#     인자:
#         job_id: 작업 ID.
#         db: DB 세션.
#     반환:
#         JobStatusResponse: 현재 상태 정보.
#     예외:
#         404: 존재하지 않는 job_id.
#     예시:
#         /jobs/job-123/status → RUNNING, progress=40.
#     """

#     job = get_job_record(db, job_id)
#     if not job:
#         raise HTTPException(status_code=404, detail="job_id를 찾을 수 없습니다.")

#     return JobStatusResponse(
#         job_id=job.job_id,
#         status=job.status,
#         progress=job.progress,
#         result_roster_id=job.result_roster_id,
#         error_message=job.error_message,
#     )


@router.get("/jobs/status/latest", response_model=JobStatusResponse)
def get_latest_job_status(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    현재 사용자(office/group/nurse) 기준 가장 최신 Job 상태를 조회한다.

    인자:
        current_user: 로그인 사용자 정보.
        db: DB 세션.
    반환:
        JobStatusResponse: 최신 Job 상태.
    예외:
        404: 해당 사용자/그룹의 Job이 없을 때.
    예시:
        /jobs/status/latest → 최근 요청의 상태 반환.
    """
    job = get_latest_job_record(
        db,
        office_id=current_user.office_id,
        group_id=current_user.group_id,
        nurse_id=current_user.nurse_id,
    )
    if not job:
        raise HTTPException(status_code=404, detail="해당 사용자/그룹의 Job이 없습니다.")

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        result_roster_id=job.result_roster_id,
        error_message=job.error_message,
        updated_at=job.updated_at,
    )

