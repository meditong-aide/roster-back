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
    """수신자 수만큼 message row 생성. 생성된 건수 반환.

    ★ 한 발송의 모든 행에 같은 `batch_id` 를 심는다. 값은 **그 발송 첫 행의 `id`** 다.
      `flush()` 로 IDENTITY 를 먼저 받아 `min(id)` 를 구한 뒤 되쓴다. 같은 트랜잭션
      안이라 commit 전에 확정된다.

    ★★ 왜 시각이 아니라 id 인가 — `created_at` 은 `datetime`(scale 3)이라 저장될 때
      **~3.33ms 로 잘린다.** 파이썬이 마이크로초를 줘도 남지 않으므로, 시각으로 묶으면
      같은 발신자의 서로 다른 두 발송이 그 안에 들어올 때 한 묶음으로 합쳐진다
      (본문이 하나만 보이고 보낸함 total 이 하나 적게 세어진다).
      `id` 는 IDENTITY 라 충돌하지 않고, 정수라 정렬·커서 키로도 쓸 수 있다.

    ★ `created_at` 을 파이썬 값으로 고정하는 것은 그대로 둔다. 묶음 키는 아니지만,
      한 발송의 행들이 화면에서 서로 다른 시각으로 보이지 않아야 한다.
    """
    sent_at = datetime.now()
    rows = [
        Message(
            office_id=office_id,
            sender_nurse_id=sender_nurse_id,
            receiver_nurse_id=receiver_id,
            message=message,
            message_img=message_img,
            created_at=sent_at,
        )
        for receiver_id in receiver_nurse_ids
    ]
    db.add_all(rows)
    db.flush()                       # IDENTITY 확정 — 아직 커밋 전이다
    batch = min(int(r.id) for r in rows)
    for r in rows:
        r.batch_id = batch
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


# ───────────────────────────── 커서 페이징 ─────────────────────────────
#
# 정렬은 **최신순**이고 커서는 keyset 이다(OFFSET 아님). OFFSET 은 앞쪽에 행이
# 추가·삭제되면 그만큼 밀려 중복·누락이 난다 — 무한 스크롤에서 바로 드러난다.
#
# 받은함  키 = (created_at, id)   … 서로 다른 발신자가 같은 시각일 수 있어 id 가 보조키다.
# 보낸함  키 = (batch_id)         … 발송 묶음의 키 자체다. IDENTITY 에서 와 동률이 없다.
#
# 커서는 프론트가 해석하지 않는 opaque 문자열이다. 형식이 바뀌어도 계약이 안 깨지도록
# base64 로 감싼다. 깨진 커서는 400 이 아니라 **처음부터**로 처리한다 — 서버가 형식을
# 바꾼 뒤 남아 있던 옛 커서 하나로 목록이 통째로 못 열리면 안 된다.

import base64 as _b64


def _encode_cursor(*parts) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return _b64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: Optional[str], count: int) -> Optional[list]:
    """커서를 part 리스트로. 없거나 깨졌으면 None(=처음부터).

    ★ **모든 성분을 여기서 변환한다.** 타임스탬프만 검증하고 id 를 호출부에서
      `int()` 하면, `...|abc` 같은 커서가 이 관문을 통과해 호출부에서 ValueError →
      500 이 된다. '깨진 커서는 처음부터'라는 계약이 절반만 지켜지는 셈이다.
      성분이 늘면 이 함수에 변환을 함께 추가할 것.
        count=1 → [int]                 (보낸함: batch_id)
        count=2 → [datetime, int]       (받은함: 시각 + 보조키 id)
    """
    if not cursor:
        return None
    try:
        raw = _b64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    parts = raw.split("|")
    if len(parts) != count:
        return None
    try:
        if count == 1:
            parts[0] = int(parts[0])
        else:
            parts[0] = datetime.fromisoformat(parts[0])
            parts[1] = int(parts[1])
    except ValueError:
        return None
    return parts


def get_received_page(
    db: Session, nurse_id: str, limit: int = 20, cursor: Optional[str] = None
) -> dict:
    """받은 메시지 한 페이지. `{items, nextCursor, total}`.

    ★ `total` 은 페이지가 아니라 **받은함 전체 건수**다. 마지막 페이지에서도 같은 값이다.
    ★ 조회 자체는 읽음 처리를 하지 않는다(`mark_as_read` 는 별도 호출).
    """
    SenderNurse = aliased(Nurse)
    # ★ **outer join 이어야 한다.** inner 로 걸면 발신자가 `nurses` 에서 사라진 메시지가
    #   목록에서 빠지는데, 아래 `total` 은 조인 없이 세므로 둘이 어긋난다 —
    #   "전체 27건" 이라면서 끝까지 넘겨도 25건만 나오는 형태다. 무한 스크롤에서는
    #   마지막 페이지가 안 끝나는 것처럼 보인다.
    #   사람 정보가 없으면 이름·직함을 null 로 내려보내고, 메시지 자체는 살린다.
    q = (
        db.query(Message, SenderNurse)
        .outerjoin(SenderNurse, Message.sender_nurse_id == SenderNurse.nurse_id)
        .filter(Message.receiver_nurse_id == nurse_id)
    )
    key = _decode_cursor(cursor, 2)
    if key is not None:
        at, last_id = key[0], key[1]      # 변환·검증은 `_decode_cursor` 가 끝냈다
        # (created_at, id) 사전식 비교. 같은 시각이면 id 로 이어 받는다.
        q = q.filter(
            (Message.created_at < at)
            | ((Message.created_at == at) & (Message.id < last_id))
        )
    rows = (
        q.order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit + 1)              # 한 건 더 떠서 다음 페이지 유무를 판정
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [
        {
            "id": m.id,
            "sender": {
                "nurse_id": m.sender_nurse_id,
                # 발신자가 nurses 에 없으면 null. 아이디는 메시지 행에 있으므로 항상 준다.
                "name": s.name if s is not None else None,
                "role": s.role if s is not None else None,
            },
            "message": m.message,
            "message_img": m.message_img,
            "created_at": m.created_at,
            "is_read": bool(m.is_read),
            "read_at": m.read_at,
        }
        for m, s in rows
    ]
    next_cursor = (
        _encode_cursor(rows[-1][0].created_at.isoformat(), rows[-1][0].id)
        if has_more and rows
        else None
    )
    total = db.query(Message).filter(Message.receiver_nurse_id == nurse_id).count()
    return {"items": items, "nextCursor": next_cursor, "total": total}


