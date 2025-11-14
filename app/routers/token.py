import os
from datetime import date
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, status, Response, Request, Form, Depends
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_301_MOVED_PERMANENTLY
from datalayer.member import Member
from datalayer.token import Token
# from db.client import get_db
from db.client2 import get_db, msdb_manager
from db.models import Nurse
from schemas.auth_schema import User as UserSchema
from utils.security import create_access_token
from utils.security import create_login_token

ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 1 day

# Configuration
router = APIRouter(
    prefix="/token",
    tags=["token"]
)

def get_extra_data_from_nurses(db: Session, account_id: str) -> dict:
    """간단 조회: nurses 테이블에서 토큰에 보강할 필드를 가져온다.

    인자
    - account_id: 조회할 계정 ID

    반환
    - {'office_id', 'account_id', 'is_head_nurse', 'group_id'} 중 존재하는 값만 담은 dict
    """
    try:
        nurse = db.query(Nurse).filter(Nurse.account_id == account_id).first()
        if not nurse:
            return {}
        return {
            "office_id": getattr(nurse, "office_id", None),
            "account_id": getattr(nurse, "account_id", None),
            "is_head_nurse": bool(getattr(nurse, "is_head_nurse", False)),
            "group_id": getattr(nurse, "group_id", None),
        }
    except Exception:
        return {}

@router.post("/", summary="Token 생성")
async def get_token(response: Response,
                    clientId: str = Form(...),
                    clientSecret: str = Form(...)):
    """
        토큰은 1일 단위로 생성되며, 중복 호출 시 DB에 저장된 값으로 반환함.
    """
    _clientId = os.getenv("CLIENT_ID")
    _clientSecret = os.getenv("CLIENT_SECRET")

    if clientId == _clientId and clientSecret == _clientSecret :
        token = create_access_token(data={"clientSecret": clientSecret, "clientId": clientId})

    else :
        raise HTTPException(status_code=401, detail=f"Invalid client ID or secret provided.")
    return {"token" : token}

@router.post("/login", summary="Token, 회원아이디로 sso")
async def login_for_access_token(response: Response,
                                 request: Request,
                                 token: str = Form(...),
                                 MemberID: str = Form(...),
                                 db: Session = Depends(get_db)):
    """
        redirectUrl이 있는 경우 처리하고 값이 없는 경우 결과값과 아이디 반환
    """

    clientId = os.getenv("CLIENT_ID")
    clientSecret = os.getenv("CLIENT_SECRET")
    today = date.today()
    current_date = today.strftime('%Y-%m-%d')

    rows = msdb_manager.fetch_all(Token.Get_Token(), params=(clientId, clientSecret, current_date))
    _token = rows[0]['token']

    if _token == token :
        client_ip = request.client.host
        try:
            # user = get_user(db, form_data.username)
            users = mworks_access_token(MemberID, client_ip)


            if not users:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Incorrect account ID",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            for row in users:
                office_id = row['office_id']
                EmpSeqNo = row['EmpSeqNo']
                nurse_id = row['nurse_id']
                account_id = row['account_id']
                EmpAuthGbn = row['EmpAuthGbn']
                name = row['name']
                # nurse_id = row['nurse_id']
                group_id = row['group_id']
                is_head_nurse = row['is_head_nurse']
                mb_part = row['mb_part']
                office_name = row['office_name']
                mb_part_name = row['mb_partName']
            # ADM 여부는 EmpAuthGbn으로 판정
            is_master_admin = True if str(EmpAuthGbn).upper() == 'ADM' else False

            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

            # nurses 테이블의 값으로 보강/덮어쓰기
            extra_data = get_extra_data_from_nurses(db, account_id)
            office_id = extra_data.get("office_id") or office_id
            group_id = extra_data.get("group_id") or group_id
            if "is_head_nurse" in extra_data:
                is_head_nurse = extra_data["is_head_nurse"]

            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            access_token = create_login_token(
                data={
                    "office_id": office_id,
                    "EmpSeqNo": EmpSeqNo,
                    "account_id": account_id,
                    "EmpAuthGbn": EmpAuthGbn,
                    "is_master_admin": is_master_admin,
                    "nurse_id": nurse_id,
                    "group_id": group_id,
                    "is_head_nurse": is_head_nurse,
                    "name": name,
                    "mb_part": mb_part,
                    "office_name": office_name,
                    "mb_part_name": mb_part_name,
                },
                expires_delta=access_token_expires,
            )

            response.set_cookie(
                key="access_token",
                value=f"Bearer {access_token}",
                httponly=True,
                samesite="lax"
            )

            return UserSchema(
                nurse_id=nurse_id,
                account_id=account_id,
                office_id=office_id,  # This should now work with eager loading
                group_id=group_id,
                is_head_nurse=is_head_nurse,
                is_master_admin=(
                    bool(is_master_admin) if is_master_admin is not None else (str(EmpAuthGbn).upper() == 'ADM')),
                name=name,
                EmpSeqNo=EmpSeqNo,
                EmpAuthGbn=EmpAuthGbn,
                mb_part=mb_part,
                office_name=office_name,
                mb_part_name=mb_part_name,
            )

            # return {"result": "succeed", "message": "Login successful", "account_id": MemberID}

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Invalid login : {str(e)}")
    else :
        raise HTTPException(status_code=401, detail=f"Invalid login.")

def mworks_access_token (account_id: str, client_ip: str) :
    rows = msdb_manager.fetch_all(Member.login_check_token(), params=(account_id))
    for row in rows :
       EmpSeqNo = row['EmpSeqNo']
       OfficeCode = row['OfficeCode']
       EmpAuthGbn = row['EmpAuthGbn']
       aiuseyn = row['aiuseyn']
    LogType = 'W'
    RegDate = datetime.now()

    if aiuseyn != 'Y' :
        raise HTTPException(status_code=500, detail=f"AI근무표 서비스에 가입되지 않았습니다.")

    params = (account_id, RegDate, client_ip, EmpSeqNo, OfficeCode, LogType)

    new_id = msdb_manager.execute(Member.login_log(), params=params)

    if new_id is None:
        new_id = msdb_manager.execute(Member.login_update(), params=(EmpSeqNo))

    try :
        user_info = msdb_manager.fetch_all(Member.member_view(), params=(account_id))
        return user_info

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")

