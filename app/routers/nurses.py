from fastapi import APIRouter, Depends, HTTPException
from fastapi import UploadFile, File, Query
from fastapi.responses import FileResponse, Response, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import List, Optional, Dict
import uuid
import tempfile
import os
import random
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
import traceback
import string

from db.client2 import get_db, msdb_manager
from db.models import Nurse as NurseModel
from db.models import Office as OfficeModel
from db.models import Team as TeamModel
from schemas.roster_schema import (
    NurseProfile, 
    MoveNurseRequest, 
    IntegratedRegisterRequest, 
    ExcelValidationRequest, 
    NurseSequenceUpdate, 
    ReorderPayload, 
    ExcelConfirmRequest,
    PersonnelUpdate,
    PasswordChangeRequest,
    PhoneChangeRequest
)
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.nurse_service import (
    get_nurses_in_group_service,
    bulk_update_nurses_service,
    move_nurse_service,
    move_nurse_with_active_service,
    reorder_nurses_service,
    get_nurses_filtered_service,
    get_personnel_basic_info_service,
)
from services.excel_service import (
    create_nurse_template, 
    process_excel_upload, 
    validate_excel_data,
    save_excel_data,
    create_groups_and_save_data,
    create_nurse_template2,
    # process_excel_upload2,
    upload2_validate,
    upload2_confirm,
    export_members_excel_bytes,
    integrated_member_and_nurse_register,
)
from datalayer.member import Member
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse
from utils.utils import set_sms


router = APIRouter(
    prefix="/nurses",
    tags=["nurses"]
)


def _ensure_office_exists(db: Session, office_id: Optional[str], office_name: Optional[str]) -> None:
    """
    오피스 ID가 있는데 DB `offices` 테이블에 레코드가 없으면 생성합니다.

    - 인자:
        - db(Session): SQLAlchemy 세션
        - office_id(Optional[str]): 오피스 ID
        - office_name(Optional[str]): 오피스명(미입력 시 생성하지 않음)
    - 반환: 없음
    - 예외:
        - sqlalchemy.exc.IntegrityError: 동시 생성 레이스 컨디션 등으로 중복 삽입이 발생한 경우
    - 예시:
        - office_id="A1", office_name="서울병원" → offices에 (A1, 서울병원) 레코드가 없으면 생성
    """
    print('여기다', office_id, office_name)
    if not office_id:
        return
    if not office_name:
        return

    exists = (
        db.query(OfficeModel.office_id)
        .filter(OfficeModel.office_id == office_id)
        .first()
    )
    if exists:
        return

    try:
        db.add(OfficeModel(office_id=office_id, office_name=office_name))
        db.commit()
    except IntegrityError:
        # 동시 요청 등으로 이미 생성된 경우를 대비
        db.rollback()


@router.get("", response_model=List[NurseProfile])
async def get_nurses_in_group(
    office_id: Optional[str] = None,
    group_id: Optional[str] = None,
    nurse_id: Optional[str] = None, # 신규 파라미터
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):

    _ensure_office_exists(db, getattr(current_user, "office_id", None), getattr(current_user, "office_name", None))
    print('current_user', current_user.nurse_id, current_user.group_id, current_user.office_id)
    try:
        # ADM는 필터링 옵션 허용, 일반/수간호사는 자신의 그룹만
        if current_user.is_master_admin:
            response = get_nurses_filtered_service(
                current_user, 
                db, 
                office_id=office_id, 
                group_id=group_id, 
                nurse_id=nurse_id   # nurse_id 전달
            )
            return response
        return get_nurses_in_group_service(
            current_user, 
            db,
            nurse_id=nurse_id   # nurse_id 전달
        )
    except Exception as e:
        print('[DEBUG] [nurses.py - get_nurses_in_group] office_id', office_id)
        print('[DEBUG] [nurses.py - get_nurses_in_group] group_id', group_id)
        print('[DEBUG] [nurses.py - get_nurses_in_group] nurse_id', nurse_id)
        print('[DEBUG] [nurses.py - get_nurses_in_group] current_user', current_user.__dict__)
        print('[DEBUG] [nurses.py - get_nurses_in_group] error', e)
        # raise HTTPException(status_code=500, detail=f"간호사 목록 조회 실패: {str(e)}")

