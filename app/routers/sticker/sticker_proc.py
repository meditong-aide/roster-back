from fastapi import APIRouter, Depends, Form, HTTPException
from starlette import status

from datalayer.sticker import sticker
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema

router = APIRouter()

@router.post("/insert", summary="회원 해당 달 스티커 입력")
def sticker_in(current_user: UserSchema = Depends(get_current_user_from_cookie),
stcker_date: str = Form(...),
sticker_contents: str = Form(...)
):
    """
    * stcker_date: 스티커 날짜 ex)2025-07
    * sticker_contents: 스티커 데이터 ex) 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0"
    * 반환값 : result, message
    *       result : success -> 성공
    """

    EmpSeqNo = current_user.nurse_id
    OfficeCode = current_user.office_id

    if not stcker_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="스티커 날짜가 없습니다.")

    if not sticker_contents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="스티커 데이터가 없습니다.")
    try:
        # 클릭할때 마다 스티커가 생기고 사라지도록 FrontEnd가 구성되어 삭제 후 입력으로 진행
        msdb_manager.execute(sticker.delete_sticker(),params=(OfficeCode, EmpSeqNo, stcker_date))

        msdb_manager.execute(sticker.insert_sticker(), params=(OfficeCode, EmpSeqNo, stcker_date, sticker_contents))

        return {"result": "success", "message": "스티커가 저장되었습니다."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing sticker request: {e}")
