from fastapi import APIRouter, HTTPException, status

from datalayer.message import Message
from db.client2 import msdb_manager

router = APIRouter()

@router.get("/delete", summary="메세지 삭제")
def message_view(idx: int):
    """
    idx: 메시지 시퀀스

    반환값 : result, message
           result : success -> 성공, fail -> 실패
    """
    if not idx:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="잘못된 접근입니다.")

    read_result = msdb_manager.execute(Message.set_message_delete(), params=idx)

    if not read_result or read_result == 0:
        return {"result": "fail", "message": "오류가 발생하였습니다."}
    else:
        return {"result": "success", "message": "메세지가 삭제되었습니다."}

