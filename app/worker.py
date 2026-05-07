# app/worker.py
"""Roster solver worker — ECS task와 Lambda 양쪽 공통 진입점.

import 정책:
    - top-level은 표준 라이브러리만 (os, sys, json, traceback).
    - 무거운 의존성(sqlalchemy, db.*, schemas.*, services.*)은 함수 내부로 lazy import.
    - 목적: Lambda init phase 10초 timeout 회피 + ECS 호환 유지.
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
        print(f"[worker] 작업 시작 job_id={job_id}, nurse_id={nurse_id}, group_id={current_user.group_id}, req={req}")

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
        print(f"[worker] 작업 완료 job_id={job_id}; roster_nurses={len(roster_data.get('nurses', []))}")
        return {"status": "success", "job_id": job_id, "result_id": result_id}

    except Exception:
        traceback.print_exc()
        err_msg = str(sys.exc_info()[1])
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
