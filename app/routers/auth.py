import logging
import os
import random
import string
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Response, Cookie, Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from fastapi_mail import MessageType  # MessageType도 필요합니다.
from jose import JWTError, jwt
from sqlalchemy.orm import Session, joinedload
from starlette.status import HTTP_301_MOVED_PERMANENTLY

from datalayer.member import Member
# from db.client import get_db
from db.client2 import get_db, msdb_manager
from db.models import Nurse
from schemas.auth_schema import User as UserSchema, TokenData
from utils.email import email_sender, EmailSchema
from utils.security import create_login_token
from utils.utils import set_sms

logger = logging.getLogger(__name__)

# Configuration
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 1 day

router = APIRouter(
    prefix="/auth",
    tags=["auth"]
)

# This was trying to read from the header, but we are using httpOnly cookies
# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_user(db: Session, account_id: str):
    return db.query(Nurse).options(joinedload(Nurse.group)).filter(Nurse.account_id == account_id).first()

def mworks_get_user (account_id: str, password: str, client_ip: str) :
    rows = msdb_manager.fetch_all(Member.login_check(), params=(password, account_id))
    print('rows', rows)
    for row in rows :
        
       IsPWCorrect = row['IsPWCorrect']
       EmpSeqNo = row['EmpSeqNo']
       office_id = row['OfficeCode']
    LogType = 'W'
    RegDate = datetime.now()


    params = (account_id, RegDate, client_ip, EmpSeqNo, office_id, LogType)
    if not IsPWCorrect :
        raise HTTPException(status_code=500, detail=f"Login failed")

    new_id = msdb_manager.execute(Member.login_log(), params=params)

    if new_id is None:
        raise HTTPException(status_code=500, detail=f"Login failed")

    rows = msdb_manager.execute(Member.login_update(), params=str(EmpSeqNo))

    if rows is None:
        raise HTTPException(status_code=500, detail=f"Login failed")

    try :
        user_info = msdb_manager.fetch_all(Member.member_view(), params=(account_id))
        return user_info

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")

@router.post("/login")
async def login_for_access_token(
    request: Request,
    response: Response, 
    form_data: OAuth2PasswordRequestForm = Depends(), 
    db: Session = Depends(get_db)
):
    client_ip = request.client.host
    try:
        #user = get_user(db, form_data.username)
        users = mworks_get_user(form_data.username, form_data.password, client_ip)

        if not users:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect account ID",
                headers={"WWW-Authenticate": "Bearer"},
            )

        for row in users:
            office_id = row['office_id']
            nurse_id = row['nurse_id']
            account_id = row['account_id']
            EmpAuthGbn = row['EmpAuthGbn']
            name = row['name']
            # nurse_id = row['nurse_id']
            group_id = row['group_id']
            is_head_nurse = row['is_head_nurse']
        # ADM 여부는 EmpAuthGbn으로 판정
        is_master_admin = True if str(EmpAuthGbn).upper() == 'ADM' else False

        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_login_token(
            data={"office_id": office_id, "account_id": account_id, "EmpAuthGbn": EmpAuthGbn, "is_master_admin": is_master_admin,
                  "nurse_id": nurse_id, "group_id": group_id, "is_head_nurse": is_head_nurse, "name": name}, expires_delta=access_token_expires
        )

        response.set_cookie(
            key="access_token", 
            value=f"Bearer {access_token}", 
            httponly=True, 
            samesite="lax"
        )
        return {"message": "Login successful", "account_id": account_id}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")


@router.post("/logout")
async def logout(response: Response, redirectUrl: str | None = None):
    """
        redirectUrl이 있는 경우 처리하고 값이 없는 경우 결과값 반환
    """

    response.delete_cookie(key="access_token")
    if redirectUrl:
        return RedirectResponse(url=redirectUrl, status_code=HTTP_301_MOVED_PERMANENTLY)
    else:
        return {"message": "Logout successful"}


async def get_current_user_from_cookie(token: Optional[str] = Cookie(None, alias="access_token"), db: Session = Depends(get_db)):
    if token is None:
        return None

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        token = token.replace("Bearer ", "")
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        office_id: str = payload.get("office_id")
        EmpSeqNo: str = payload.get("EmpSeqNo")
        account_id: str = payload.get("account_id")
        EmpAuthGbn: str = payload.get("EmpAuthGbn")
        nurse_id: str = payload.get("nurse_id")
        group_id: str = payload.get("group_id")
        is_head_nurse: str = payload.get("is_head_nurse")
        name: str = payload.get("name")
        is_master_admin = payload.get("is_master_admin")
        if account_id is None:
            return None
        token_data = TokenData(account_id=account_id)
        print('token_data', group_id)
    except JWTError:
        return None # If token is invalid, treat as not logged in
    
    # user = get_user(db, token_data.account_id)
    # if user is None:
    #     return None
    
    # Manually construct UserSchema to avoid from_orm issues
    # return UserSchema(
    #     nurse_id=user.nurse_id,
    #     account_id=user.account_id,
    #     office_id=user.office_id,  # This should now work with eager loading
    #     group_id=user.group_id,
    #     is_head_nurse=user.is_head_nurse,
    #     name = user.name

    return UserSchema(
        nurse_id= nurse_id,
        account_id=account_id,
        office_id=office_id,  # This should now work with eager loading
        group_id=group_id,
        is_head_nurse=is_head_nurse,
        is_master_admin= (bool(is_master_admin) if is_master_admin is not None else (str(EmpAuthGbn).upper() == 'ADM')),
        name = name,
        # EmpSeqNo = EmpSeqNo,
        EmpAuthGbn = EmpAuthGbn
    )

