# app/worker.py
"""Roster solver worker — ECS task와 Lambda 양쪽 공통 진입점.

import 정책:
    - top-level은 표준 라이브러리 + 가벼운 logger 만 (os, sys, json, traceback, powertools.Logger).
    - 무거운 의존성(sqlalchemy, db.*, schemas.*, services.*)은 함수 내부로 lazy import.
    - 목적: Lambda init phase 10초 timeout 회피 + ECS 호환 유지.

Logging:
    aws-lambda-powertools.Logger(child=True) 로 lambda_handler 의 부모 logger 컨텍스트 상속.
    process_job 진입 시 office_id 를 append_keys 로 추가 주입.
    핵심 비즈니스 이벤트(작업 시작/완료/실패)만 JSON 으로 logging.
    그 외 디버깅 print 는 표준 stdout 그대로 유지 (CloudWatch 표준 log group).
"""
from __future__ import annotations
import os, sys

# sys.path 설정: app/ 디렉토리를 추가하여 db, schemas 등을 직접 import 가능하게 함
app_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(app_dir, '..'))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import json
import traceback

# Structured JSON logger (powertools).
# Lambda 환경: lambda_handler 의 부모 Logger 컨텍스트 자동 상속.
# ECS 환경: 부모 없이 단독 동작 (stdout JSON 출력).
from aws_lambda_powertools import Logger
logger = Logger(service="roster-solver", child=True)


# =========================================================
# 사용자 로딩 함수
# =========================================================
def load_current_user_by_nurse_id(
    db, nurse_id: str, override_group_id: str | None = None,
):
    """
    nurse_id로 간호사를 조회해 생성 엔진이 필요한 최소 UserSchema를 구성한다.

    인자:
        db: DB 세션.
        nurse_id: 간호사 ID.
        override_group_id: SQS payload 등에서 전달된 대상 그룹 ID.
            그룹 관리자가 타 그룹 근무표를 생성할 때, 관리자 본인의 group_id가
            아닌 실제 대상 그룹 ID를 사용해야 하므로 이 값이 우선한다.
    반환:
        UserSchema: 엔진 실행에 필요한 필드만 채운 사용자 정보.
    예외:
        RuntimeError: 대상 간호사가 없을 때.
    예시:
        nurse_id="438390" → office_id, group_id, is_head_nurse 포함한 스키마 반환.
    """
    from db.models import Nurse
    from schemas.auth_schema import User as UserSchema

    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    if not nurse:
        raise RuntimeError(f"해당 nurse_id에 대한 사용자 없음: nurse_id={nurse_id}")

    # override_group_id가 있으면 SQS payload의 대상 그룹을 우선 사용
    group_id = override_group_id or getattr(nurse, "group_id", None)
    # office_id 보강: Nurse.office_id가 없으면 group.office_id 사용
    office_id = getattr(nurse, "office_id", None) or (getattr(getattr(nurse, "group", None), "office_id", None))
    is_head_nurse = getattr(nurse, "is_head_nurse", False)
    name = getattr(nurse, "name", "")

    if override_group_id and override_group_id != getattr(nurse, "group_id", None):
        print(
            f"[worker] group_id override 적용: "
            f"nurse 원본={getattr(nurse, 'group_id', None)} → 대상={override_group_id}"
        )

    # 엔진에 불필요한 필드는 기본값으로 채워 ValidationError만 방지한다.
    return UserSchema(
        nurse_id=nurse.nurse_id,
        account_id=nurse.account_id,
        office_id=office_id,
        group_id=group_id,
        is_head_nurse=is_head_nurse,
        name=name,
        mb_part=getattr(nurse, "mb_part", "") or "",
        office_name=getattr(getattr(nurse, "group", None), "office_name", "") or getattr(nurse, "office_name", "") or "",
        mb_part_name=getattr(nurse, "mb_part_name", "") or "",
        gw_useYN=str(getattr(nurse, "gw_useYN", "") or "N"),
        qpis_useYN=str(getattr(nurse, "qpis_useYN", "") or "N"),
        official_title_name=getattr(nurse, "official_title_name", None),
    )

