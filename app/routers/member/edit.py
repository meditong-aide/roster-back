from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks
from fastapi import Request, File, UploadFile, Form, HTTPException, Depends
from fastapi.templating import Jinja2Templates
from fastapi_mail import MessageType  # MessageType도 필요합니다.
from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse

from datalayer.member import Member
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
# EmailSender 클래스 인스턴스를 import
from utils.email import email_sender, EmailSchema
from utils.utils import set_mlink_push, set_app_push, set_sms, save_uploaded_files, delete_files, download_file

router = APIRouter()

templates = Jinja2Templates(directory="templates")
DOWNLOAD_FOLDER = "downloads"

@router.get("/edit", summary="회원정보 수정")
def member_edit_view(request: Request, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    EmpAuthGbn = current_user.EmpAuthGbn
    account_id = current_user.account_id

    rows = msdb_manager.fetch_all(Member.member_view(), params=account_id)
    return templates.TemplateResponse("member_edit.html", {"request": request, "user_data" : rows[0]})


@router.post("/edit")
async def update_member_info(current_user: UserSchema = Depends(get_current_user_from_cookie),
        # 기본 정보 (읽기 전용 포함)
        EmpSeqNo: str = Form(...),
        account_id: str = Form(...),
        name: str = Form(...),
        CurMemberPass: str = Form(...),
        MemberPass: str = Form(...),
        MemberPassRe: str = Form(...),

        gender: str = Form(...),
        DateOfBirth: str = Form(None),
        JoinDate: str = Form(...),

        # 연락처 및 주소
        Tel: str = Form(None),
        PortableTel: str = Form(...),
        Email: str = Form(...),
        zipcode: str = Form(None),
        Address1: str = Form(None),
        Address2: str = Form(None),

        # 직무 및 조직 정보
        # 체크박스(스위치)는 체크되지 않으면 데이터가 전송되지 않으므로 기본값을 None으로 설정
        mb_part_managerYN: str = Form(None),
        mb_partName: str = Form(None),
        OfficialTitleName: str = Form(None),
        career: str = Form(None),
        duty: str = Form(None),
        is_head_nurse: str = Form(None),
        nightkeep: str = Form(None),
):
    OfficeCode = current_user.office_id

    if not current_user.EmpSeqNo:
       return HTTPException(status_code=500, detail=f"Internal Server Error")

    if not CurMemberPass:
        return HTTPException(status_code=400, detail=f"Internal Server Error")

    rows = msdb_manager.fetch_all(Member.login_check(), params=(CurMemberPass, account_id))
    if rows[0]['IsPWCorrect'] :
        if not DateOfBirth:
            DateOfBirth = datetime.strptime(DateOfBirth, '%Y-%m-%d').date()

        # 회원정보 수정
        param = (gender, DateOfBirth, JoinDate, Tel, PortableTel, Email, zipcode, Address1, Address2, EmpSeqNo)
        update_row = msdb_manager.execute(Member.member_update(), params=param)

        if update_row is not None:
            # Qpis 데이터 수정을 위한 컬럼 변환
            strgender = '1' if gender == 'Y' else '2'
            mem_birth = DateOfBirth.replace("-", "") + strgender

            # 패스워드 수정 시
            if MemberPass and MemberPass == MemberPassRe:
                chg_pwd_YN = 'Y'
                password_edit = msdb_manager.execute(Member.member_pwd_update(), params=(MemberPass, MemberPass, OfficeCode, EmpSeqNo))

                if password_edit is None:
                    return {"result": "password change failed"}
            else:
                chg_pwd_YN ='N'

            # Qpis 정보 수정
            if chg_pwd_YN == 'Y':
                qpis_params = (MemberPass, PortableTel, Tel, zipcode, Address1, Address2, name, mem_birth, Email, account_id)
            else:
                qpis_params = (PortableTel, Tel, zipcode, Address1, Address2, name, mem_birth, Email, account_id)

            qpis_update = msdb_manager.execute(Member.qpis_member_update(chg_pwd_YN), params=qpis_params)

            if qpis_update is not None:
                return {"result": "success"}
            else:
                return {"result": "update failed"}
        else:
            return {"result": "error"}
    else:
        return HTTPException(status_code=400, detail=f"Internal Server Error")

# Email 발송 테스트
@router.post("/send-email-background", status_code=202)
async def send_email_as_background(background_tasks: BackgroundTasks):
    """
    EmailSender 인스턴스의 send_in_background 메서드를 사용하여 메일 발송.
    """
    body_data = {
        "name": "홍길동",
        "message": "메일 테스트 성공"
    }
    email_object = EmailSchema(
        email=['dhsung@meditong.com'],
        body=body_data
    )

    try:
        subject = "FastAPI 클래스 기반 백그라운드 메일 테스트"
        html_body = email_sender.create_email_body(email_object.body)

        print("html_body : ", html_body)

        # 클래스 인스턴스의 메서드 호출
        email_sender.send_in_background(
            background_tasks,
            subject=subject,
            recipients=email_object.email,
            html_body=html_body,
            subtype=MessageType.html
        )

        return JSONResponse(
            status_code=202,
            content={"message": "Email sending has been initiated in the background."}
        )

    except Exception as e:
        # SMTP 연결 오류 등이 발생하면 500 에러를 반환
        raise HTTPException(status_code=500, detail=f"Error processing email request: {e}")

# 링크 메세지 테스트
@router.post("/send-mlink-message", status_code=202)
async def send_mlink_message():
    empseqno = '430461'
    officecode = '100723'
    str_to_arr = '430461' # ,로 구분해서 전송
    contents = "링크 메세지 테스트"

    message_result = set_mlink_push(empseqno, officecode, str_to_arr, contents)

    return message_result


# 푸시 메세지 테스트
@router.post("/send-push-message", status_code=202)
async def send_push_message():
    empseqno = '430461'
    officecode = '100723'
    str_to_arr = '430461' # ,로 구분해서 전송
    contents = "링크 메세지 테스트"

    pushCode = 'P30'
    pushSubCode = 'S01'
    officeCode = '100723'
    sendEmpSeqNo = '430461'
    sendMemberId = 'dhsung'
    receiveEmpSeqNo = '430461'
    pushMessage = "pushMessage 메세지 테스트"
    orgPushMessage = "orgPushMessage 메세지 테스트"
    linkUrl = "linkUrl"
    linkCode = "olinkCode"

    message_result = set_app_push(pushCode, pushSubCode, officeCode, sendEmpSeqNo, sendMemberId, receiveEmpSeqNo, pushMessage, orgPushMessage, linkUrl, linkCode)
    return message_result

# SMS 메세지 테스트
@router.post("/send-sms-message", status_code=202)
async def send_sms_message():
    sendPhoneNumber = "0269593214"
    userPhoneNumber = "01062496700"
    smsMessage = 'SMS 발송 테스트'

    sms_result = set_sms(userPhoneNumber, sendPhoneNumber, smsMessage)
    return sms_result

#첨부파일 테스트
@router.get("/file-upload", status_code=202)
async def get_upload_form(request: Request):
    """파일 업로드 폼을 렌더링"""
    return templates.TemplateResponse("upload_form.html", {"request": request})


# 파일 업로드를 처리하는 POST 라우터
@router.post("/file-upload", response_class=HTMLResponse)
async def handle_file_upload(
        request: Request,
        current_user: UserSchema = Depends(get_current_user_from_cookie),
        files: Optional[List[UploadFile]] = File(None)
):
    """
    업로드된 파일을 save_uploaded_files 함수를 이용하여 처리
    """
    officecode = current_user.office_id
    GLOBAL_UPLOAD_ROOT = 'downloads/' + officecode + '/board'
    use_size_limit = 100*1024*1024 #100MB

    uploaded_files = []
    storage_path = ""

    # 폼에서 파일이 첨부되었는지 확인
    if files and files[0].filename:
        try:
            # 파일 저장
            storage_path, uploaded_files = await save_uploaded_files(
                files=files,
                root_upload_dir=GLOBAL_UPLOAD_ROOT,
                file_type='all',
                use_size_limit=use_size_limit
            )

            message = f"파일 {len(uploaded_files)}개 업로드 성공!"
            error = None

        except HTTPException as e:
            # 파일 처리 중 발생한 HTTPException 반환
            message = None
            error = e.detail
            uploaded_files = None
            storage_path = None
        except Exception as e:
            # 기타 예상치 못한 오류
            message = None
            error = f"파일 처리 중 예상치 못한 오류 발생: {e}"
            uploaded_files = None
            storage_path = None
    else:
        # 파일이 첨부되지 않았을 경우
        message = f"첨부 파일 없음."
        error = None

    # #파일 삭제
    # deletion_result = delete_files(
    #     file_names=["easysetting_division (1)_1761790614728.xls"],
    #     root_upload_dir=GLOBAL_UPLOAD_ROOT
    # )

    # 결과를 템플릿에 전달하여 폼과 함께 표시 파일업로드, 삭제에서 활용
    return templates.TemplateResponse(
        "upload_form.html",
        {
            "request": request,
            "message": message,
            "error": error,
            "uploaded_files": uploaded_files,
            "storage_path": storage_path
        }
    )

    # 파일 다운로드 테스트를 위해서 처리
    # return download_file(
    #     stored_file_name="easysetting_position_1761790759835.xls",
    #     root_upload_dir=GLOBAL_UPLOAD_ROOT,
    #     download_as="original"
    # )