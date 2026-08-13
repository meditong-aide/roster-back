from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Literal

from datalayer.common import Common
from datalayer.setting import Setting
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie, require_current_user
from schemas.auth_schema import User as UserSchema


class PushSettingRequest(BaseModel):
    push_yn: Literal["Y", "N"]

class PushReadRequest(BaseModel):
    fk_idx: int

router = APIRouter(
    prefix="/push",
    tags=["push"]
)

@router.get("/listcnt", summary="총 게시물수")
def message_view(current_user: UserSchema = Depends(require_current_user)):
    """
    * 호출방식 : /push/listcnt
    * 리턴값 : PushCode, PushCnt
    """
    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.EmpSeqNo

    rows = msdb_manager.fetch_all(Common.get_push_cnt(), params=(OfficeCode, EmpSeqNo))

    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    return [{
        "PushCode": row['PushCode'],
        "PushCnt": row['PushCnt']
    } for row in rows]


@router.get("/list", summary="메세지 리스트")
def message_view(listsize: int, current_user: UserSchema = Depends(require_current_user)):
    """
    * 호출방식 : /push/list?listsize=10
    * 리턴값 :
      - pushcode: 푸시 구분코드 (AI근무표 P30)
      - pushsubcode: 푸시 서브코드
      - officecode: 병원코드
      - senderEmpSeqNo: 푸시 전송자 EmpSeqNo
      - sendername: 푸시 전송자명
      - Message: 푸시 메세지
      - regdate: 등록일 ex) 2025-11-06
      - ReadYN : 읽음 여부 (Y,N)
    """
    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.EmpSeqNo

    if not listsize:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="리스트수값이 필요합니다.")

    # params 순서 변경: CTE로 인해 (OfficeCode, EmpSeqNo, listsize) 순서
    rows = msdb_manager.fetch_all(Common.get_push_list(), params=(OfficeCode, EmpSeqNo, listsize))

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
        "ReadYN": row['ReadYN'],
        "fk_idx": row['Fk_Idx'],
        "linkUrl": row['LinkUrl'],
        "linkCode": row['LinkCode'],
    } for row in rows]


@router.patch("/read", summary="알림 단건 읽음 처리 (웹 - pushcode 기준)")
def mark_push_read_by_code(
    pushcode: str,
    pushsubcode: str,
    officecode: str,
    current_user: UserSchema = Depends(require_current_user)
):
    """
    * 호출방식 : PATCH /push/read?pushcode=P30&pushsubcode=S04&officecode=102560
    * 기능 : pushcode + pushsubcode + officecode 조건에 해당하는 알림을 ReadYN = Y로 변경
    """
    EmpSeqNo = current_user.EmpSeqNo
    OfficeCode = current_user.office_id

    msdb_manager.execute(
        Common.update_push_read_by_code(),
        params=(EmpSeqNo, OfficeCode, pushcode, pushsubcode, officecode)
    )

    return {"message": "읽음 처리 완료"}


@router.patch("/read/one", summary="알림 단건 읽음 처리")
def mark_one_push_read(req: PushReadRequest, current_user: UserSchema = Depends(require_current_user)):
    """
    * 호출방식 : PATCH /push/read/one
    * 바디 : { "fk_idx": 123 }
    * 기능 : 특정 알림 1건을 ReadYN = Y로 변경
    """
    EmpSeqNo = current_user.EmpSeqNo
    OfficeCode = current_user.office_id

    msdb_manager.execute(Common.update_push_read_one(), params=(req.fk_idx, EmpSeqNo, OfficeCode))

    return {"message": "읽음 처리 완료"}


@router.patch("/read/all", summary="알림 전체 읽음 처리")
def mark_all_push_read(current_user: UserSchema = Depends(require_current_user)):
    """
    * 호출방식 : PATCH /push/read/all
    * 기능 : 알림 모달 진입 시 안읽은 알림 전체를 ReadYN = Y로 일괄 변경
    """
    EmpSeqNo = current_user.EmpSeqNo
    OfficeCode = current_user.office_id

    msdb_manager.execute(Common.update_push_read_all(), params=(EmpSeqNo, OfficeCode))

    return {"message": "전체 읽음 처리 완료"}


@router.get("/setting", summary="푸시 알림 수신 여부 조회")
def get_push_setting(current_user: UserSchema = Depends(require_current_user)):
    """
    * 호출방식 : GET /push/setting
    * 리턴값 : push_yn (Y/N)
    """
    MemberID = current_user.account_id

    row = msdb_manager.fetch_all(Setting.get_push_yn(), params=(MemberID,))

    # 설정 행은 직원 엑셀 일괄등록(setting/member.py) 경로에서만 만들어져서, 그 경로를
    # 안 거친 계정은 행이 없다. 이건 오류가 아니라 "아직 안 만든 상태"이고, 행을 만들 때
    # 넣는 기본값이 PushYN='Y' 다 → 같은 값을 200 으로 돌려준다.
    # ★ 404 를 쓰면 안 된다 — CloudFront 가 `/api/*` 의 404 를 `index.html` 200 으로
    #   바꿔 보내서(CustomErrorResponses 는 배포 전체 적용) 클라이언트의 404 분기가 죽고,
    #   HTML 을 JSON 으로 파싱하다 모바일 화면이 하얗게 뜬다.
    if not row:
        return {"push_yn": "Y"}

    return {"push_yn": row[0]["PushYN"]}


@router.patch("/setting", summary="푸시 알림 수신 여부 변경")
def update_push_setting(req: PushSettingRequest, current_user: UserSchema = Depends(require_current_user)):
    """
    * 호출방식 : PATCH /push/setting
    * 바디 : { "push_yn": "Y" | "N" }
    * 기능 : PushYN 변경 → Y면 푸시 수신, N이면 기기 푸시 차단
    """
    MemberID = current_user.account_id

    result = msdb_manager.execute(Setting.update_push_yn(), params=(req.push_yn, MemberID))

    if result == 0:
        raise HTTPException(status_code=404, detail="설정 정보를 찾을 수 없습니다.")

    return {"message": "푸시 설정이 변경되었습니다.", "push_yn": req.push_yn}
