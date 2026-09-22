from typing import List, Optional
from datetime import datetime

from sqlalchemy.orm import Session, aliased

from db.models import Message, Nurse, Group
from schemas.auth_schema import User as UserSchema
from services.group_access import resolve_home_group_id, resolve_managed_group_ids
from services.nurse_service import get_profile_image_url


def resolve_recipient_group_ids(db: Session, current_user: UserSchema) -> List[str]:
    """메시지를 보낼 수 있는 병동 목록.

    - 그룹관리자(HN)·ADM: `resolve_managed_group_ids` — 자기가 관리하는 병동 전부.
      `3병동-RN` 근무자가 `3병동-AN` 에 못 보내던 문제는 관리자가 두 병동을 다
      맡고 있으면 여기서 풀린다.
    - 일반 근무자: **자기 병동 하나뿐**이다.

    ★★ 종전에는 호출자가 `extra_group_ids` 로 병동을 **마음대로 덧붙일 수 있었다.**
      서버는 "같은 office 인가" 만 봤지 "이 사람이 그 병동을 볼 수 있는가" 는 보지
      않았다 — 일반 근무자도 파라미터 하나로 병원 전 병동 명단을 훑을 수 있었다.
      범위를 **호출자에게서 유도**하면 그 구멍이 닫힌다.
    ★ 일반 근무자는 토큰의 `group_id` 가 아니라 **DB home** 을 쓴다. 토큰 쪽은
      병동 전환으로 바뀔 수 있어 소속과 어긋날 수 있다.
    """
    managed = resolve_managed_group_ids(db, current_user)
    if managed:
        return managed
    home = resolve_home_group_id(db, current_user) or (current_user.group_id or "")
    return [home] if home else []


#: `LIKE` 에서 뜻을 가지는 문자. 사용자가 친 그대로 찾게 하려면 탈출시켜야 한다.
#: `%` 하나만 쳐도 전원이 걸리고, `[` 는 문자 집합으로 읽혀 엉뚱한 결과가 나온다.
_LIKE_ESCAPE = "\\"


def _like_contains(term: str) -> str:
    """부분일치용 패턴. 특수문자를 탈출시켜 **친 글자 그대로** 찾는다."""
    for ch in (_LIKE_ESCAPE, "%", "_", "["):
        term = term.replace(ch, _LIKE_ESCAPE + ch)
    return f"%{term}%"


def _member_rows_query(db: Session, group_ids: List[str], q: Optional[str] = None):
    """수신자 후보 기본 질의. 목록·페이지·발송 가드가 **모두 이 축을 쓴다.**

    ★ `q` 는 이름·직급·병동 이름 부분일치다 — 화면이 하던 것과 **같은 세 필드**다.
      세 컬럼 모두 `Korean_Wansung_CI_AS` 라 대소문자는 이미 무시된다. `lower()` 를
      씌우면 계산식이 되어 collation 이점만 잃는다.
    ★ 파라미터가 아니라 **컬럼의 collation 이 이긴다**(collation 우선순위). 그래서
      Latin1 이 기본인 이 DB 에서도 한글 비교가 어긋나거나 468 로 터지지 않는다.
    """
    query = (
        db.query(Nurse, Group.group_name)
        .join(Group, Nurse.group_id == Group.group_id)
        .filter(Nurse.group_id.in_(group_ids), Nurse.active == 1)
    )
    term = (q or "").strip()
    if term:
        pattern = _like_contains(term)
        query = query.filter(
            Nurse.name.like(pattern, escape=_LIKE_ESCAPE)
            | Nurse.role.like(pattern, escape=_LIKE_ESCAPE)
            | Group.group_name.like(pattern, escape=_LIKE_ESCAPE)
        )
    return query


def _member_item(n: Nurse, group_name: str) -> dict:
    return {
        "nurse_id": n.nurse_id,
        "name": n.name,
        "role": n.role,
        "level_": n.level_,
        "group_id": n.group_id,
        "group_name": group_name,
        # 프로필 사진 미등록이면 null — 프론트가 기본 아바타로 대체한다.
        "profile_image_url": get_profile_image_url(n.profile_image_key),
    }


def get_member_list(db: Session, group_ids: List[str]) -> List[dict]:
    """메시지 수신자 목록(전량). 범위는 호출부가 `resolve_recipient_group_ids` 로 정한다."""
    if not group_ids:
        return []
    rows = (
        _member_rows_query(db, group_ids)
        .order_by(Group.group_name, Nurse.sequence, Nurse.nurse_id)
        .all()
    )
    return [_member_item(n, group_name) for n, group_name in rows]


