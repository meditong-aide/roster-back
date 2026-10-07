from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.client2 import get_db
from routers.auth import get_current_user_from_cookie, require_current_user
from schemas.auth_schema import User as UserSchema
from schemas.message_schema import (
    MessageCountResponse,
    MessageCreateRequest,
    MessageItem,
    MessageMemberItem,
)
from services import message_service

router = APIRouter(prefix="/message", tags=["message"])


@router.get("/groups", summary="메시지 수신자 병동 목록 (보낼 수 있는 범위)")
def get_message_groups(
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """보낼 수 있는 병동만. 그룹관리자는 관리 병동 전부, 일반 근무자는 자기 병동 하나.

    ★ 종전에는 office 전체를 돌려줬다. 고를 수 없는 병동까지 뜨고, 그 자체가
      병원 조직도를 흘린다.
    """
    return message_service.get_available_groups(
        db, message_service.resolve_recipient_group_ids(db, current_user)
    )


@router.get("/memberlist", summary="메시지 수신자 목록", response_model=List[MessageMemberItem])
def get_member_list(
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """수신자 전량. 범위는 **호출자에게서 유도한다**(그룹관리자=관리 병동 전부,
    일반 근무자=자기 병동).

    ★★ `extra_group_ids` 파라미터를 없앴다. 서버가 "같은 office 인가" 만 보고
      "이 사람이 그 병동을 볼 수 있는가" 는 안 봐서, 일반 근무자도 파라미터 하나로
      병원 전 병동 명단을 훑을 수 있었다. 호출하던 곳은 없다(모바일은 늘 생략).
    ★ 응답은 **배열 그대로** 둔다. 배포된 모바일이 이 형태를 읽는다 — 페이지가
      필요하면 아래 `/memberlist/page` 를 쓴다.
    """
    return message_service.get_member_list(
        db, message_service.resolve_recipient_group_ids(db, current_user)
    )


@router.get("/memberlist/page", summary="메시지 수신자 목록 (커서)")
def get_member_list_page(
    limit: int = 20,
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """수신자 한 페이지. 받은함과 **같은 계약** — `{items, nextCursor, total}`.

    * 호출방식 : /message/memberlist/page?limit=30 · ...&cursor=...
    * 리턴값 : `{"result": {"items": [...], "nextCursor": str|null, "total": int}}`
    * 범위는 호출자에게서 유도한다 — 그룹관리자는 관리 병동 전부, 일반 근무자는
      자기 병동. 병동을 나누지 않고 **한 줄로** 이어 내려준다(각 행에 `group_name`
      이 실려 있어 화면이 소속을 그대로 보여줄 수 있다).
    * `limit` 은 프론트가 정하고 상한 200 으로 자른다 — 범위가 가장 큰 병원이
      262명이라, 상한이 없으면 한 번에 전량을 끌어오는 호출과 구분되지 않는다.
    * `q` 는 이름·직급·병동 이름 부분일치다(생략 가능). **검색을 서버가 하므로**
      화면이 전량을 들고 있지 않아도 결과가 정확하다 — 받은 만큼만 거르면 아직
      안 받은 사람이 "검색 결과 없음" 으로 나와, 사용자는 없다고 믿게 된다.
      `total` 도 거른 뒤 수다.
    ★ 커서는 **같은 `q` 안에서만** 유효하다. 검색어를 바꾸면 커서를 버리고 처음부터
      받아야 한다(정렬은 같아 이어받기 자체는 성립하지만 모수가 달라진다).

    ★ 정렬은 `(병동이름, sequence, nurse_id)` 다. 같은 병동 사람이 붙어 나오고,
      `sequence` 는 병동 안에서만 유일하므로 `nurse_id` 를 보조키로 둔다 — 없으면
      같은 값에서 건너뛰거나 되풀이된다.
    ★ `result` 래퍼는 받은함·보낸함과 같은 모양을 지키기 위해서다.
    """
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit 은 1 이상이어야 합니다.")
    return {
        "result": message_service.get_member_page(
            db,
            message_service.resolve_recipient_group_ids(db, current_user),
            limit=min(limit, 200),
            cursor=cursor,
            q=q,
        )
    }


@router.post("/write", summary="메시지 전송")
def write_message(
    body: MessageCreateRequest,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    if not body.receiver_nurse_ids:
        raise HTTPException(status_code=400, detail="수신자를 선택해주세요.")
    if not body.message and not body.message_img:
        raise HTTPException(status_code=400, detail="메시지 내용을 입력해주세요.")

    try:
        count = message_service.create_message(
            db,
            office_id=current_user.office_id,
            sender_nurse_id=current_user.nurse_id,
            receiver_nurse_ids=body.receiver_nurse_ids,
            message=body.message,
            message_img=body.message_img,
            # ★ 목록과 같은 범위로 검사한다. 목록에 뜬 사람은 반드시 보낼 수 있고,
            #   안 뜬 사람에게는 못 보낸다.
            allowed_group_ids=message_service.resolve_recipient_group_ids(
                db, current_user
            ),
        )
    except ValueError as exc:
        # 수신자 스코프 위반. 403 이 아니라 400 으로 답한다 — 403 은 "그 대상은
        # 실재하는데 권한이 없다" 로 읽혀 존재 여부를 흘린다.
        raise HTTPException(status_code=400, detail=str(exc))
    return {"result": "success", "sent_count": count}


@router.get("/received", summary="받은 메시지 목록 (커서)")
def get_received_messages(
    limit: int = 20,
    cursor: Optional[str] = None,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """받은 메시지를 최신순 커서로. 첫 요청은 `cursor` 생략.

    * 호출방식 : /message/received?limit=20 · /message/received?limit=20&cursor=...
    * 리턴값 : `{"result": {"items": [...], "nextCursor": str|null, "total": int}}`
      - items[].sender: `{nurse_id, name, role}` — 답장 대상이자 발신자 표시용
      - total: **받은함 전체 건수**. 페이지마다 같은 값이라 프론트가 더하면 안 된다.
      - nextCursor 가 null 이면 끝이다.

    ★ 조회는 읽음 처리를 하지 않는다 — 읽음은 `/message/read` 계열이 담당한다.
    ★ 기존 `/list`·`/listcnt` 는 그대로 둔다. PC 가 아직 쓰고 있어 지금 지우면 깨진다.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit 은 1~100 이어야 합니다.")
    return {
        "result": message_service.get_received_page(
            db, current_user.nurse_id, limit=limit, cursor=cursor
        )
    }


@router.get("/sent", summary="보낸 메시지 목록 (커서 · 발송 단위)")
def get_sent_messages(
    limit: int = 20,
    cursor: Optional[str] = None,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """보낸 메시지를 **발송 묶음** 단위로. 22명에게 한 번 보내면 1건이다.

    * 호출방식 : /message/sent?limit=20 · /message/sent?limit=20&cursor=...
    * 리턴값 : `{"result": {"items": [...], "nextCursor": str|null, "total": int}}`
      - items[]: `batch_id, message, message_img, created_at, recipient_count, recipients`
      - recipients[]: `message_id, nurse_id, name, role, is_read, read_at`
        `recipient_count` 는 이 배열 길이와 항상 일치한다.
      - total: **발송 횟수**(수신자 행 수가 아니다).

    ★ `batch_id` 는 그 묶음의 `MIN(id)` 다 — 별도 컬럼이 아니라 **기존 PK** 라 불변이다.
      묶는 키는 `(sender_nurse_id, created_at)` 이고, `create_message` 가 한 발송의
      `created_at` 을 한 값으로 고정해 넣는다(그쪽 주석 참조).
    ★ limit 은 발송 수에 걸린다. 수신자 묶음이 페이지 경계에서 잘리지 않는다.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit 은 1~100 이어야 합니다.")
    return {
        "result": message_service.get_sent_page(
            db, current_user.nurse_id, limit=limit, cursor=cursor
        )
    }


@router.get("/listcnt", summary="메시지 총 건수", response_model=MessageCountResponse)
def get_message_count(
    list_type: str = "reception",
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    total = message_service.get_message_count(db, current_user.nurse_id, list_type)
    return {"total_count": total}


@router.get("/list", summary="메시지 목록", response_model=List[MessageItem])
def get_message_list(
    list_type: str = "reception",
    offset: int = 0,
    limit: int = 10,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    return message_service.get_message_list(
        db,
        nurse_id=current_user.nurse_id,
        msg_type=list_type,
        offset=offset,
        limit=limit,
    )


@router.get("/view/{message_id}", summary="메시지 단건 조회", response_model=MessageItem)
def get_message(
    message_id: int,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    # 당사자(발신자·수신자)가 아니면 서비스가 None 을 준다 — 없는 메시지와 같은 404 다.
    msg = message_service.get_message(db, message_id, current_user.nurse_id)
    if not msg:
        raise HTTPException(status_code=404, detail="메시지를 찾을 수 없습니다.")

    message_service.mark_as_read(db, message_id, current_user.nurse_id)
    return msg


@router.delete("/delete/{message_id}", summary="메시지 삭제")
def delete_message(
    message_id: int,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    deleted = message_service.delete_message(db, message_id, current_user.nurse_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="메시지를 찾을 수 없거나 권한이 없습니다.")
    return {"result": "success"}
