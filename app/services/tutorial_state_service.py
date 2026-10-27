"""튜토리얼(사용 안내) 계정별 상태 — 계정·키 1개 = 1행(`tutorial_user_state`).

★ 로그인한 본인 계정 행(office_id + account_id)만 읽고 쓴다. 다른 사람·병동 데이터에 닿지 않는다.
★ 진행 위치·완료 여부는 저장하지 않는다(기획 2-5) — 첫 접속 카드 노출과 '다음 달 안내 보기' 선택만 남긴다.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import TutorialUserState
from schemas.auth_schema import User as UserSchema


def get_state(db: Session, user: UserSchema) -> dict:
    """이 계정에 저장된 상태 — `{"values": {키: 값}}` (있는 키만)."""
    rows = (
        db.query(TutorialUserState.state_key, TutorialUserState.state_value)
        .filter(TutorialUserState.office_id == user.office_id,
                TutorialUserState.account_id == user.account_id)
        .limit(50)   # 전수 조회 금지 규칙 — 계정당 키가 몇 개뿐이라 넉넉한 상한
        .all()
    )
    return {"values": {key: value for key, value in rows}}


def put_state(db: Session, user: UserSchema, values: dict[str, Optional[str]]) -> dict:
    """바꾼 키만 upsert, 값이 None 이면 그 키의 행을 삭제한다. 저장 뒤 전체 상태를 돌려준다.

    ★ 같은 계정이 두 탭에서 동시에 처음 저장하면 PK 충돌(IntegrityError)이 난다 — rollback 뒤 **한 번만** 다시 한다.
      두 번째 시도에는 상대가 넣은 행이 보여 갱신으로 수렴한다.
    """
    for attempt in (1, 2):
        now = datetime.now()   # 기존 코드와 같은 naive 로컬 시각(roster_change_notice_service.ack_notice)
        for key, value in values.items():
            row = db.get(TutorialUserState, (user.office_id, user.account_id, key))
            if row is None and value is not None:       # 없던 키를 새로 저장
                db.add(TutorialUserState(office_id=user.office_id, account_id=user.account_id,
                                         state_key=key, state_value=value, updated_at=now))
            elif row is not None and value is None:     # null = 그 키를 지움
                db.delete(row)
            elif row is not None:                       # 있던 키를 갱신
                row.state_value, row.updated_at = value, now
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise
    return get_state(db, user)
