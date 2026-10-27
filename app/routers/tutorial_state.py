"""튜토리얼(사용 안내) 계정별 상태 — GET/PUT /tutorial/state.

★ 역할 검사를 하지 않는다 — .claude/rules/backend-routers.md 'Authorization: always verify roles' 의
  의도적 예외. 로그인한 본인 계정 행(office_id + account_id)만 읽고 쓰며 다른 사람·병동 데이터에
  닿지 않는다. 안내를 누구에게 보일지는 화면 설정값(roster_front tutorial.config.ts 의 roles)이 정한다.
★ 인증은 require_current_user(쿠키 인증 + 미인증 401) — 본보기 routers/roster_change_notice.py 와 같다.
★ 핸들러는 동기 def 로 둔다(비동기 핸들러 안에서 동기 DB 세션을 쓰면 이벤트 루프가 막혀 서버 전체가 멈춘다).
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db.client2 import get_db
from routers.auth import require_current_user
from schemas.auth_schema import User as UserSchema
from schemas.tutorial_state_schema import TutorialStatePutRequest, TutorialStateResponse
from services import tutorial_state_service

router = APIRouter(prefix="/tutorial", tags=["tutorial"])


@router.get("/state", response_model=TutorialStateResponse)
def get_tutorial_state(
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """계정 기준 사용 안내 상태(첫 접속 카드 노출·다음 달 안내 보기). 있는 키만 돌려준다."""
    return tutorial_state_service.get_state(db, current_user)


@router.put("/state", response_model=TutorialStateResponse)
def put_tutorial_state(
    payload: TutorialStatePutRequest,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """바꿀 키만 보낸다. null 이면 그 키를 지운다. 자기 계정 행만 바뀌고, 저장 뒤 전체 상태를 돌려준다."""
    return tutorial_state_service.put_state(db, current_user, payload.values)
