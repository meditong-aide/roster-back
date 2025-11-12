from typing import List

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.templating import Jinja2Templates

from datalayer.common import Common
from datalayer.message import Message
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# 메세지 화면 출력
@router.get("/reply", summary="메세지등록 화면처리")
def message_write_form(idx:int, request: Request,current_user: UserSchema = Depends(get_current_user_from_cookie)):
    if not idx:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="잘못된 접근 입니다.")

    OfficeCode = current_user.OfficeCode
    rows = msdb_manager.fetch_all(Message.get_message_view(), params=idx)

    print("rows : ", rows)

    if rows:
        sendername = rows[0]['sendername']
        sendempseqno = rows[0]['sendempseqno']
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="잘못된 접근 입니다.")

    return templates.TemplateResponse("message_reply.html", {"request": request, "sendername": sendername, "sendempseqno": sendempseqno})

@router.post("/reply", summary="메세지등록")
def set_message(current_user: UserSchema = Depends(get_current_user_from_cookie),
        # 기본 정보 (읽기 전용 포함)
        receptionid: str = Form(...),
        message: str = Form(...),
        messageimg: str = Form(None)
):
    """
    receptionid : 이전 메세지 전송자 empseqno
    message : 메세지 내용
    messageimg : 메세지 이미지

    반환값 : result, message
           result : success -> 성공, fail -> 실패
    """
    OfficeCode = current_user.office_id
    sendempseqno = current_user.nurse_id

    if not receptionid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효한 수신자가 선택되지 않았습니다.")

    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="메세지 내용이 입력되지 않았습니다.")

    try:
        row = msdb_manager.execute(Message.set_message(), params=(OfficeCode, sendempseqno, receptionid, message, messageimg))

        return {"result": "success", "message": "답변 메세지가 전송되었습니다."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing email request: {e}")