# @router.get("/personnel-basic-info")
# async def get_personnel_basic_info(
#     current_user: UserSchema = Depends(get_current_user_from_cookie),
#     db: Session = Depends(get_db)
# ):
#     try:
#         return get_personnel_basic_info_service(current_user, db)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"간호사 기본 정보 조회 실패: {str(e)}")



@router.post("/sequence/save")
async def save_nurse_sequence(
    req: NurseSequenceUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    단일 간호사 이동/상태변경 (드래그앤드롭 중간 저장 용도)
    """
    try:
        return move_nurse_with_active_service(req.nurse_id, req.new_sequence, req.active, current_user, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"간호사 순서 변경 실패: {str(e)}")

@router.post("/sequence/reorder")
async def reorder_nurses(
    payload: ReorderPayload,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    드래그앤드롭 종료 시점에 한 번 호출하여 서버 기준으로 순서를 확정.
    프론트에서는 active 리스트와 inactive 리스트의 nurse_id 배열을 넘겨주세요.
    """
    try:
        return reorder_nurses_service(payload.active_order, payload.inactive_order, current_user, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"일괄 재정렬 실패: {str(e)}")


@router.post("/bulk-update")
async def bulk_update_nurses(
    nurses_data: List[NurseProfile],
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:

        # ADM이 group_id를 지정하면 해당 병동을 대상으로 업데이트 허용
        return bulk_update_nurses_service(nurses_data, current_user, db, override_group_id=group_id)
    except Exception as e:
        print('error1', e)
        raise HTTPException(status_code=500, detail=f"간호사 일괄 업데이트 실패: {str(e)}") 

@router.get("/template-download")
async def download_template(
    current_user: UserSchema = Depends(get_current_user_from_cookie)
):
    """엑셀 템플릿 파일 다운로드"""
    try:
        if not current_user or not current_user.is_head_nurse:
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        template_path = create_nurse_template()
        return FileResponse(
            path=template_path,
            filename="간호사_정보_템플릿.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"템플릿 생성 실패: {str(e)}")

@router.get("/template2-download")
async def download_template2(
    current_user: UserSchema = Depends(get_current_user_from_cookie)
):
    """신규 엑셀 템플릿2 (계정ID/이름만) 다운로드 - ADM 전용"""
    try:
        # if not current_user or not current_user.is_master_admin:
        #     raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")
        template_path = create_nurse_template2()
        return FileResponse(
            path=template_path,
            filename="간호사_업로드2_템플릿.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        print('error', e)
        raise HTTPException(status_code=500, detail=f"템플릿2 생성 실패: {str(e)}")

@router.post("/upload-excel")
async def upload_excel(
    file: UploadFile = File(...),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """엑셀 파일 업로드 및 검증"""
    try:
        if not current_user or not current_user.is_head_nurse:
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        if file.size and file.size > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="파일 크기는 10MB를 초과할 수 없습니다.")
        if not file.filename.lower().endswith(('.xlsx', '.xls')):
            raise HTTPException(status_code=400, detail="지원되지 않는 파일 형식입니다. (.xlsx, .xls만 지원)")
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name
        try:
            result = process_excel_upload(tmp_file_path, current_user, db)
            return result
        finally:
            os.unlink(tmp_file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"엑셀 업로드 실패: {str(e)}")

class Upload2ConfirmRequest(BaseModel):
    rows: List[dict]
    group_id: Optional[str] = None


@router.post("/upload2-validate")
async def upload2_validate_endpoint(
    file: UploadFile = File(...),
    group_id: str = Query(..., description="병동 그룹 ID (필수)"), # 추가
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """업로드2 - 검증 전용. 오류 목록과 정규화된 행을 반환한다."""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name
        try:
            result = upload2_validate(tmp_file_path, current_user, db, group_id=group_id)
            return result
        finally:
            os.unlink(tmp_file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"검증 실패: {str(e)}")


# @router.post("/upload2-confirm")
# async def upload2_confirm_endpoint(
#     payload: Upload2ConfirmRequest,
#     current_user: UserSchema = Depends(get_current_user_from_cookie),
#     db: Session = Depends(get_db)
# ):
#     """업로드2 - 검증 통과 후 저장. 오류가 있는 행은 건너뜀."""
#     try:
#         # if not current_user or not current_user.is_master_admin:
#         #     raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")
#         target_group_id = payload.group_id or current_user.group_id
#         result = upload2_confirm(payload.rows, current_user, db, target_group_id)
#         return result
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"저장 실패: {str(e)}")


@router.post("/upload2-confirm")
async def upload2_confirm_endpoint(
    payload: Upload2ConfirmRequest,
    group_id: str = Query(..., description="대상 병동 group_id (필수)"),  # ← 반드시 URL에 ?group_id=... 포함
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """업로드2 - 검증 통과 후 저장. 오류가 있는 행은 건너뜀."""
    try:
        print(f"[DEBUG] 라우터 - 받은 쿼리 group_id: {group_id}")
        print(f"[DEBUG] current_user: nurse_id={current_user.nurse_id}, "
              f"group_id={getattr(current_user, 'group_id', '없음')}, "
              f"is_master_admin={current_user.is_master_admin}")

        # 1. 쿼리 파라미터가 있으면 무조건 그걸 우선 사용 (가장 안전)
        target_group_id = group_id

        # 2. 만약 쿼리가 비어있으면 (미래 안전장치) current_user fallback
        if not target_group_id:
            target_group_id = getattr(current_user, 'group_id', None)

        # 3. 그래도 없으면 → 에러
        if not target_group_id:
            raise HTTPException(
                status_code=400,
                detail="group_id가 필요합니다. URL에 ?group_id=... 를 반드시 포함해주세요."
            )

        # ADM인 경우: group_id가 쿼리로 왔는지 로그로 확인 (디버깅용)
        if current_user.is_master_admin:
            print(f"[ADM 모드] 관리자 업로드 - 선택된 병동 group_id: {target_group_id}")

        result = upload2_confirm(payload.rows, current_user, db, target_group_id)
        return result

    except Exception as e:
        print(f"[ERROR] upload2-confirm 엔드포인트 실패: {str(e)}")
        raise HTTPException(status_code=500, detail=f"저장 실패: {str(e)}")


@router.get("/export-members")
async def export_members(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
):
    """ADM 전용: 현재 오피스 전체 인원 정보를 엑셀로 내려받기."""
    try:
        if not current_user or not current_user.is_master_admin:
            raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")

        print(f"[DEBUG] current_user={current_user}")
        print(f"[DEBUG] current_user.office_id={getattr(current_user, 'office_id', None)}")

        content = export_members_excel_bytes(current_user.office_id)
        print(f"[DEBUG] export result type={type(content)} size={len(content) if content else 0}")

        filename = f"구성원_목록_{current_user.office_id}.xlsx"
        import urllib.parse
        encoded_filename = urllib.parse.quote(filename)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
            },
        )

    except Exception as e:
        print("[ERROR] export_members_excel error:", e)
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"detail": str(e)})

@router.post("/validate-excel")
async def validate_excel_data_endpoint(
    request: ExcelValidationRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """업로드된 데이터 유효성 검증"""
    try:
        if not current_user or not current_user.is_head_nurse:
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        result = validate_excel_data(request.data, current_user, db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 검증 실패: {str(e)}")

@router.post("/confirm-upload")
async def confirm_upload(
    request: ExcelConfirmRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """검증된 데이터 최종 저장"""
    try:
        if not current_user or not current_user.is_head_nurse:
            raise HTTPException(status_code=403, detail="수간호사만 접근 가능합니다.")
        filtered_data = [
            data for i, data in enumerate(request.data) 
            if i < len(request.include_rows) and request.include_rows[i]
        ]
        if request.new_groups_to_create:
            result = create_groups_and_save_data(filtered_data, request.new_groups_to_create, current_user, db)
        else:
            result = save_excel_data(filtered_data, current_user, db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 저장 실패: {str(e)}") 


@router.post("/integrated-register")
async def integrated_register(
    payload: IntegratedRegisterRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    직접 입력으로 신규 직원 계정 생성 + 근무자 등록 (통합)
    """
    try:
        if not current_user.is_master_admin:
            raise HTTPException(status_code=403, detail="관리자 권한 필요")

        result = integrated_member_and_nurse_register(
            payload.members,
            current_user,
            db,
            payload.group_id  # 프론트에서 반드시 보내야 함
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"통합 등록 실패: {str(e)}")




# 마이 페이지 테스트
# 인증번호 임시 저장소 (프로덕션에서는 Redis로 교체 권장)
verification_cache: Dict[str, Dict] = {}  # {nurse_id: {'code': str, 'expires': datetime, 'new_phone': str or None, 'new_password': str or None}}


@router.get("/personnel-basic-info")
async def get_personnel_basic_info(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    try:
        nurse = db.query(NurseModel).filter(
            NurseModel.group_id == current_user.group_id,
            NurseModel.nurse_id == current_user.nurse_id
        ).first()
        
        if not nurse:
            raise HTTPException(status_code=404, detail="간호사 정보를 찾을 수 없습니다.")
        
        # 재직기간 계산
        tenure_display = "미등록"
        if nurse.joining_date:
            today = date.today()
            join_date = nurse.joining_date.date() if hasattr(nurse.joining_date, 'date') else nurse.joining_date
            if join_date <= today:
                delta = relativedelta(today, join_date)
                tenure_display = f"{delta.years}년 {delta.months}개월"
        
        age = None
        if nurse.birth_date:
            try:
                # birth_date가 "YYYY-MM-DD" 형식 문자열이라고 가정
                birth_date = datetime.strptime(nurse.birth_date, "%Y-%m-%d").date()
                today = date.today()
                age = today.year - birth_date.year
                if (today.month, today.day) < (birth_date.month, birth_date.day):
                    age -= 1
                # 음수 방지 (미래 생일 등)
                age = max(0, age)
            except ValueError:
                # 날짜 형식이 잘못된 경우 무시
                age = None
        
        work_place = "미등록"
        if nurse.group_id and nurse.office_id:
            team = db.query(TeamModel).filter(
                TeamModel.group_id == nurse.group_id,
                TeamModel.office_id == nurse.office_id
            ).first()
            
            if team and team.team_name:
                work_place = team.team_name
        
        # Member 테이블에서 이메일 + PortableTel 보강
        member_raw = msdb_manager.fetch_one(
            Member.member_view(),
            params=(current_user.account_id,)
        )
        
        member_rows = msdb_manager.fetch_all(
            Member.member_view(),
            params=(current_user.account_id,)
        )
        
        email = ""
        member_phone = ""
        
        if member_rows and len(member_rows) > 0:
            # fetch_all 결과가 리스트라고 가정
            first_row = member_rows[0]
            
            if isinstance(first_row, dict):
                # 딕셔너리면 안전하게 get
                email = first_row.get('Email', '')
                member_phone = first_row.get('PortableTel', '') or first_row.get('Tel', '')
            elif isinstance(first_row, tuple):
                # 튜플이면 인덱스로 매핑 (member_view 쿼리 순서 기준)
                if len(first_row) > 11:
                    email = first_row[11] or ''          # Email (인덱스 11)
                if len(first_row) > 8:
                    portable_tel = first_row[8] or ''    # PortableTel (인덱스 8)
                    tel = first_row[7] or ''             # Tel (인덱스 7)
                    member_phone = portable_tel or tel
            elif isinstance(first_row, str):
                # 문자열이면 (현재 상황처럼) office_id만 온 것으로 간주
                print(f"[WARNING] fetch_all 첫 행이 문자열: {first_row}")
                # 이 경우 추가 쿼리 필요하거나 email 빈 값 유지
            else:
                print(f"[ERROR] 예상치 못한 fetch_all 행 타입: {type(first_row)}")
        else:
            print("[WARNING] member_view 결과 없음")
        
        return {
            "nurse_id": nurse.nurse_id,
            "name": nurse.name,
            "account_id": nurse.account_id,
            "emp_num": nurse.emp_num,
            "gender": nurse.gender,
            "birth_date": nurse.birth_date,
            "age": age,
            "joining_date": nurse.joining_date.isoformat() if nurse.joining_date else None,
            "experience": nurse.experience,
            "phone_number": nurse.phone_number or member_phone,
            "email": email,
            "role": nurse.role,
            "level_": nurse.level_,
            "is_head_nurse": nurse.is_head_nurse,
            "tenure": tenure_display,
            "work_place": work_place
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"간호사 기본 정보 조회 실패: {str(e)}")


@router.patch("/personnel-basic-info")
async def partial_update_personnel_basic_info(
    update_data: PersonnelUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    nurse = db.query(NurseModel).filter(
        NurseModel.nurse_id == current_user.nurse_id
    ).first()

    if not nurse:
        raise HTTPException(404, "간호사 정보 없음")

    updated = False

    if update_data.experience is not None:
        nurse.experience = update_data.experience
        updated = True

    if updated:
        db.commit()
        db.refresh(nurse)

    if update_data.email is not None:
        msdb_manager.execute(
            "UPDATE bizwiz20db.Member SET Email = %s WHERE EmpSeqNo = %s",
            (update_data.email, current_user.nurse_id)
        )

    return {"message": "정보가 성공적으로 수정되었습니다"}


@router.put("/change-password")
async def change_password(
    payload: PasswordChangeRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    nurse_id = current_user.nurse_id
    print(f"[DEBUG] change-password 요청 - nurse_id: {nurse_id}")
    print(f"[DEBUG] payload: {payload.dict()}")

    # 인증번호가 없으면 → 발송 단계
    if not payload.verification_code:
        print("[DEBUG] 인증번호 발송 단계 진입")
        code = ''.join(random.choices(string.digits, k=6))
        print(f"[DEBUG] 생성된 인증번호: {code}")

        verification_cache[nurse_id] = {
            'code': code,
            'expires': datetime.now() + timedelta(minutes=3),
            'new_password': payload.new_password,
            'confirm_password': payload.confirm_password
        }
        print(f"[DEBUG] 캐시 저장 완료: {verification_cache.get(nurse_id)}")

        sendPhoneNumber = "0269593214"

        # nurses 테이블에서 phone_number 가져오기
        nurse = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
        
        if not nurse or not nurse.phone_number:
            raise HTTPException(
                status_code=400,
                detail="등록된 휴대폰 번호가 없습니다. 먼저 프로필에서 연락처를 등록해주세요."
            )
        
        userPhoneNumber = nurse.phone_number.replace('-', '')  # ← 핵심 수정
        print(f"[DEBUG] userPhoneNumber: {userPhoneNumber}")

        smsMessage = f'[메디통] 비밀번호 변경 인증번호: {code} (3분 이내 입력)'
        print(f"[DEBUG] SMS 발송 시도 - 번호: {userPhoneNumber}, 메시지: {smsMessage}")

        from utils.utils import set_sms
        sms_result = set_sms(userPhoneNumber, sendPhoneNumber, smsMessage)
        print(f"[DEBUG] set_sms 결과: {sms_result}")

        if sms_result.get('result') == 'fail':
            raise HTTPException(500, "인증번호 발송 실패")

        return {"message": "인증번호가 휴대폰으로 발송되었습니다"}

    # 인증번호 검증 단계
    print("[DEBUG] 인증번호 검증 단계 진입")
    cached = verification_cache.get(nurse_id)
    print(f"[DEBUG] 캐시 데이터: {cached}")

    if not cached:
        raise HTTPException(400, "인증 정보가 없거나 만료되었습니다")

    if datetime.now() > cached['expires']:
        del verification_cache[nurse_id]
        raise HTTPException(410, "인증 시간이 초과되었습니다")

    if payload.verification_code != cached['code']:
        raise HTTPException(400, "인증번호가 올바르지 않습니다")

    print("[DEBUG] 인증번호 일치 확인 완료")

    if payload.new_password != payload.confirm_password:
        raise HTTPException(400, "새 비밀번호와 확인 비밀번호가 일치하지 않습니다")

    if payload.new_password == payload.current_password:
        raise HTTPException(400, "기존 비밀번호와 동일한 비밀번호는 사용할 수 없습니다")

    print("[DEBUG] 기존 비밀번호 검증 시작")
    result = msdb_manager.fetch_one(
        "SELECT pwdcompare(%s, MemberPassEncrypt) AS IsCorrect FROM bizwiz20db.Member_Login WHERE EmpSeqNo = %s",
        (payload.current_password, nurse_id)
    )
    print(f"[DEBUG] pwdcompare 결과: {result}")

    if not result or result != 1:
        raise HTTPException(401, "기존 비밀번호가 올바르지 않습니다")

    print("[DEBUG] 비밀번호 업데이트 시작")
    update_result = msdb_manager.execute(
        Member.member_pwd_update(),
        (payload.new_password, payload.new_password, current_user.office_id, nurse_id)
    )
    print(f"[DEBUG] 업데이트 결과: {update_result}")

    del verification_cache[nurse_id]

    return {"message": "비밀번호가 성공적으로 변경되었습니다"}


@router.post("/change-phone/send-code")
async def send_phone_verification_code(
    payload: PhoneChangeRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    nurse_id = current_user.nurse_id
    code = ''.join(random.choices(string.digits, k=6))

    verification_cache[nurse_id] = {
        'code': code,
        'expires': datetime.now() + timedelta(minutes=3),
        'new_phone': payload.new_phone_number
    }

    sendPhoneNumber = "0269593214"

    # nurses 테이블에서 phone_number 가져오기
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
    
    if not nurse or not nurse.phone_number:
        raise HTTPException(
            status_code=400,
            detail="등록된 휴대폰 번호가 없습니다. 먼저 프로필에서 연락처를 등록해주세요."
        )
    
    userPhoneNumber = nurse.phone_number.replace('-', '')  # ← 핵심: 문자열 필드만 사용

    smsMessage = f'[메디통] 휴대폰 번호 변경 인증번호: {code} (3분 이내 입력)'

    from utils.utils import set_sms
    sms_result = set_sms(userPhoneNumber, sendPhoneNumber, smsMessage)

    if sms_result.get('result') == 'fail':
        raise HTTPException(500, "인증번호 발송에 실패했습니다")

    return {"message": "인증번호가 발송되었습니다"}


@router.put("/change-phone/verify")
async def verify_and_update_phone(
    payload: PhoneChangeRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    nurse_id = current_user.nurse_id
    cached = verification_cache.get(nurse_id)

    if not cached:
        raise HTTPException(400, "인증 정보가 없거나 만료되었습니다")

    if datetime.now() > cached['expires']:
        del verification_cache[nurse_id]
        raise HTTPException(410, "인증 시간이 초과되었습니다")

    if payload.verification_code != cached['code']:
        raise HTTPException(400, "인증번호가 올바르지 않습니다")

    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
    if nurse:
        nurse.phone_number = cached['new_phone']
        db.commit()

    msdb_manager.execute(
        "UPDATE bizwiz20db.Member SET PortableTel = %s WHERE EmpSeqNo = %s",
        (cached['new_phone'], nurse_id)
    )

    del verification_cache[nurse_id]

    return {"message": "휴대폰 번호가 성공적으로 변경되었습니다"}