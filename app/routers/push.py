import base64 as _b64
import logging
import os
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

logger = logging.getLogger(__name__)

# `/push/inbox` 조회 상한(초). 이 경로만 건다 — 그룹웨어(bizwiz20db)를 보는데
# 그쪽은 21개 DB 가 한 인스턴스에 얹힌 공용 서버라 남의 락 경합에 물릴 수 있다.
# 모바일이 폴링하는 화면이라 물리면 워커가 차례로 잡힌다.
# ★ 전역이 아니라 여기만인 이유는 `MsDbManager.get_connection` 도크스트링 참조.
_INBOX_QUERY_TIMEOUT = int(os.getenv("PUSH_INBOX_QUERY_TIMEOUT", "20"))

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
def message_view(
    listsize: int,
    scope: Literal["linked"] | None = None,
    current_user: UserSchema = Depends(require_current_user),
):
    """
    * 호출방식 : /push/list?listsize=10 · /push/list?listsize=10&scope=linked

    * scope (선택)
      - 생략: 전체 알림(기존 동작 그대로)
      - `linked`: **페이지 이동이 되는 알림만**. 원티드 요청·근무표 마감처럼 눌렀을 때
        갈 곳이 있는 것들이다. 프리셉티 종료·병동이동 배정 같은 통보성 알림은 빠진다.

      ★ 판별자는 `linkUrl` 이 **아니다**. 실측상 그 컬럼은 전 행이 비어 있다.
        실제 기준은 `linkCode` — 서버가 `pushsubcode` 와 메시지에서 `ROSTER:YYYY:MM` ·
        `WANTED:YYYY:MM` 을 파생시키고, 파생하지 못한 행은 `Idx`(숫자)가 된다.
      ★ 필터는 `TOP` 앞에서 걸린다. 즉 `listsize` 는 '최근 N건 중 공지'가 아니라
        **'공지 N건'** 을 뜻한다.
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
    # scope 는 Literal 이라 FastAPI 가 422 로 막는다 — 여기서 다시 검증하지 않는다.
    rows = msdb_manager.fetch_all(
        Common.get_push_list(linked_only=(scope == "linked")),
        params=(OfficeCode, EmpSeqNo, listsize),
    )

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


def _inbox_cursor_encode(idx: int) -> str:
    return _b64.urlsafe_b64encode(str(idx).encode("ascii")).decode("ascii")


def _inbox_cursor_decode(cursor: str | None) -> int | None:
    """커서 → Idx. 없거나 깨졌으면 None(= 처음부터).

    ★ 깨진 커서를 400 으로 막지 않는다. 서버가 커서 형식을 바꾼 뒤 남아 있던 옛 커서
      하나로 목록이 통째로 안 열리면 안 된다. 처음부터 주는 편이 안전하다.
    """
    if not cursor:
        return None
    try:
        return int(_b64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except Exception:
        return None


@router.get("/inbox", summary="알림 목록 (커서 무한스크롤)")
def push_inbox(
    filter: Literal["all", "unread"] = "all",
    limit: int = 20,
    cursor: str | None = None,
    scope: Literal["linked"] | None = None,
    current_user: UserSchema = Depends(require_current_user),
):
    """알림을 최신순 커서로. PC·모바일 공용.

    * 호출방식 : /push/inbox?filter=all&limit=20
                 /push/inbox?filter=unread&limit=20&cursor=...
                 /push/inbox?filter=all&scope=linked
    * filter : `all` | `unread`. 서버가 **전체에서** 거른 뒤 페이지를 낸다 —
      기존 `/list` 처럼 '최근 50개 안에서' 세지 않으므로 오래된 미확인도 잡힌다.
    * scope=linked : 페이지 이동이 되는 알림만(`/push/list?scope=linked` 와 같은 기준).
    * 리턴값 : `{"result": {items, nextCursor, total, unreadTotal}}`
      - items[] 필드는 기존 `/push/list` 와 **동일**하다(pushcode·Message·ReadYN·fk_idx…).
      - total : 현재 `scope` 의 전체 표시 항목 수. `filter=unread` 면 `unreadTotal` 과 같다.
      - unreadTotal : filter 와 무관한, 현재 scope 안의 안 읽은 전체 수.
        읽음 버튼 활성 여부를 이 값으로 판단하면 된다.
      - 둘 다 페이지 길이가 아니다. 프론트가 페이지별로 더하면 안 된다.
      - nextCursor 가 null 이면 끝이다.

    ★ 기존 `/list`·`/listcnt` 는 그대로 둔다 — PC 가 쓰고 있어 지금 지우면 깨진다.
    ★ 대표 알림·unread 판정 순서는 `Common._push_inbox_cte` 도크스트링이 정본이다.
      목록·total·unreadTotal 이 같은 규칙을 써야 건수가 어긋나지 않는다.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit 은 1~100 이어야 합니다.")

    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.EmpSeqNo
    linked = scope == "linked"
    cursor_idx = _inbox_cursor_decode(cursor)

    # 한 건 더 떠서 다음 페이지 유무를 판정한다(총건수와 비교하면 그 사이 추가·삭제에 어긋난다).
    #
    # ★ params 순서는 **SQL 텍스트에 `%s` 가 나오는 순서**다(pymssql 은 위치 기반).
    #   `Top %s` 가 `Where ... Idx < %s` 보다 **앞**에 있으므로 limit 이 cursor 보다 먼저다.
    #   뒤집으면 `Top <커서Idx>` · `Idx < 21` 이 되어 목록이 거의 비는데,
    #   에러가 아니라 '결과가 적은' 형태라 조용히 잘못 나간다.
    params = (OfficeCode, EmpSeqNo, limit + 1) + (
        (cursor_idx,) if cursor_idx is not None else ()
    )
    try:
        rows = msdb_manager.fetch_all(
            Common.get_push_inbox(
                linked_only=linked,
                unread_only=(filter == "unread"),
                use_cursor=cursor_idx is not None,
            ),
            params=params,
            timeout=_INBOX_QUERY_TIMEOUT,
        )
        # 건수는 목록 행에 얹혀 온다(같은 무거운 스캔을 두 번 돌지 않으려고).
        # 0행일 때만 따로 부른다 — 알림이 없거나 커서가 끝을 넘은 경우다.
        # ★ 이 조회도 **반드시 같은 try 안에서 같은 상한**을 받아야 한다. 밖으로 빼면
        #   알림이 없는 계정이 매번 타는 흔한 경로가 상한 없이 공용 DB 를 훑게 되어
        #   위에 건 보호가 그대로 무력해진다.
        if rows:
            total_all = int(rows[0]['total_all'] or 0)
            unread_total = int(rows[0]['unread_all'] or 0)
        else:
            cnt = msdb_manager.fetch_all(
                Common.get_push_inbox_counts(linked_only=linked),
                params=(OfficeCode, EmpSeqNo),
                timeout=_INBOX_QUERY_TIMEOUT,
            )
            total_all = int((cnt[0]['total'] if cnt else 0) or 0)
            unread_total = int((cnt[0]['unreadTotal'] if cnt else 0) or 0)
    except Exception as exc:
        # ★ 상한에 걸리면 **504** 로 끊는다. 여기서 무한정 기다리면 uvicorn 워커가
        #   그 대기에 잡히고, 모바일이 폴링하는 경로라 워커가 차례로 물려 서버 전체가
        #   느려진다. 조회 하나 실패는 화면 한 곳이 비는 일이지만, 워커가 물리면
        #   로그인까지 멈춘다 — 후자가 훨씬 나쁘다.
        logger.warning("[push/inbox] 조회 실패(또는 시간 초과): %s", exc)
        raise HTTPException(
            status_code=504,
            detail="알림을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )
    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    has_more = len(rows) > limit
    rows = rows[:limit]

    return {
        "result": {
            "items": [{
                "pushcode": row['pushcode'],
                "pushsubcode": row['pushsubcode'],
                "officecode": row['officecode'],
                "senderEmpSeqNo": row['senderEmpSeqNo'],
                "sendername": row['sendername'],
                "senderduty": row['senderduty'],
                "Message": row['Message'],
                "regdate": row['regdate'],
                "ReadYN": row['ReadYN'],
                "fk_idx": row['Fk_Idx'],
                "linkUrl": row['LinkUrl'],
                "linkCode": row['LinkCode'],
            } for row in rows],
            "nextCursor": _inbox_cursor_encode(rows[-1]['Idx']) if (has_more and rows) else None,
            # unread 필터일 때의 total 은 '표시 중인 집합의 전체'라 unreadTotal 과 같다.
            "total": unread_total if filter == "unread" else total_all,
            "unreadTotal": unread_total,
        }
    }


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