@router.get("/me", response_model=UserSchema)
async def read_users_me(current_user: UserSchema = Depends(get_current_user_from_cookie)):
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    return current_user


@router.get("/find_id", summary="ID 찾기")
def get_id_find_form(request: Request):

    return templates.TemplateResponse("id_find.html", {"request": request})

@router.post("/find_id")
async def handle_find_id_request(
        # 필수 공통 필드
        auth_method: str = Form(...),
        EmployeeName: str = Form(...),

        # bio (생년월일 + 성별) 인증 필드 (선택적)
        DateOfBirth: str = Form(None),
        gender: str = Form(None),

        # phone 인증 필드 (선택적)
        PortableTel: str = Form(None),

        # email 인증 필드 (선택적)
        Email: str = Form(None),
):
    """
    아이디 찾기 폼 데이터를 받아 인증 방식에 따라 처리하고 결과를 반환합니다.
    """
    if not auth_method:
        return HTTPException(status_code=400, detail="유효한 인증 방식을 선택해 주세요.")

    if auth_method == "bio":
        params = (EmployeeName, DateOfBirth, gender)
    elif auth_method == "phone":
        params = (EmployeeName, PortableTel)
    elif auth_method == "email":
        params = (EmployeeName, Email)

    member_row = msdb_manager.fetch_all(Member.find_id(auth_method), params=params)

    if member_row:
        return {"result": "succeed", "memberid" :  member_row[0]['memberid'] }
    else:
        return {"result": "fail", "message" : "일치하는 회원정보가 없습니다." }

@router.get("/find_pw", summary="PW 찾기")
def get_pw_find_form(request: Request):

    return templates.TemplateResponse("pw_find.html", {"request": request})

@router.post("/find_pw")
async def handle_find_pw_request(
        background_tasks: BackgroundTasks,
        # 필수 공통 필드
        auth_method: str = Form(...),
        memberID: str = Form(...),
        EmployeeName: str = Form(...),

        # 휴대폰번호, 이메일 선택적
        receivenum: str = Form(None),
        email: str = Form(None),
):
    pw_chk_result = msdb_manager.fetch_all(Member.find_pw_chk(), params=(memberID, EmployeeName))

    if not pw_chk_result:
        return {"result": "fail", "message" : "일치하는 회원정보가 없습니다."}

    # 회원데이터
    phoneNum_chk = pw_chk_result[0]['PortableTel']
    email_chk = pw_chk_result[0]['Email']
    empseqno = pw_chk_result[0]['EmpSeqNo']
    user_pw_reset = pw_chk_result[0]['idx']

    # 8자리 랜덤 패스워드
    numbers = string.digits
    length = 8
    password_list = random.sample(numbers, length)
    new_password = "".join(password_list)

    if auth_method == 'phone':
        if receivenum != phoneNum_chk :
            return {"result": "fail", "message": "휴대폰번호가 일치하지 않습니다."}
    else:
        if email != email_chk :
            return {"result": "fail", "message": "email이 일치하지 않습니다."}

    # 패스워드 변경
    pwd_reset_result = msdb_manager.execute(Member.member_pwd_reset(), params=(new_password, new_password, mpseqno))

    if not pwd_reset_result or pwd_reset_result == 0:
        return {"result": "fail", "message": "오류가 발생하였습니다."}
    else:
        pwd_reset_history_result = msdb_manager.execute(Member.member_pwd_reset_history(user_pw_reset), params=empseqno)
        if not pwd_reset_history_result or pwd_reset_result == 0:
            return {"result": "fail", "message": "오류가 발생하였습니다."}

    if auth_method == 'phone':
        sendPhoneNumber = "0269593214"
        smsMessage = '[메디통] 임시패스워드는 ' + new_password + ' 입니다. 로그인 후 패스워드를 변경해 주세요'
        userPhoneNumber = receivenum.replace('-','')

        sms_result = set_sms(userPhoneNumber, sendPhoneNumber, smsMessage)

        if not sms_result or sms_result['result'] == 'fail':
            return {"result": "fail", "message": "오류가 발생하였습니다."}
    else:
        body_data = {
            "name": EmployeeName,
            "pwd": new_password,
            "message": "로그인 후 패스워드를 변경해 주세요"
        }
        email_object = EmailSchema(
            email=[email],
            body=body_data
        )

        try:
            subject = "[메디통] 임시 비밀번호를 전달 드립니다."
            html_body = email_sender.create_email_body(email_object.body)

            # 클래스 인스턴스의 메서드 호출
            email_sender.send_in_background(
                background_tasks,
                subject=subject,
                recipients=email_object.email,
                html_body=html_body,
                subtype=MessageType.html
            )
        except Exception as e:
            # SMTP 연결 오류 등이 발생하면 500 에러를 반환
            raise HTTPException(status_code=500, detail=f"Error processing email request: {e}")

    return {"result": "succeed", "message": "임시비밀번호 발급이 완료되었습니다. 로그인 후 패스워드를 변경해 주세요"}
