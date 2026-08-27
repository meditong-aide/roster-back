from datetime import datetime

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
      - senderduty: 푸시 전송자 직함 (string | null)
          · roster 의 `nurses.level_` 을 조회 시점에 조인한다.
            예) 수간호사 · 책임간호사 · 주임간호사 · 간호사 · 간호조무사 · 일반
          · 값이 없거나 **발신자가 roster 에 등록돼 있지 않으면** 필드를 생략하지 않고
            null 을 준다. 근무표 알림을 보내는 사람이 반드시 간호 인력으로 등록돼 있지는 않다.
          · 발행 시점 스냅샷이 아니라 **현재 값**이다. 푸시 이력은 그룹웨어 테이블이라
            컬럼 추가가 곧 그룹웨어 스키마 변경이고, 조인 방식은 과거 알림까지 직함이
            채워진다는 이점이 있다(스냅샷은 신규만 값이 생긴다).
          · 그룹웨어 `Member.duty` 는 쓰지 않는다 — 실측상 거의 비어 있다
            (재직 1,796명 중 NULL 1,792). 그룹웨어 직위(`T_Part`)도 후보였으나,
            직함을 우리가 직접 고칠 수 있는 roster 컬럼으로 가기로 했다.
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
        # 값이 없으면 None → JSON null. 프론트 계약상 필드를 생략하지 않는다.
        "senderduty": row['senderduty'],
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

    # UPDATE 0건 = 설정 행이 아직 없는 계정이다. 행은 직원 엑셀 일괄등록
    # (setting/member.py) 경로에서만 만들어지므로, 그 경로를 안 거친 계정은
    # **첫 토글이 항상 실패**했다. GET 은 같은 상황을 기본값 'Y' 로 200 처리하는데
    # PATCH 만 404 를 내던 비대칭이었다.
    #
    # ★ 404 를 쓰면 안 되는 이유는 GET 쪽 주석과 같다 — CloudFront 가 `/api/*` 의
    #   404 를 `index.html` 200 으로 바꿔 보내서 클라이언트가 HTML 을 JSON 으로
    #   파싱하다 죽는다. 그러나 여기서는 코드만 바꾸는 게 아니라 **행을 만들어**
    #   요청을 실제로 이행한다. 조회(GET)가 이미 '없으면 Y' 로 동작하므로
    #   쓰기도 같은 전제로 맞추는 것이 정합적이다.
    if result == 0:
        msdb_manager.execute(
            Setting.insert_push_yn_if_absent(),
            params=(MemberID, req.push_yn, datetime.now(), MemberID),
        )
        # 경합이 나면 INSERT 쪽 `UPDLOCK, HOLDLOCK` 이 두 번째 요청을 대기시키고,
        # 그 요청은 NOT EXISTS 에 걸려 0건이 된다(= 행은 하나만 생긴다).
        # 그 경우 값은 먼저 들어간 쪽이므로 여기서 한 번 더 UPDATE 해 요청값을 반영한다.
        msdb_manager.execute(Setting.update_push_yn(), params=(req.push_yn, MemberID))

    return {"message": "푸시 설정이 변경되었습니다.", "push_yn": req.push_yn}
