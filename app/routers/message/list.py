from fastapi import APIRouter, HTTPException, Depends

from datalayer.message import Message
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema

router = APIRouter()

@router.get("/listcnt", summary="총 게시물수")
def message_view(list_type: str, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    """
    * list_type : send -> 보낸 메세지 리스트
    * list_type : reception -> 받은 메세지 리스트
    * 호출방식 : /message/listcnt?list_type=send
    * 리턴값 : total_count
    """
    EmpSeqNo = current_user.EmpSeqNo

    print("EmpSeqNo : ", EmpSeqNo)
    if not list_type: list_type = "send"

    rows = msdb_manager.fetch_all(Message.get_message_list_cnt(list_type), params=EmpSeqNo)

    print("rows : ", rows)
    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    return [{
        "total_count": row['total_count']
    } for row in rows]


@router.get("/list", summary="메세지 리스트")
def message_view(page: int, pagesize: int, list_type: str, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    """
    * list_type : send -> 보낸 메세지 리스트
    * list_type : reception -> 받은 메세지 리스트
    * page, pagesize, list_type -> get방식으로 전달
    * 호출방식 : /message/list?page=1&pagesize=10&list_type=send
    """
    EmpSeqNo = current_user.EmpSeqNo
    # if not page: page = 1
    # if not pagesize: pagesize = 10
    if not list_type: list_type = "send"

    rows = msdb_manager.fetch_all(Message.get_message_list(page, pagesize, list_type), params=EmpSeqNo)

    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    return [{
        "idx": row['idx'],
        "sendempseqno": row['sendempseqno'],
        "sendername": row['sendername'],
        "senderduty": row['senderduty'],
        "receptionempseqno": row['receptionempseqno'],
        "receptionname": row['receptionname'],
        "receptionduty": row['receptionduty'],
        "message": row['message'],
        "messageimg": row['messageimg'],
        "readyn": row['readyn'],
        "regdate": row['regdate'],
        "readdate": row['readdate']
    } for row in rows]