# =========================================================
# 핵심 워커 실행
# =========================================================
def process_job(payload: dict) -> dict:
    """SQS / JOB_JSON payload를 받아 솔버를 실행.

    ECS task와 Lambda 양쪽에서 공통으로 호출되는 진입점.

    인자:
        payload: {"job_id":..., "nurse_id":..., "group_id":..., "params":{...}}
    반환:
        성공 시 {"status": "success", "job_id":..., "result_id":...}
    예외:
        - ValueError: payload 자체 부적절 (nurse_id 없음 등). 재시도 무의미.
        - 그 외 Exception: 솔버 실행/DB 오류. DB STATUS_FAILED 기록 후 그대로 전파하여
          호출자(SQS event source mapping, ECS exit code)가 재시도/실패 처리를 결정하도록 한다.
          Caller는 exception swallow 금지 — transient 오류 시 메시지 영구 손실 방지.
    """
    # ↓↓↓ Lazy import: 무거운 모듈을 함수 내부로 이동하여 Lambda init phase 부하 절감.
    from sqlalchemy.orm import Session  # noqa: F401 — type hint 외에는 미사용
    from db.client2 import SessionLocal
    from db.models import RosterConfig
    from schemas.roster_schema import RosterRequest
    from services.roster_create_service import generate_roster_service
    from services.job_status_service import (
        STATUS_FAILED,
        STATUS_RUNNING,
        STATUS_SUCCESS,
        update_job_record,
    )

    job_id = payload.get("job_id")
    nurse_id = payload.get("nurse_id")
    job_group_id = payload.get("group_id")
    params = payload.get("params", {})

    if not nurse_id:
        raise ValueError("nurse_id 값이 필요합니다")

    try:
        req = RosterRequest(**params)
    except Exception as e:
        raise ValueError(f"RosterRequest 생성 오류: {e}") from e

    db: Session = SessionLocal()
    try:
        current_user = load_current_user_by_nurse_id(db, nurse_id, override_group_id=job_group_id)

        # office_id 를 logger context 에 추가 주입.
        # (lambda_handler 가 이미 job_id/group_id/nurse_id 주입한 상태)
        # 이후 모든 logger 호출(이 함수 + services 의 child logger 포함)에 자동 포함.
        logger.append_keys(office_id=current_user.office_id)
        logger.info(
            "작업 시작",
            extra={
                "year": getattr(req, "year", None),
                "month": getattr(req, "month", None),
                "config_id": getattr(req, "config_id", None),
            },
        )

        update_job_record(db, job_id, status=STATUS_RUNNING, progress=10)

        latest_config = None
        if getattr(req, "config_id", None):
            latest_config = db.query(RosterConfig).filter(RosterConfig.config_id == req.config_id).first()
            print('최종 config 정보', latest_config.__dict__ if latest_config else None)
        else:
            latest_config = (
                db.query(RosterConfig)
                .filter(RosterConfig.group_id == current_user.group_id)
                .order_by(RosterConfig.created_at.desc())
                .first()
            )
        if not latest_config:
            raise RuntimeError("RosterConfig가 존재하지 않습니다. 먼저 설정을 등록하거나 config_id를 전달하세요.")

        roster_data = generate_roster_service(req, current_user, db)
        result_id = None
        if isinstance(roster_data, dict):
            result_id = roster_data.get("schedule_id") or roster_data.get("schedule")
        update_job_record(
            db,
            job_id,
            status=STATUS_SUCCESS,
            progress=100,
            result_roster_id=result_id,
        )
        roster_nurses_count = len(roster_data.get("nurses", [])) if isinstance(roster_data, dict) else 0
        logger.info(
            "작업 완료",
            extra={
                "result_roster_id": result_id,
                "roster_nurses_count": roster_nurses_count,
            },
        )
        return {"status": "success", "job_id": job_id, "result_id": result_id}

    except Exception:
        import json as _json
        from fastapi import HTTPException as _HTTPException

        exc_obj = sys.exc_info()[1]
        # generate_roster_service 가 raise 한 구조화 infeasibility payload 가 있으면
        # error_message 에 JSON 으로 보존 → get_job_status 가 narrative 추출 가능.
        if isinstance(exc_obj, _HTTPException) and isinstance(exc_obj.detail, dict):
            try:
                err_msg = _json.dumps(exc_obj.detail, ensure_ascii=False)
            except (TypeError, ValueError):
                err_msg = str(exc_obj.detail)
        else:
            err_msg = str(exc_obj)
        # JSON structured log + traceback (powertools 가 stack_trace 자동 포함)
        logger.exception("작업 실패", extra={"error_message": err_msg})
        # stdout 에도 traceback 출력 (기존 호환)
        traceback.print_exc()
        try:
            db.rollback()
        except Exception as exc_rb:
            print(f"[worker] DB rollback 실패: {exc_rb}", file=sys.stderr)
        try:
            update_job_record(
                db,
                job_id,
                status=STATUS_FAILED,
                progress=100,
                error_message=err_msg,
            )
        except Exception as exc2:
            print(f"[worker] Job 상태 업데이트 실패(FAILED): {exc2} → 새 session 으로 재시도", file=sys.stderr)
            try:
                db_retry = SessionLocal()
                try:
                    update_job_record(
                        db_retry,
                        job_id,
                        status=STATUS_FAILED,
                        progress=100,
                        error_message=err_msg,
                    )
                finally:
                    db_retry.close()
            except Exception as exc3:
                print(f"[worker] Job 상태 업데이트 재시도 실패(FAILED): {exc3}", file=sys.stderr)
        # transient 오류(DB 일시 단절, 네트워크 등)도 영구 ack 되지 않도록 caller에 전파한다.
        raise

    finally:
        db.close()


def main():
    """ECS task 진입점: JOB_JSON 환경변수에서 payload 파싱 후 process_job 호출."""
    print("ENV KEYS:", os.environ.keys())
    print("JOB_JSON RAW:", os.getenv("JOB_JSON"))
    job_json = os.getenv("JOB_JSON")
    if not job_json:
        print("[worker] JOB_JSON 환경변수가 없습니다", file=sys.stderr)
        sys.exit(2)

    try:
        payload = json.loads(job_json)
    except json.JSONDecodeError:
        print("[worker] JOB_JSON JSON 파싱 실패", file=sys.stderr)
        sys.exit(2)

    try:
        process_job(payload)
    except ValueError as e:
        # payload 자체 오류 (재시도 무의미)
        print(f"[worker] payload 오류: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        # 처리 실패 (transient 가능) → ECS는 exit(1) 반환, 상위 dispatcher 정책에 위임
        print(f"[worker] 처리 실패: {e}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
