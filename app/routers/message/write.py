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

# 메세지 등록 사용자 목록
@router.get("/memberlist", summary="메세지등록 조직인원")
def message_write_form(current_user: UserSchema = Depends(get_current_user_from_cookie), deptyn : str | None = 'Y'):

    print("memberlist current_user : ", current_user)
    """
    <option value="EmpSeqNo">name-> 팀명, EmployeeName->이름, part_name -> 직위</option>
    """
    OfficeCode = current_user.office_id
    mb_part = current_user.mb_part

    if deptyn == 'Y':
        params = (OfficeCode, mb_part)
    else:
        params = OfficeCode

    all_users = msdb_manager.fetch_all(Common.get_organization_member(deptyn), params=params)

    return all_users

# 메세지 화면 출력
@router.get("/write", summary="메세지등록 화면처리", description="메세지등록 화면처리")
def message_write_form(request: Request,current_user: UserSchema = Depends(get_current_user_from_cookie)):
    print("current_user : ", current_user)



    OfficeCode = current_user.office_id

    all_users = msdb_manager.fetch_all(Common.get_organization_member(), params=OfficeCode)

    return templates.TemplateResponse("message_write.html", {"request": request, "all_users": all_users})

@router.post("/write", summary="메세지등록")
def set_message(current_user: UserSchema = Depends(get_current_user_from_cookie),
        # 기본 정보 (읽기 전용 포함)
        recipient_ids: List[str] = Form(..., alias="recipient_ids"), # 다중 선택 필드는 리스트로 받습니다.
        message: str = Form(...),
        messageimg: str = Form(None)
):
    """
    * recipient_ids: 수신자 empseqno, 다중선택 가능 전달을 List 형태로 전달 [테스트는 멀티셀렉트 박스로 진행함]
    * message: 메세지내용
    * messageimg: 메세지 이미지
    * 반환값 : result, message
    *       result : success -> 성공, fail -> 실패
    """


    OfficeCode = current_user.office_id
    sendempseqno = current_user.nurse_id

    if not recipient_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="유효한 수신자가 선택되지 않았습니다.")

    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="메세지 내용이 입력되지 않았습니다.")

    try:
        for recipient_id in recipient_ids:
            row = msdb_manager.execute(Message.set_message(), params=(OfficeCode, sendempseqno, recipient_id, message, messageimg))

        return {"result": "success", "message": "메세지가 전송되었습니다."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing email request: {e}")