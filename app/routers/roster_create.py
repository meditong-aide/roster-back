import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
import dotenv
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.client2 import get_db
from db.models import RosterConfig
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import RosterRequest, RosterConfigCreate
from services.roster_create_service import (
    generate_roster_service,
    # generate_roster_service_with_fixed_cells,
    request_schedule_service,
)
from services.job_status_service import create_job_record
from services.group_access import resolve_effective_group

router = APIRouter(tags=["roster_create"])


def _fallback_unrecoverable_from_exception(error_message: str) -> dict:
    """비구조화 예외를 표준 infeasibility payload로 변환한다."""
    from services.precheck import build_unrecoverable_payload

    msg = str(error_message or "")
    reasons: list[dict] = []
    seen: set[str] = set()

    def _add(code: str):
        if code in seen:
            return
        seen.add(code)
        reasons.append(
            {
                "reason_code": code,
                "node_id": f"infeasibility:{code.lower()}",
                "details": {"source": "router_exception_fallback"},
                "human_message_ko": msg[:300],
            }
        )

    for m in re.finditer(r"\[reason_code=([A-Za-z_]+)\]", msg, flags=re.IGNORECASE):
        _add(m.group(1).upper())

    # NO_ASSIGNMENT / NO_ASSIGNMENT_* 4축 라벨은 "결과(미배정 cell)" 일 뿐 cause 가 아님 — 차단.
    # 진짜 cause 는 산술 detector (team_grade_precheck) + MUS inferer (cause_inferer)
    # 가 더 정확한 cause_id 로 만들어준다.

    if not reasons:
        _add("INTERNAL_GENERATION_ERROR")

    return build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason=msg,
        violated_constraints=reasons,
        conflict_cores=[],
        pool_snapshot={},
    )


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

