import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import boto3
import dotenv
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import RosterRequest
from services.roster_create_service import (
    generate_roster_service,
    generate_roster_service_with_fixed_cells,
    request_schedule_service,
)

router = APIRouter(tags=["roster_create"])


class HoldGenerateRequest(BaseModel):
    """
    고정 셀 정보를 포함한 근무표 생성 요청 모델.

    인자:
        year: 대상 연도 (예: 2025).
        month: 대상 월 (예: 3).
        fixed_cells: 미리 확정한 셀 목록.
        config_id: 사용 설정 ID.
        distribution_mode: 근무 배분 모드.
        oversupply_balance_gauge: 과잉 공급 균형 지표(0~10, 예: 6).
        monthly_preference_gauge: 월 선호도 반영 지표(0~10, 예: 3).
        monthly_shift_preferences: 개인별 월간 선호 근무 설정.
    반환:
        BaseModel: Pydantic 검증을 거친 요청 데이터.
    예외:
        ValidationError: 필드 검증 실패 시 발생.
    예시:
        year=2025, month=3, oversupply_balance_gauge=6 → 검증 통과.
    """

    year: int
    month: int
    fixed_cells: List[Dict[str, Any]]
    config_id: Optional[int] = None
    # ── Shift 분배 정책(임시: UI 대신 req로 제어) ──
    distribution_mode: str = "hybrid"
    oversupply_balance_gauge: Optional[int] = Field(default=6, ge=0, le=10)
    monthly_preference_gauge: Optional[int] = Field(default=3, ge=0, le=10)
    monthly_shift_preferences: Optional[Dict[str, Dict[str, Any]]] = None


dotenv.load_dotenv()

sqs = boto3.client("sqs", region_name="ap-northeast-2")
QUEUE_URL = os.getenv("SQS_QUEUE_URL")


async def _send_sqs_job(job_body: Dict[str, Any]) -> Dict[str, Any]:
    """
    SQS에 근무표 생성 작업 메시지를 전송한다.

    인자:
        job_body: 직렬화될 작업 본문.
    반환:
        dict: SQS 전송 응답 메타데이터.
    예외:
        HTTPException: 환경 변수 미설정(500) 또는 전송 실패(502).
    예시:
        job_id가 "job-1-202503-abc12345"인 메시지 전송.
    """

    if not QUEUE_URL:
        print('QUEUE_URL', QUEUE_URL)
        raise HTTPException(status_code=500, detail="SQS_QUEUE_URL이 설정되지 않았습니다.")

    try:
        message_body = json.dumps(job_body, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        print('message_body', message_body)
        raise HTTPException(status_code=400, detail=f"잘못된 메시지 본문: {exc}") from exc

    try:
        from functools import partial
        response = await to_thread.run_sync(
            partial(
                sqs.send_message,
                QueueUrl=QUEUE_URL,
                MessageBody=message_body,
            )
        )
    except Exception as exc:  # boto3 예외 타입 다양
        # print('response', response)
        print('exc', exc)
        raise HTTPException(status_code=502, detail=f"SQS 전송 실패: {exc}") from exc

    return response


@router.post("/roster_create/async")
async def roster_create_async(
    req: RosterRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    _db: Session = Depends(get_db),
    wait_for_result: bool = False,
):
    """
    근무표 생성 작업을 SQS로 위임하여 비동기로 처리한다.

    인자:
        req: 근무표 생성 요청 DTO.
        db: DB 세션 (현재는 인증/권한 검증을 위한 의존성).
        current_user: 로그인 사용자 정보.
        wait_for_result: True일 때 동기 처리로 즉시 결과 반환.
    반환:
        dict: 생성된 job 정보와 SQS 응답 메타데이터 또는 동기 생성 결과.
    예외:
        HTTPException: 큐 전송 실패 시 502, 환경 변수 미설정 시 500.
    예시:
        year=2025, month=3 요청, wait_for_result=False → job-<user>-202503-<uuid> 전송.
        year=2025, month=3 요청, wait_for_result=True → 동기 생성 결과 반환.
    """

    if wait_for_result:
        # 동기로 바로 생성(레거시/테스트 용). CPU 부하는 EC2에 남습니다.
        try:
            return {
                "mode": "sync",
                "result": generate_roster_service(req, current_user, _db),
            }
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"근무표 생성 실패: {exc}"
            ) from exc

    job_body: Dict[str, Any] = {
        "job_id": (
            f"job-{current_user.nurse_id}-"
            f"{req.year:04d}{req.month:02d}-"
            f"{uuid.uuid4().hex[:8]}"
        ),
        "nurse_id": current_user.nurse_id,
        "account_id": current_user.account_id,
        "office_id": current_user.office_id,
        "group_id": current_user.group_id,
        "params": req.dict(),
        "requested_at": datetime.utcnow().isoformat(),
    }

    response = await _send_sqs_job(job_body)

    return {
        "message": "✅ Job submitted to SQS",
        "job": job_body,
        "sqs_message_id": response.get("MessageId"),
    }


# [Roster] - 근무표 생성
@router.post("/roster_create/generate")
async def generate_roster_endpoint(
    req: RosterRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    동기 방식으로 근무표를 생성한다.

    인자:
        req: 근무표 생성 요청 DTO.
        current_user: 로그인 사용자 정보.
        db: DB 세션.
    반환:
        dict: 생성된 근무표 데이터.
    예외:
        HTTPException: 내부 오류 발생 시 500.
    예시:
        year=2025, month=3 요청 시 동기 생성 결과 반환.
    """
    try:
        return generate_roster_service(req, current_user, db)
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"근무표 생성 실패: {str(e)}")


    # [Schedules] - 수간호사가 근무표 생성 요청
@router.post("/roster/request")
async def request_schedule(
    req: RosterRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    수간호사 근무표 생성 요청을 기록하고 처리한다.

    인자:
        req: 근무표 생성 요청 DTO.
        current_user: 로그인 사용자 정보.
        db: DB 세션.
    반환:
        dict: 요청 등록 결과.
    예외:
        HTTPException: 처리 실패 시 500.
    예시:
        year=2025, month=3 요청 등록 후 상태 반환.
    """
    try:
        return request_schedule_service(req, current_user, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"스케줄 생성 실패: {str(e)}")


# [Roster] - 고정된 셀을 반영한 근무표 생성
@router.post("/roster_create/hold_generate")
async def hold_generate_roster_endpoint(
    req: HoldGenerateRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    고정된 셀 정보를 반영해 근무표를 생성한다.

    인자:
        req: 고정 셀 포함 근무표 생성 요청 DTO.
        current_user: 로그인 사용자 정보.
        db: DB 세션.
    반환:
        dict: 생성된 근무표 데이터.
    예외:
        HTTPException: 내부 오류 발생 시 500.
    예시:
        fixed_cells를 포함한 year=2025, month=3 요청 처리.
    """
    try:
        # 고정된 셀 정보를 포함하여 근무표 생성 서비스 호출
        return generate_roster_service_with_fixed_cells(req, current_user, db)
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"고정 후 근무표 생성 실패: {str(e)}")


    