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


@router.get("/groups", summary="메시지 수신자 그룹 목록 (office 내 전체)")
def get_message_groups(
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    return message_service.get_available_groups(db, current_user.office_id)


@router.get("/memberlist", summary="메시지 수신자 목록", response_model=List[MessageMemberItem])
def get_member_list(
    extra_group_ids: Optional[str] = None,  # 콤마 구분 "groupA,groupB"
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    extra = [g.strip() for g in extra_group_ids.split(",")] if extra_group_ids else None
    return message_service.get_member_list(
        db,
        office_id=current_user.office_id,
        group_id=current_user.group_id,
        extra_group_ids=extra,
    )


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

    count = message_service.create_message(
        db,
        office_id=current_user.office_id,
        sender_nurse_id=current_user.nurse_id,
        receiver_nurse_ids=body.receiver_nurse_ids,
        message=body.message,
        message_img=body.message_img,
    )
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
