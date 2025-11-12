from fastapi import APIRouter, Depends, HTTPException, status

from datalayer.common import Common
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema

router = APIRouter(
    prefix="/push",
    tags=["push"]
)

@router.get("/listcnt", summary="총 게시물수")
def message_view(current_user: UserSchema = Depends(get_current_user_from_cookie)):
    """
    * 호출방식 : /push/listcnt
    * 리턴값 : PushCode, PushCnt
    """
    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.nurse_id

    rows = msdb_manager.fetch_all(Common.get_push_cnt(), params=(OfficeCode, EmpSeqNo))

    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    return [{
        "PushCode": row['PushCode'],
        "PushCnt": row['PushCnt']
    } for row in rows]


@router.get("/list", summary="메세지 리스트")
def message_view(listsize: int, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    """
    * 호출방식 : /message/list?listsize=10
    * 리턴값 :
        pushcode: 푸시 구분코드 (AI근무표 P30)
        pushsubcode: 푸시 서브코드
        officecode: 병원코드
        senderEmpSeqNo: 푸시 전송자 EmpSeqNo
        sendername: 푸시 전송자명
        Message: 푸시 메세지
        regdate: 등록일 ex) 2025-11-06
        ReadYN : 읽음 여부 (Y,N)
    """
    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.nurse_id

    if not listsize:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="리스트수값이 필요합니다.")

    rows = msdb_manager.fetch_all(Common.get_push_list(), params=(listsize, OfficeCode, EmpSeqNo))

    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    return [{
        "pushcode": row['pushcode'],
        "pushsubcode": row['pushsubcode'],
        "officecode": row['officecode'],
        "senderEmpSeqNo": row['senderEmpSeqNo'],
        "sendername": row['sendername'],
        "Message": row['Message'],
        "regdate": row['regdate'],
        "ReadYN": row['ReadYN']
    } for row in rows]
