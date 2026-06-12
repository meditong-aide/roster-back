import asyncio
import time
from datetime import datetime
from threading import Lock
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from db.client2 import SessionLocal
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.job_status_service import get_latest_job_record

router = APIRouter(tags=["jobs"])

# Long-polling 설정 (프론트 변경 없이 동일 엔드포인트로 동작)
_TERMINAL_STATUSES = {"SUCCESS", "FAILED"}
_LONG_POLL_WAIT = 25       # 최대 대기 시간(초). ALB idle timeout 60초 안전마진
_LONG_POLL_INTERVAL = 1.0  # 서버 측 DB 재조회 간격(초)
# polling 끊김(새로고침) 감지용 last_seen TTL.
# 정상 polling interval(3초) 보다 약간 큰 값 → 새로고침으로 갭 발생 시 만료되어
# 다음 요청 즉시 응답 (long-poll 대기 회피, 모달이 빈 상태 유지되는 시간 최소화).
_LAST_SEEN_TTL = 4.0

# 사용자별 last-seen (job_id, status, set_at_monotonic) 추적.
# 단일 API 인스턴스 가정 (멀티 인스턴스 운영 시 Redis 등 외부 저장소 필요).
_LAST_SEEN: Dict[str, Tuple[str, str, float]] = {}
_LAST_SEEN_LOCK = Lock()


def _user_key(office_id: Optional[str], group_id: Optional[str], nurse_id: Optional[str]) -> str:
    return f"{office_id or ''}:{group_id or ''}:{nurse_id or ''}"


def _get_last_seen(key: str) -> Optional[Tuple[str, str]]:
    """TTL 만료 체크 후 (job_id, status) 반환. 만료 시 자동 정리 + None."""
    with _LAST_SEEN_LOCK:
        entry = _LAST_SEEN.get(key)
        if entry is None:
            return None
        job_id, status, set_at = entry
        if time.monotonic() - set_at > _LAST_SEEN_TTL:
            _LAST_SEEN.pop(key, None)
            return None
        return (job_id, status)


def _set_last_seen(key: str, value: Tuple[str, str]) -> None:
    with _LAST_SEEN_LOCK:
        _LAST_SEEN[key] = (value[0], value[1], time.monotonic())


def _clear_last_seen(key: str) -> None:
    with _LAST_SEEN_LOCK:
        _LAST_SEEN.pop(key, None)


class JobStatusResponse(BaseModel):
    """Job 상태 응답.
    - status: QUEUED/RUNNING/SUCCESS/FAILED
    - progress: 0~100
    """
    job_id: str
    status: str
    progress: Optional[int] = None
    result_roster_id: Optional[str] = None
    error_message: Optional[str] = None
    updated_at: Optional[datetime] = None


def _build_response(job) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        result_roster_id=job.result_roster_id,
        error_message=job.error_message,
        updated_at=job.updated_at,
    )


def _fetch_latest_job(office_id: Optional[str], group_id: Optional[str], nurse_id: Optional[str]):
    """매 호출마다 새 session으로 latest job 조회.
    long-poll 대기 중 connection 점유를 피해 pool 고갈을 방지.
    """
    session = SessionLocal()
    try:
        return get_latest_job_record(
            session,
            office_id=office_id,
            group_id=group_id,
            nurse_id=nurse_id,
        )
    finally:
        session.close()


@router.get("/jobs/status/latest", response_model=JobStatusResponse)
async def get_latest_job_status(
    response: Response,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
):
    """현재 사용자(office/group/nurse) 기준 가장 최신 Job 상태.

    프론트 변경 없이 자동 long-polling (서버 측 last-seen 추적):
        - 첫 호출: 현재 status 즉시 반환 + 메모리 기록.
        - 동일 (job_id, status) 의 후속 호출: status 변경 또는 새 job 등장 시까지
          최대 _LONG_POLL_WAIT 초 대기 후 응답.
        - 종료 status(SUCCESS/FAILED) 시 즉시 응답 + last-seen 정리.
        - deadline 도달 시 현재 status 반환(재연결 유도).

    제약:
        단일 API 인스턴스 가정. 멀티 인스턴스 운영 시 Redis 등 외부 state 필요.

    예외:
        404: 해당 사용자/그룹의 Job이 없을 때.
    """
    # 폴링 응답이 브라우저/CloudFront 디스크 캐시에 stale 상태로 남아 폴링이 멈추는 것을 방지
    response.headers["Cache-Control"] = "no-store"
    if current_user is None:
        raise HTTPException(
            status_code=401,
            detail="인증이 필요합니다.",
            headers={"Cache-Control": "no-store"},
        )
    key = _user_key(current_user.office_id, current_user.group_id, current_user.nurse_id)
    deadline = time.monotonic() + _LONG_POLL_WAIT
    last_seen = _get_last_seen(key)

    while True:
        job = await asyncio.to_thread(
            _fetch_latest_job,
            current_user.office_id,
            current_user.group_id,
            current_user.nurse_id,
        )
        if not job:
            raise HTTPException(
                status_code=404,
                detail="해당 사용자/그룹의 Job이 없습니다.",
                headers={"Cache-Control": "no-store"},
            )

        current = (job.job_id, job.status)

        # 1) 첫 호출 또는 (job_id, status) 변경 감지 → 즉시 응답
        if last_seen is None or last_seen != current:
            if job.status in _TERMINAL_STATUSES:
                _clear_last_seen(key)  # 종료 후 새 작업 대비 정리
            else:
                _set_last_seen(key, current)
            return _build_response(job)

        # 2) 동일 (job_id, status) 이고 종료 status면 즉시 응답 (이미 last_seen 정리됨이 정상,
        #    예외적으로 남아있으면 통과 응답)
        if job.status in _TERMINAL_STATUSES:
            return _build_response(job)

        # 3) deadline 도달 → 현재 status 반환 (재연결 유도)
        if time.monotonic() >= deadline:
            return _build_response(job)

        await asyncio.sleep(_LONG_POLL_INTERVAL)
