import os
from datetime import date
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, status, Response, Request
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_301_MOVED_PERMANENTLY

from datalayer.member import Member
from datalayer.token import Token
from db.client2 import msdb_manager
from utils.security import create_access_token, create_login_token

ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 1 day

# Configuration
router = APIRouter(
    prefix="/token",
    tags=["token"]
)

@router.post("/", summary="Token 생성")
async def get_token(response: Response, clientId: str, clientSecret: str):
    """
        토큰은 1일 단위로 생성되며, 중복 호출 시 DB에 저장된 값으로 반환함.
    """
    _clientId = os.getenv("CLINET_ID")
    _clientSecret = os.getenv("CLINET_SECRET")

    if clientId == _clientId and clientSecret == _clientSecret :
        token = create_access_token(data={"clientSecret": clientSecret, "clientId": clientId})
    else :
        raise HTTPException(status_code=401, detail=f"Invalid client ID or secret provided.")

@router.post("/login", summary="Token, 회원아이디로 sso")
async def login_for_access_token(response: Response, request: Request, token: str, MemberID: str, redirectUrl: str | None = None):
    """
        redirectUrl이 있는 경우 처리하고 값이 없는 경우 결과값과 아이디 반환
    """

    clientId = os.getenv("CLINET_ID")
    clientSecret = os.getenv("CLINET_SECRET")
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
                OfficeCode = row['OfficeCode']
                EmpSeqNo = row['EmpSeqNo']
                account_id = row['account_id']
                EmpAuthGbn = row['EmpAuthGbn']
                name = row['name']
                nurse_id = row['nurse_id']
                group_id = row['group_id']
                is_head_nurse = row['is_head_nurse']

            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            access_token = create_login_token(
                data={"OfficeCode": OfficeCode, "EmpSeqNo": EmpSeqNo, "account_id": account_id, "EmpAuthGbn": EmpAuthGbn
                    , "nurse_id": EmpSeqNo, "group_id": group_id, "is_head_nurse": is_head_nurse, "name": name},
                expires_delta=access_token_expires
            )

            response.set_cookie(
                key="access_token",
                value=f"Bearer {access_token}",
                httponly=True,
                samesite="lax"
            )

            if redirectUrl :
                return RedirectResponse(url=redirectUrl, status_code=HTTP_301_MOVED_PERMANENTLY)
            else :
                return {"result": "succeed", "message": "Login successful", "account_id": MemberID}

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
    LogType = 'W'
    RegDate = datetime.now()

    params = (account_id, RegDate, client_ip, EmpSeqNo, OfficeCode, LogType)

    new_id = msdb_manager.execute(Member.login_log(), params=params)

    if new_id is None:
        new_id = msdb_manager.execute(Member.login_update(), params=(EmpSeqNo))

    try :
        user_info = msdb_manager.fetch_all(Member.member_view(), params=(account_id))
        return user_info

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")

