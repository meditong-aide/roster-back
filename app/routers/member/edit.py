from fastapi import APIRouter, Depends, Request, Form, HTTPException, BackgroundTasks
from fastapi.templating import Jinja2Templates

from datalayer.member import Member
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from datetime import datetime

# EmailSender 클래스 인스턴스를 import
from utils.email import email_sender, EmailSchema
from fastapi_mail import MessageType # MessageType도 필요합니다.
from starlette.responses import JSONResponse

from utils.utils import set_mlink_push, set_app_push, set_sms

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

    if not current_user.account_id:
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


# 링크 메세지 테스트
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

