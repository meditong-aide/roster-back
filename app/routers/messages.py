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
    msg = message_service.get_message(db, message_id)
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