BASE_DIR = Path(__file__).resolve().parent.parent
dotenv.load_dotenv(BASE_DIR / ".env")

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
    request: Request,
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

    # 대상 그룹: 토큰 group_id 대신 req.group_id(없으면 DB home)로 해석·검증(권한 없으면 403).
    # 워커가 params 로 동일 그룹을 재현하도록 req.group_id 에 되써 둔다.
    target_group_id = resolve_effective_group(
        _db, current_user, getattr(req, "group_id", None)
    )
    req.group_id = target_group_id

    # 모달 payload 제공 시: config 굳히기(materialize) + 라이브 동기화(apply).
    # 변경 시 '새로운 설정n' 신규 row, 동일 시 baseline 재사용. 이후 req.config_id 로 진행.
    materialized = None
    if getattr(req, "config", None):
        from services.roster_service import materialize_generation_config
        try:
            payload_cfg = RosterConfigCreate(**req.config)
            resolved = materialize_generation_config(
                _db, payload_cfg, current_user, override_group_id=target_group_id
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"설정 materialize 실패: {exc}"
            ) from exc
        req.config_id = resolved.config_id
        req.config = None  # 워커는 config_id 만 사용 — job_body 슬림화
        materialized = {
            "config_id": resolved.config_id,
            "version": resolved.version,
            "config_name": resolved.config_name,
        }

    if wait_for_result:
        # 동기로 바로 생성(레거시/테스트 용). CPU 부하는 EC2에 남습니다.
        try:
            return {
                "mode": "sync",
                "result": generate_roster_service(
                    req, current_user, _db,
                    config_override=(getattr(req, "config_override", None) or None),
                    treatment_ids=(getattr(req, "treatment_ids", None) or None),
                    weekend_off_release=(getattr(req, "weekend_off_release", None) or None),
                    monthly_limit_release=(getattr(req, "monthly_limit_release", None) or None),
                ),
                "materialized_config": materialized,
            }
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"근무표 생성 실패: {exc}"
            ) from exc

    # 진단용: 호출자 추적. 백엔드 stdout 로그 파이프에 의존하지 않고, 클라이언트 IP/UA 를
    # SQS 페이로드에 실어 워커(검증된 람다 로그 파이프)에서 찍는다.
    # ALB/CloudFront 뒤 실제 IP 는 X-Forwarded-For 첫 항목.
    _xff = request.headers.get("x-forwarded-for", "")
    _client_ip = _xff.split(",")[0].strip() if _xff else (
        request.client.host if request.client else "-"
    )
    _user_agent = request.headers.get("user-agent", "-")

    job_body: Dict[str, Any] = {
        "job_id": (
            f"job-{current_user.nurse_id}-"
            f"{req.year:04d}{req.month:02d}-"
            f"{uuid.uuid4().hex[:8]}"
        ),
        "nurse_id": current_user.nurse_id,
        "account_id": current_user.account_id,
        "office_id": current_user.office_id,
        "group_id": target_group_id,
        "params": req.dict(),
        "requested_at": datetime.utcnow().isoformat(),
        "client_ip": _client_ip,
        "x_forwarded_for": _xff,
        "user_agent": _user_agent,
    }

    # 상태 테이블에 Job 생성(QUEUED)
    try:
        create_job_record(
            db=_db,
            job_id=job_body["job_id"],
            office_id=current_user.office_id,
            group_id=target_group_id,
            nurse_id=current_user.nurse_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Job 생성 실패: {exc}") from exc

    response = await _send_sqs_job(job_body)

    return {
        "message": "✅ Job submitted to SQS",
        "job": job_body,
        "sqs_message_id": response.get("MessageId"),
        "materialized_config": materialized,
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
        # 해결책 재생성 파라미터를 서비스로 전달(누락 시 config_override 의 비-DB 솔버키
        # (예: weekend_off_only_enable)와 weekend_off_release/monthly_limit_release 가 반영 안 됨).
        return generate_roster_service(
            req, current_user, db,
            treatment_ids=(getattr(req, "treatment_ids", None) or None),
            config_override=(getattr(req, "config_override", None) or None),
            weekend_off_release=(getattr(req, "weekend_off_release", None) or None),
            monthly_limit_release=(getattr(req, "monthly_limit_release", None) or None),
        )
    except HTTPException:
        # 구조화된 infeasibility 페이로드 등 의도된 HTTPException은 그대로 전파
        raise
    except Exception as e:
        print('error', e)
        payload = _fallback_unrecoverable_from_exception(f"근무표 생성 실패: {str(e)}")
        raise HTTPException(status_code=500, detail=payload)


class ApplyResolutionRequest(BaseModel):
    """infeasibility 해결 옵션을 적용해 재생성하는 요청.

    apply 는 /roster_create/generate 의 infeasibility.resolution_options[*].apply 를
    그대로 echo 한 것(RosterConfig 컬럼 delta, 예: {"max_nig_per_month": 18}).
    """
    year: int
    month: int
    grade_strategy: Optional[str] = None
    # probe 옵션: RosterConfig 컬럼 delta(예: {"max_nig_per_month": 18}).
    apply: Dict[str, Any] = {}
    # ontology 옵션: 적용할 treatment_id 리스트(런타임 플래그라 DB 미변경, 이번 생성만).
    treatment_ids: Optional[List[str]] = None
    option_id: Optional[str] = None
    # True 면 재생성 성공 시 설정 변경을 영구 반영("이 설정 저장"). 기본은 transient(미리보기).
    # 실패(여전히 infeasible)면 persist 여부와 무관하게 원복. (treatment_ids 경로엔 미적용)
    persist: bool = False


@router.post("/roster_create/apply-resolution")
async def apply_resolution_endpoint(
    req: ApplyResolutionRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """선택한 해결 옵션(설정 delta)을 **이번 생성에만 transient 적용**해 재생성한다.

    동작: RosterConfig 컬럼 snapshot → delta 적용(commit) → generate_roster_service →
    (persist=False) finally 에서 원복 / (persist=True 이고 성공) 원복 생략(영구 반영).
    기본 transient 라 여러 옵션을 부담 없이 미리보기 가능, 마음에 들면 persist=True 로 저장.
    적용 후에도 실패하면 persist 와 무관하게 원복하고 새 infeasibility(500)가 전파된다.

    주의: 적용~원복 사이 동안 해당 group 의 config 가 일시 변경되므로, 동일 group 의 동시
    생성과는 경합 가능(수간호사 월간 생성 빈도상 위험 낮음). 영구 반영은 별도 설정 저장 단계.
    """
    delta = {k: v for k, v in (req.apply or {}).items()}
    treatment_ids = list(req.treatment_ids or [])
    if not delta and not treatment_ids:
        raise HTTPException(status_code=400, detail="apply(컬럼 delta) 또는 treatment_ids 가 필요합니다.")
    gen_req = RosterRequest(year=req.year, month=req.month, grade_strategy=req.grade_strategy)

    # ── ontology treatment 경로: 런타임 적용(DB 미변경, 이번 생성만, persist N/A) ──
    if treatment_ids:
        try:
            result = generate_roster_service(gen_req, current_user, db, treatment_ids=treatment_ids)
            if isinstance(result, dict):
                result["applied_resolution"] = {
                    "option_id": req.option_id, "treatment_ids": treatment_ids, "persisted": False,
                }
            return result
        except HTTPException:
            raise  # 여전히 infeasible → 새 옵션 payload 전파
        except Exception as e:
            print("apply-resolution(treatment) error", e)
            payload = _fallback_unrecoverable_from_exception(f"treatment 적용 재생성 실패: {str(e)}")
            raise HTTPException(status_code=500, detail=payload)

    # ── 설정 delta 경로: 컬럼 키는 DB(persist 가능), 비-컬럼 solver 키는 config_override(이번 생성만) ──
    # probe 옵션의 apply 키 중 DB 컬럼이 아닌 solver 파라미터(weekend_off_only_enable, ban_n_to_d,
    # team_min_soft_fallback, max_consecutive_nights 등)는 컬럼 검증에서 400 나던 것을,
    # RosterConfig(dataclass) 필드면 허용하고 config_override 로 라우팅해 적용한다.
    import dataclasses as _dc
    from db.roster_config import NurseRosterConfig as _NRC
    allowed_cols = {c.name for c in RosterConfig.__table__.columns}
    _valid_override = {f.name for f in _dc.fields(_NRC)}
    bad = [k for k in delta if k not in allowed_cols and k not in _valid_override]
    if bad:
        raise HTTPException(status_code=400, detail=f"적용 불가한 설정 키: {bad}")
    col_delta = {k: v for k, v in delta.items() if k in allowed_cols}
    override_delta = {k: v for k, v in delta.items() if k not in allowed_cols}

    rc = None
    cid = None
    snapshot: dict = {}
    if col_delta:
        rc = (
            db.query(RosterConfig)
            .filter(RosterConfig.group_id == current_user.group_id)
            .order_by(RosterConfig.config_id.desc())
            .first()
        )
        if rc is None:
            raise HTTPException(status_code=404, detail="roster_config 를 찾을 수 없습니다.")
        cid = rc.config_id
        snapshot = {k: getattr(rc, k) for k in col_delta}
    _keep = False  # persist + 성공 시에만 True → 컬럼 변경 원복 생략(영구 반영)
    try:
        if col_delta:
            for k, v in col_delta.items():
                setattr(rc, k, v)
            db.commit()
        # 비-컬럼 키는 DB 미변경 → config_override 로 이번 생성에만 주입
        result = generate_roster_service(
            gen_req, current_user, db, config_override=(override_delta or None)
        )
        if bool(getattr(req, "persist", False)):
            _keep = True  # 성공 후에만 도달 → 컬럼 변경 영구 유지
        if isinstance(result, dict):
            result["applied_resolution"] = {
                "option_id": req.option_id, "changes": delta, "persisted": _keep,
                # 비-컬럼 키는 DB 컬럼이 없어 영구저장 불가 → persist 여부와 무관하게 이번 생성만 적용
                "transient_keys": (list(override_delta.keys()) or None),
            }
        return result
    except HTTPException:
        raise  # 여전히 infeasible → _keep=False → finally 컬럼 원복, 새 옵션 payload 전파
    except Exception as e:
        print("apply-resolution error", e)
        payload = _fallback_unrecoverable_from_exception(f"해결책 적용 재생성 실패: {str(e)}")
        raise HTTPException(status_code=500, detail=payload)
    finally:
        if col_delta and not _keep:
            try:
                rc2 = db.query(RosterConfig).filter(RosterConfig.config_id == cid).first()
                if rc2 is not None:
                    for k, v in snapshot.items():
                        setattr(rc2, k, v)
                    db.commit()
            except Exception as _re:
                try:
                    db.rollback()
                except Exception:
                    pass
                print("apply-resolution restore failed", _re)
        elif _keep:
            print(f"[apply-resolution] persisted: config_id={cid} col_delta={col_delta} transient={override_delta}")


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


    
