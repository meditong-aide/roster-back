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

def _log_final_roster_tail(roster_data: dict, job_id, result_id) -> None:
    """성공한 최종 배정표를 로그 맨 끝(작업 종료 직전)에 재출력한다.

    솔버 내부 출력은 폴백(team_min hard→soft 등) 중간 로그에 묻히므로, CloudWatch tail
    에서 스크롤만으로 바로 확인할 수 있도록 성공 결과를 한 번 더 마커로 감싸 찍는다.
    """
    try:
        nurses = roster_data.get("nurses") if isinstance(roster_data, dict) else None
        if not nurses:
            return
        print(f"[CP-SAT-Basic] ===== 최종 배정표 (FINAL ROSTER) job={job_id} sched={result_id} =====")
        for n in nurses:
            cells = n.get("schedule") or []
            if not any(c and c != "-" for c in cells):
                continue  # 배정 없는 행(전출자 등) 은 생략
            print(f"[CP-SAT-Basic] 배정표 {n.get('name')}({n.get('id')}): {' '.join(cells)}")
        print("[CP-SAT-Basic] ===== 최종 배정표 끝 (/FINAL ROSTER) =====")
    except Exception as exc:
        print(f"[CP-SAT-Basic] 최종 배정표 tail 출력 실패: {exc}")


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

    # --- 진단용: 유령 job(442171) 원점 추적 ---
    # SQS 메시지엔 최초 enqueue 시각(requested_at)이 그대로 보존되므로, 매 재전송마다
    # 이 로그가 원래 요청 시각 + 전체 컨텍스트를 남긴다. DB created_at 이 갱신돼도 원점 특정 가능.
    print(
        f"[worker][CallerTrace] job_id={job_id} nurse_id={nurse_id} "
        f"group_id={job_group_id} account_id={payload.get('account_id')} "
        f"office_id={payload.get('office_id')} requested_at={payload.get('requested_at')} "
        f"client_ip={payload.get('client_ip')} x_forwarded_for={payload.get('x_forwarded_for')!r} "
        f"user_agent={payload.get('user_agent')!r} "
        f"payload_keys={list(payload.keys())}"
    )

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

        # 해결책 재생성: 유저가 고른 옵션의 delta 를 이번 생성에만 override(DB 미변경) /
        # ontology 옵션은 treatment_ids 로. 일반 생성이면 둘 다 None → 기존 동작 동일.
        roster_data = generate_roster_service(
            req, current_user, db,
            config_override=getattr(req, "config_override", None) or None,
            treatment_ids=(getattr(req, "treatment_ids", None) or None),
        )
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

        # 품질 요약(하네스 스타일): shortage/하드위반/N블록/공정성(y축).
        # best-effort — 실패해도 job 완료 처리에 영향 없음.
        quality_lines: list[str] = []
        quality_metrics: dict = {}
        try:
            from utils.roster_quality import summarize
            _q = summarize(roster_data if isinstance(roster_data, dict) else {})
            quality_lines = _q.get("lines", [])
            quality_metrics = _q.get("metrics", {})
        except Exception as exc_q:
            print(f"[worker] 품질 요약 계산 실패(무시): {exc_q}", file=sys.stderr)

        # office_id → office_name 해석 (실패해도 무시).
        office_name = None
        try:
            from db.models import Office
            _off = db.query(Office.office_name).filter(Office.office_id == current_user.office_id).first()
            office_name = str(_off.office_name) if _off and _off.office_name else None
        except Exception as exc_o:
            print(f"[worker] office_name 조회 실패(무시): {exc_o}", file=sys.stderr)

        logger.info(
            "작업 완료",
            extra={
                "result_roster_id": result_id,
                "roster_nurses_count": roster_nurses_count,
                "quality": quality_metrics,
            },
        )
        # 최종 배정표를 로그 맨 끝에 재출력 — CloudWatch tail 에서 바로 보이도록.
        _log_final_roster_tail(roster_data, job_id, result_id)
        # 모니터링 알림(fire-and-forget): 전송 실패해도 job 처리에 영향 없음.
        from utils.slack_notify import notify_roster_result
        notify_roster_result(
            ok=True,
            job_id=job_id,
            group_id=current_user.group_id,
            office_id=current_user.office_id,
            office_name=office_name,
            nurse_id=nurse_id,
            year=getattr(req, "year", None),
            month=getattr(req, "month", None),
            result_roster_id=result_id,
            quality_lines=quality_lines,
        )
        return {"status": "success", "job_id": job_id, "result_id": result_id}

    except Exception:
        import json as _json
        from fastapi import HTTPException as _HTTPException

        exc_obj = sys.exc_info()[1]
        # generate_roster_service 가 raise 한 구조화 infeasibility payload 가 있으면
        # error_message 에 JSON 으로 보존 → get_job_status 가 narrative 추출 가능.
        _has_struct_detail = isinstance(exc_obj, _HTTPException) and isinstance(exc_obj.detail, dict)
        if _has_struct_detail:
            try:
                err_msg = _json.dumps(exc_obj.detail, ensure_ascii=False)
            except (TypeError, ValueError):
                err_msg = str(exc_obj.detail)
        else:
            err_msg = str(exc_obj)
        # precheck 산술 블로킹·UNRECOVERABLE 등 "결정론적 infeasibility"는 재시도해도
        # 반드시 동일하게 실패한다(transient 아님). 아래에서 ack(정상 반환)로 종료하여
        # SQS 좀비 재시도와 중복 Slack 알림을 원천 차단한다. ValueError 와 동일한 정책.
        is_deterministic_infeasible = _has_struct_detail and "infeasibility" in exc_obj.detail
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
        # 모니터링 알림(fire-and-forget). current_user 는 로드 실패 시 미정의일 수 있어 방어적 접근.
        _cu = locals().get("current_user")
        from utils.slack_notify import notify_roster_result
        notify_roster_result(
            ok=False,
            job_id=job_id,
            group_id=getattr(_cu, "group_id", None) or job_group_id,
            office_id=getattr(_cu, "office_id", None),
            nurse_id=nurse_id,
            year=getattr(req, "year", None),
            month=getattr(req, "month", None),
            error_message=err_msg,
        )
        # 결정론적 infeasibility 는 재시도 무의미 → ack(정상 반환)로 SQS 메시지 삭제.
        # DB 에는 이미 STATUS_FAILED + narrative 가 기록되어 프론트 조회에는 영향이 없다.
        if is_deterministic_infeasible:
            print(
                f"[worker] 결정론적 infeasibility → 재시도 안 함(ack). job_id={job_id}",
                file=sys.stderr,
            )
            return {"status": "failed", "job_id": job_id, "infeasible": True, "retriable": False}
        # 그 외(DB 일시 단절·네트워크 등 transient 가능)는 caller 에 전파하여
        # 영구 손실 없이 SQS 재시도되도록 한다.
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
