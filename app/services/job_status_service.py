"""
근무표 생성 작업(Job) 상태 관리 서비스.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from db.models import RosterJob

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"


def create_job_record(
    db: Session,
    job_id: str,
    office_id: Optional[str],
    group_id: Optional[str],
    nurse_id: Optional[str],
) -> RosterJob:
    """
    신규 Job 레코드를 생성한다.

    인자:
        db: DB 세션.
        job_id: 작업 식별자.
        office_id: 병원 ID.
        group_id: 병동/그룹 ID.
        nurse_id: 간호사 ID.
    반환:
        RosterJob: 생성된 레코드.
    예외:
        IntegrityError: 중복 job_id 등 제약 위반 시 발생.
    예시:
        job_id="job-123" → status=QUEUED, progress=0 으로 생성.
    """
    job = RosterJob(
        job_id=job_id,
        office_id=office_id,
        group_id=group_id,
        nurse_id=nurse_id,
        status=STATUS_QUEUED,
        progress=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def update_job_record(
    db: Session,
    job_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    result_roster_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> RosterJob:
    """
    Job 레코드를 업데이트한다.

    인자:
        db: DB 세션.
        job_id: 작업 식별자.
        status: 변경할 상태.
        progress: 진행률(0~100).
        result_roster_id: 완료 시 생성된 roster ID.
        error_message: 실패 메시지.
    반환:
        RosterJob | None: 업데이트된 레코드, 없으면 None.
    예시:
        status="RUNNING", progress=10 → 실행 중 표시.
    """
    print('job_id', job_id)
    job = db.query(RosterJob).filter(RosterJob.job_id == job_id).first()
    print('job', job.__dict__)
    if not job:
        raise ValueError(f"job_id를 찾을 수 없습니다: {job_id}")

    if status is not None:
        job.status = status
    if progress is not None:
        job.progress = progress
    if result_roster_id is not None:
        job.result_roster_id = result_roster_id
    if error_message is not None:
        job.error_message = error_message

    db.commit()
    db.refresh(job)
    return job


# def get_job_record(db: Session, job_id: str) -> Optional[RosterJob]:
#     """
#     Job 레코드를 조회한다.

#     인자:
#         db: DB 세션.
#         job_id: 작업 식별자.
#     반환:
#         RosterJob | None: 조회 결과.
#     예시:
#         job_id="job-123" → 상태/결과 반환.
#     """
#     return db.query(RosterJob).filter(RosterJob.job_id == job_id).first()


def get_latest_job_record(
    db: Session,
    *,
    office_id: Optional[str],
    group_id: Optional[str],
    nurse_id: Optional[str],
) -> Optional[RosterJob]:
    """
    office/group/nurse 기준으로 가장 최신 Job 레코드를 조회한다.

    인자:
        db: DB 세션.
        office_id: 병원 ID.
        group_id: 그룹/병동 ID.
        nurse_id: 요청 간호사 ID.
    반환:
        RosterJob | None: 조회 결과.
    예시:
        최신 생성 요청의 상태 확인 용도.
    """
    query = (
        db.query(RosterJob)
        .filter(
            RosterJob.office_id == office_id,
            RosterJob.group_id == group_id,
            RosterJob.nurse_id == nurse_id,
        )
        .order_by(RosterJob.created_at.desc())
    )
    return query.first()