def get_sent_page(
    db: Session, nurse_id: str, limit: int = 20, cursor: Optional[str] = None
) -> dict:
    """보낸 메시지 한 페이지 — **발송 묶음 단위**. `{items, nextCursor, total}`.

    22명에게 한 번 보내면 item 1개 · `recipient_count=22` · total 1건이다.

    ★ 묶음 키는 `batch_id` 다(= 그 발송 첫 행의 `id`). `create_message` 가 심는다.
      **컬럼 도입 전 데이터는 NULL** 이라 `COALESCE(batch_id, id)` 로 읽는다 —
      그 행들은 각각 단건 묶음이 된다(백필해 두면 COALESCE 는 그냥 통과한다).
    ★ 정수 키라 커서가 `Idx` 하나로 끝난다. 시각으로 묶던 이전 방식은 `datetime` 의
      3.33ms 절삭 때문에 서로 다른 발송이 합쳐질 수 있었다(`create_message` 주석).
    ★ limit 은 수신자 행이 아니라 **발송 수**에 건다. 그래서 2단계로 읽는다 —
      (1) 묶음 키를 limit+1 개 뽑고 (2) 그 묶음들의 수신자를 전부 가져온다.
      한 번에 읽으면 수신자 묶음이 페이지 경계에서 잘린다.
    """
    from sqlalchemy import func as _sa_func

    _bkey = _sa_func.coalesce(Message.batch_id, Message.id)

    # (1) 묶음 키
    gq = (
        db.query(_bkey.label("bid"))
        .filter(Message.sender_nurse_id == nurse_id)
        .group_by(_bkey)
    )
    key = _decode_cursor(cursor, 1)
    if key is not None:
        gq = gq.filter(_bkey < key[0])
    groups = gq.order_by(_bkey.desc()).limit(limit + 1).all()

    has_more = len(groups) > limit
    groups = groups[:limit]
    if not groups:
        return {"items": [], "nextCursor": None, "total": _sent_total(db, nurse_id)}

    # (2) 그 묶음들의 수신자 전부
    bids = [int(g.bid) for g in groups]
    ReceiverNurse = aliased(Nurse)
    # ★ **outer join 이어야 한다.** inner 로 걸면 `nurses` 에서 사라진 수신자의 행이
    #   빠지는데, 묶음 키는 (1)에서 조인 없이 뽑았으므로 어긋난다. 수신자가 전부
    #   사라진 묶음은 members 가 비어 **카드 자체가 목록에서 증발**하고,
    #   일부만 사라지면 `recipient_count` 가 실제 발송 인원보다 작게 나온다.
    #   사람 정보가 없으면 이름·직함만 null 로 두고 수신자 행은 유지한다.
    rows = (
        db.query(Message, ReceiverNurse, _bkey.label("bid"))
        .outerjoin(ReceiverNurse, Message.receiver_nurse_id == ReceiverNurse.nurse_id)
        .filter(Message.sender_nurse_id == nurse_id, _bkey.in_(bids))
        .order_by(Message.id.asc())
        .all()
    )
    by_bid: dict = {}
    for m, r, bid in rows:
        by_bid.setdefault(int(bid), []).append((m, r))

    items = []
    for g in groups:
        members = by_bid.get(int(g.bid)) or []
        if not members:
            continue                    # 그 사이 전부 삭제된 묶음
        head = members[0][0]
        items.append({
            "batch_id": int(g.bid),
            "message": head.message,
            "message_img": head.message_img,
            "created_at": head.created_at,
            "recipient_count": len(members),
            "recipients": [
                {
                    "message_id": m.id,
                    "nurse_id": m.receiver_nurse_id,
                    # 수신자가 nurses 에 없으면 null. 행은 살려 인원 수를 맞춘다.
                    "name": r.name if r is not None else None,
                    "role": r.role if r is not None else None,
                    "is_read": bool(m.is_read),
                    "read_at": m.read_at,
                }
                for m, r in members
            ],
        })
    next_cursor = (
        _encode_cursor(int(groups[-1].bid)) if has_more and groups else None
    )
    return {"items": items, "nextCursor": next_cursor, "total": _sent_total(db, nurse_id)}


def _sent_total(db: Session, nurse_id: str) -> int:
    """보낸함 total = **발송 횟수**(수신자 행 수가 아니다)."""
    from sqlalchemy import func as _sa_func

    return (
        db.query(_sa_func.coalesce(Message.batch_id, Message.id))
        .filter(Message.sender_nurse_id == nurse_id)
        .distinct()
        .count()
    )


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
