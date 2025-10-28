from fastapi import APIRouter, HTTPException, status

from datalayer.message import Message
from db.client2 import msdb_manager

router = APIRouter()

@router.get("/view", summary="메세지 상세보기")
def message_view(idx: int):
    """
    idx : 메세지 시퀀스 번호
    호출방식 : /message/view?idx=1

    [반환값]
    "idx": 메세지 번호
    "sendername": 보내는사람명
    "senderduty": 보내는사람 직무
    "receptionname": 받는사람명
    "receptionduty": 받는사람 직무
    "message": 메세지
    "messageimg": 이미지
    "readyn": Y - 읽음, N -  않읽음
    "regdate": 메세지 등록일
    "readdate": 읽은 시간

    """
    if not idx:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="잘못된 접근입니다.")

    # 메세지 읽음 처리, 읽음 시간
    read_result = msdb_manager.execute(Message.set_message_read(), params=idx)

    if not read_result or read_result == 0:
        return {"result": "fail", "message": "오류가 발생하였습니다."}

    # 메세지 상세조회
    rows = msdb_manager.fetch_all(Message.get_message_view(), params=idx)

    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    return [{
        "idx": row['idx'],
        "sendername": row['sendername'],
        "senderduty": row['senderduty'],
        "receptionname": row['receptionname'],
        "receptionduty": row['receptionduty'],
        "message": row['message'],
        "messageimg": row['messageimg'],
        "readyn": row['readyn'],
        "regdate": row['regdate'],
        "readdate": row['readdate']
    } for row in rows]
