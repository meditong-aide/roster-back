from fastapi import APIRouter, Depends, HTTPException
from fastapi.templating import Jinja2Templates

from datalayer.sticker import sticker
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema

router = APIRouter()

templates = Jinja2Templates(directory="templates")


@router.get("/list", summary="회원 해당달 스티커 조회")
def sticker_list(stcker_date: str, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    """
    * stcker_date : 2025-07
        - 해당 달 스티커 데이터
    * 호출방식 : /sticker/list?stcker_date=2025-05
    * return : [ \n
        {\n
            "OfficeCode": row['OfficeCode'],        # 오피스코드\n
            "EmpSeqNo": row['EmpSeqNo'],            # 회원번호\n
            "stcker_date": row['stcker_date'],      # 스티커 호출 날짜, 예: 2025-07\n
            "sticker_contents": row['sticker_contents'] # 스티커 데이터, 예: 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0\n
        }\n
    ]
    """

    # get_current_user_from_cookie 는 미인증이면 예외가 아니라 None 을 준다.
    # 가드가 없으면 아래 역참조에서 AttributeError → 401 이어야 할 자리에 500 이 나간다.
    if not current_user:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    EmpSeqNo = current_user.nurse_id
    OfficeCode = current_user.office_id

    rows = msdb_manager.fetch_all(sticker.get_list(), params=(OfficeCode, EmpSeqNo, stcker_date))

    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    return [{
        "OfficeCode": row['OfficeCode'],
        "EmpSeqNo": row['EmpSeqNo'],
        "stcker_date": row['stcker_date'],
        "sticker_contents": row['sticker_contents']

    } for row in rows]