def get_member_page(
    db: Session,
    group_ids: List[str],
    limit: int = 20,
    cursor: Optional[str] = None,
    q: Optional[str] = None,
) -> dict:
    """수신자 한 페이지. `{items, nextCursor, total}` — 받은함과 같은 계약.

    ★★ `q` 는 **서버에서** 거른다. 화면이 받은 만큼만 거르면 아직 안 받은 사람이
      "검색 결과 없음" 으로 나와, 사용자는 그 사람이 없다고 믿는다. 검색을 서버로
      올리면 화면이 전량을 들고 있지 않아도 결과가 정확하다.
    ★ `total` 도 **거른 뒤 수**다. 검색 중에 "131명 중" 같은 숫자가 뜨면 안 된다.
    ★ 커서는 `q` 가 같을 때만 유효하다. 정렬이 같아 이어받기는 성립하지만,
      검색어를 바꾸면 화면이 커서를 버리고 처음부터 받아야 한다.

    ★ 정렬은 `(병동이름, sequence, nurse_id)` 다. `sequence` 는 병동 안에서만 유일해
      보조키 없이 커서를 만들면 같은 값에서 **건너뛰거나 되풀이**된다.
    ★★ 커서에 **병동 이름을 담지 않는다.** `_encode_cursor` 가 `|` 로 이어 붙이므로
      이름에 `|` 가 들어가면 복호가 어긋나고, 그러면 `None`(=처음부터)이 되어
      무한 스크롤이 같은 페이지를 영원히 반복한다. 대신 `group_id` 를 담고
      이름은 이 함수가 다시 찾는다 — `group_id` 는 생성된 식별자라 `|` 가 없다.
    """
    if not group_ids:
        return {"items": [], "nextCursor": None, "total": 0}

    names = {
        g.group_id: g.group_name
        for g in db.query(Group.group_id, Group.group_name)
        .filter(Group.group_id.in_(group_ids))
        .all()
    }

    rows_q = _member_rows_query(db, group_ids, q)
    key = _decode_cursor(cursor, 3)
    if key is not None:
        last_gid, last_seq, last_nid = key[0], key[1], key[2]
        last_name = names.get(last_gid)
        if last_name is not None:
            # (이름, sequence, nurse_id) 사전식 비교로 이어 받는다.
            rows_q = rows_q.filter(
                (Group.group_name > last_name)
                | (
                    (Group.group_name == last_name)
                    & (
                        (Nurse.sequence > last_seq)
                        | ((Nurse.sequence == last_seq) & (Nurse.nurse_id > last_nid))
                    )
                )
            )

    rows = (
        rows_q.order_by(Group.group_name, Nurse.sequence, Nurse.nurse_id)
        .limit(limit + 1)             # 한 건 더 떠서 다음 페이지 유무를 판정
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last_n = rows[-1][0]
        next_cursor = _encode_cursor(
            last_n.group_id, last_n.sequence, last_n.nurse_id
        )

    return {
        "items": [_member_item(n, group_name) for n, group_name in rows],
        "nextCursor": next_cursor,
        # ★ 페이지가 아니라 **조건에 맞는 전체 인원**이다(검색 중이면 거른 뒤 수).
        #   마지막 페이지에서도 같은 값이다.
        "total": _member_rows_query(db, group_ids, q).count(),
    }


def get_available_groups(db: Session, group_ids: List[str]) -> List[dict]:
    """수신자를 고를 수 있는 병동 목록. **범위는 호출부가 정한다.**

    ★ 종전에는 office 전체를 돌려줬다. 고를 수 없는 병동까지 화면에 뜨고, 그 자체가
      병원 조직도를 흘린다. 이제 `resolve_recipient_group_ids` 결과만 담는다.
    """
    if not group_ids:
        return []
    groups = (
        db.query(Group)
        .filter(Group.group_id.in_(group_ids))
        .order_by(Group.group_name)
        .all()
    )
    return [{"group_id": g.group_id, "group_name": g.group_name} for g in groups]


def create_message(
    db: Session,
    office_id: str,
    sender_nurse_id: str,
    receiver_nurse_ids: List[str],
    message: Optional[str],
    message_img: Optional[str],
    allowed_group_ids: Optional[List[str]] = None,
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
    # ★★ 수신자가 **발신자와 같은 office 소속인지** 반드시 확인한다.
    #   종전에는 검사가 없어 임의 `nurse_id` 를 보내면 그대로 행이 만들어졌고,
    #   `get_message_list` 는 `office_id` 없이 `nurse_id` 로만 거르므로 그 메시지가
    #   **다른 병원 간호사 받은함에 실제로 도착했다.** 화면이 자기 병동만 보여줘서
    #   드러나지 않았을 뿐이라, 수신자 범위를 넓히면 바로 닿는 자리다.
    # ★ 조용히 거르지 않고 막는다 — 걸러 버리면 "보냈는데 일부는 안 갔다" 가 되어
    #   발신자도 수신자도 그 사실을 알 수 없다.
    # ★★ 판정축은 **수신자 목록과 똑같아야 한다.** `_member_rows_query` 를 그대로
    #   쓰는 이유다 — 축이 갈리면 "목록에는 떴는데 보내면 막히는" 사람이 생긴다.
    #   `Nurse.office_id`(비정규화 컬럼)로 재면 안 된다. `add_nurses_to_group_service`
    #   가 기존 간호사를 옮길 때 `group_id` 만 바꿔서 그 값은 낡을 수 있다
    #   (현재 데이터 불일치는 0건이지만 구조가 그렇다).
    # ★ 범위가 안 넘어오면 office 전체로 재지 않고 **차단한다.** 기본값을 넓게 두면
    #   호출부가 빠뜨렸을 때 조용히 열린다.
    _scope = allowed_group_ids or []
    _valid = {
        n.nurse_id
        for n, _ in _member_rows_query(db, _scope)
        .filter(Nurse.nurse_id.in_(receiver_nurse_ids))
        .all()
    } if _scope else set()
    # ★ 어느 id 가 걸렸는지 **되돌려주지 않는다.** 목록에서 빠진 것이 곧 "내 office
    #   소속" 이라는 뜻이라, 되돌려주면 id 를 훑어 소속을 알아낼 수 있다.
    if any(r not in _valid for r in receiver_nurse_ids):
        raise ValueError("수신자 중 보낼 수 없는 대상이 있습니다.")

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


def get_message(db: Session, message_id: int, nurse_id: str) -> Optional[dict]:
    """단건 조회 — **당사자(발신자·수신자)만**. 아니면 None.

    ★ 당사자 조건이 없으면 `id` 는 IDENTITY 연번이라 로그인만 한 사람이 번호를
      올려가며 남의 메시지 본문을 그대로 읽는다(실측: 제3자 계정으로 200).
      `delete_message` 는 처음부터 같은 조건을 걸고 있었고 여기만 빠져 있었다.
    ★ 없는 메시지와 남의 메시지를 **같은 None** 으로 돌린다 — 404/403 을 갈라
      주면 존재 여부가 새고, 그 자체가 열람 대상 목록이 된다.
    """
    SenderNurse = aliased(Nurse)
    ReceiverNurse = aliased(Nurse)

    row = (
        db.query(Message, SenderNurse, ReceiverNurse)
        .join(SenderNurse, Message.sender_nurse_id == SenderNurse.nurse_id)
        .join(ReceiverNurse, Message.receiver_nurse_id == ReceiverNurse.nurse_id)
        .filter(
            Message.id == message_id,
            (Message.sender_nurse_id == nurse_id)
            | (Message.receiver_nurse_id == nurse_id),
        )
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
        count=3 → [str, int, str]       (수신자: group_id + sequence + nurse_id)
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
        elif count == 3:
            # [group_id(str), sequence(int), nurse_id(str)] — 문자열 성분은 그대로 둔다.
            parts[1] = int(parts[1])
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
                # 프로필 사진 미등록이면 null — 프론트가 기본 아바타로 대체한다.
                "profile_image_url": (
                    get_profile_image_url(s.profile_image_key) if s is not None else None
                ),
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
                    # 프로필 사진 미등록이면 null — 프론트가 기본 아바타로 대체한다.
                    "profile_image_url": (
                        get_profile_image_url(r.profile_image_key) if r is not None else None
                    ),
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
        # 프로필 사진 미등록이면 null — 프론트가 기본 아바타로 대체한다.
        "sender_profile_image_url": get_profile_image_url(sender.profile_image_key),
        "receiver_nurse_id": msg.receiver_nurse_id,
        "receiver_name": receiver.name,
        "receiver_role": receiver.role,
        "receiver_profile_image_url": get_profile_image_url(receiver.profile_image_key),
        "message": msg.message,
        "message_img": msg.message_img,
        "is_read": msg.is_read,
        "created_at": msg.created_at,
        "read_at": msg.read_at,
    }
