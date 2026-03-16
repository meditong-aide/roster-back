from typing import List, Optional
from datetime import datetime

from sqlalchemy.orm import Session, aliased

from db.models import Message, Nurse, Group


def get_member_list(db: Session, office_id: str, group_id: str, extra_group_ids: Optional[List[str]] = None) -> List[dict]:
    """
    메시지 수신자 목록 조회.
    - 기본: 동일 group_id
    - extra_group_ids 지정 시: 동일 office 내 해당 group들 추가 (office 범위 벗어나는 group은 자동 제외)
    """
    target_group_ids = [group_id]

    if extra_group_ids:
        valid_groups = db.query(Group.group_id).filter(
            Group.office_id == office_id,
            Group.group_id.in_(extra_group_ids)
        ).all()
        target_group_ids += [g.group_id for g in valid_groups]

    nurses = (
        db.query(Nurse, Group.group_name)
        .join(Group, Nurse.group_id == Group.group_id)
        .filter(
            Nurse.group_id.in_(target_group_ids),
            Nurse.active == 1,
        )
        .order_by(Group.group_name, Nurse.sequence)
        .all()
    )

    return [
        {
            "nurse_id": n.nurse_id,
            "name": n.name,
            "role": n.role,
            "level_": n.level_,
            "group_id": n.group_id,
            "group_name": group_name,
        }
        for n, group_name in nurses
    ]


def get_available_groups(db: Session, office_id: str) -> List[dict]:
    """동일 office 내 선택 가능한 group 목록 반환 (수신자 확장 선택용)."""
    groups = db.query(Group).filter(Group.office_id == office_id).all()
    return [{"group_id": g.group_id, "group_name": g.group_name} for g in groups]


def create_message(
    db: Session,
    office_id: str,
    sender_nurse_id: str,
    receiver_nurse_ids: List[str],
    message: Optional[str],
    message_img: Optional[str],
) -> int:
    """수신자 수만큼 message row 생성. 생성된 건수 반환."""
    rows = [
        Message(
            office_id=office_id,
            sender_nurse_id=sender_nurse_id,
            receiver_nurse_id=receiver_id,
            message=message,
            message_img=message_img,
        )
        for receiver_id in receiver_nurse_ids
    ]
    db.add_all(rows)
    db.commit()
    return len(rows)


def get_message_list(
    db: Session,
    nurse_id: str,
    msg_type: str,   # "send" | "reception"
    offset: int,
    limit: int,
) -> List[dict]:
    """보낸/받은 메시지 목록 조회."""
    SenderNurse = aliased(Nurse)
    ReceiverNurse = aliased(Nurse)

    q = (
        db.query(Message, SenderNurse, ReceiverNurse)
        .join(SenderNurse, Message.sender_nurse_id == SenderNurse.nurse_id)
        .join(ReceiverNurse, Message.receiver_nurse_id == ReceiverNurse.nurse_id)
    )

    if msg_type == "send":
        q = q.filter(Message.sender_nurse_id == nurse_id)
    else:
        q = q.filter(Message.receiver_nurse_id == nurse_id)

    rows = q.order_by(Message.id.desc()).offset(offset).limit(limit).all()

    return [_to_dict(msg, sender, receiver) for msg, sender, receiver in rows]


def get_message_count(db: Session, nurse_id: str, msg_type: str) -> int:
    """보낸/받은 메시지 총 건수."""
    q = db.query(Message)
    if msg_type == "send":
        q = q.filter(Message.sender_nurse_id == nurse_id)
    else:
        q = q.filter(Message.receiver_nurse_id == nurse_id)
    return q.count()


def get_message(db: Session, message_id: int) -> Optional[dict]:
    """단건 조회."""
    SenderNurse = aliased(Nurse)
    ReceiverNurse = aliased(Nurse)

    row = (
        db.query(Message, SenderNurse, ReceiverNurse)
        .join(SenderNurse, Message.sender_nurse_id == SenderNurse.nurse_id)
        .join(ReceiverNurse, Message.receiver_nurse_id == ReceiverNurse.nurse_id)
        .filter(Message.id == message_id)
        .first()
    )
    if not row:
        return None
    return _to_dict(*row)


def mark_as_read(db: Session, message_id: int, nurse_id: str) -> bool:
    """수신자 본인 확인 후 읽음 처리. 성공 여부 반환."""
    msg = db.query(Message).filter(
        Message.id == message_id,
        Message.receiver_nurse_id == nurse_id,
        Message.is_read == False,
    ).first()

    if not msg:
        return False

    msg.is_read = True
    msg.read_at = datetime.now()
    db.commit()
    return True


def delete_message(db: Session, message_id: int, nurse_id: str) -> bool:
    """발신자 또는 수신자 본인만 삭제 가능. 성공 여부 반환."""
    msg = db.query(Message).filter(
        Message.id == message_id,
        (Message.sender_nurse_id == nurse_id) | (Message.receiver_nurse_id == nurse_id),
    ).first()

    if not msg:
        return False

    db.delete(msg)
    db.commit()
    return True


def _to_dict(msg: Message, sender: Nurse, receiver: Nurse) -> dict:
    return {
        "id": msg.id,
        "sender_nurse_id": msg.sender_nurse_id,
        "sender_name": sender.name,
        "sender_role": sender.role,
        "receiver_nurse_id": msg.receiver_nurse_id,
        "receiver_name": receiver.name,
        "receiver_role": receiver.role,
        "message": msg.message,
        "message_img": msg.message_img,
        "is_read": msg.is_read,
        "created_at": msg.created_at,
        "read_at": msg.read_at,
    }
