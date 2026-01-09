# app/worker.py
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
from sqlalchemy.orm import Session

# app/ 디렉토리가 sys.path에 있으므로 db, schemas 직접 import 가능
from db.client2 import SessionLocal
from db.models import Nurse, RosterConfig
from schemas.roster_schema import RosterRequest
from schemas.auth_schema import User as UserSchema
from services.roster_create_service import generate_roster_service

# =========================================================
# 사용자 로딩 함수
# =========================================================
def load_current_user_by_nurse_id(db: Session, nurse_id: str) -> UserSchema:
    """
    nurse_id로 간호사를 조회해 생성 엔진이 필요한 최소 UserSchema를 구성한다.

    인자:
        db: DB 세션.
        nurse_id: 간호사 ID.
    반환:
        UserSchema: 엔진 실행에 필요한 필드만 채운 사용자 정보.
    예외:
        RuntimeError: 대상 간호사가 없을 때.
    예시:
        nurse_id="438390" → office_id, group_id, is_head_nurse 포함한 스키마 반환.
    """
    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    if not nurse:
        raise RuntimeError(f"해당 nurse_id에 대한 사용자 없음: nurse_id={nurse_id}")

    # office_id 보강: Nurse.office_id가 없으면 group.office_id 사용
    group_id = getattr(nurse, "group_id", None)
    office_id = getattr(nurse, "office_id", None) or (getattr(getattr(nurse, "group", None), "office_id", None))
    is_head_nurse = getattr(nurse, "is_head_nurse", False)
    name = getattr(nurse, "name", "")

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
def main():
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

    job_id   = payload.get("job_id")
    nurse_id  = payload.get("nurse_id")  # ⬅ account_id 로 사용한다고 가정
    params   = payload.get("params", {})

    if not nurse_id:
        print("[worker] nurse_id 값이 필요합니다", file=sys.stderr)
        sys.exit(2)

    # params로 RosterRequest 구성 (모델 필드: year, month, config_id, preceptor_gauge 등)
    try:
        req = RosterRequest(**params)
    except Exception as e:
        print(f"[worker] RosterRequest 생성 오류: {e}", file=sys.stderr)
        sys.exit(2)

    db: Session = SessionLocal()
    try:
        current_user = load_current_user_by_nurse_id(db, nurse_id)
        print(f"[worker] 작업 시작 job_id={job_id}, nurse_id={nurse_id}, req={req}")

        # 사전 검사: 설정 존재 여부 확인 (없으면 서비스 내부에서 충돌 가능)
        latest_config = None
        if getattr(req, "config_id", None):
            latest_config = db.query(RosterConfig).filter(RosterConfig.config_id == req.config_id).first()
            print('최종 config 정보', latest_config.__dict__)
        else:
            # 간단한 최신 설정 조회 (group 기준)
            latest_config = (
                db.query(RosterConfig)
                .filter(RosterConfig.group_id == current_user.group_id)
                .order_by(RosterConfig.created_at.desc())
                .first()
            )
        if not latest_config:
            print("[worker] RosterConfig가 존재하지 않습니다. 먼저 설정을 등록하거나 config_id를 전달하세요.", file=sys.stderr)
            sys.exit(2)

        # 핵심: 엔드포인트가 하던 것을 그대로 서비스로 호출
        roster_data = generate_roster_service(req, current_user, db)

        # 필요하면 jobs 테이블에 DONE/요약 기록 (선택)
        # update_job_status(db, job_id, "DONE", meta=...)

        print(f"[worker] 작업 완료 job_id={job_id}; roster_nurses={len(roster_data.get('nurses', []))}")
        sys.exit(0)

    except Exception:
        traceback.print_exc()
        # update_job_status(db, job_id, "FAILED")  # 선택
        sys.exit(1)

    finally:
        db.close()


if __name__ == "__main__":
    main()
