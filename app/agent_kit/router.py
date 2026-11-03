"""에이전트 공통 경로 — `/api/agents/{agent_id}/…`. 화면은 에이전트가 무엇이든 이 경로와 블록 어휘만 쓴다.

  GET  /{agent_id}/availability?…대상…   패널을 보일지·처음 화면(intro) — 꺼져 있으면 404, 못 쓰면 값으로
  POST /{agent_id}/turn                   한 턴 — 입력 봉투 {kind: text|action, …} → 블록·효과
  GET  /{agent_id}/conversations          메모리에 올라 있는 내 대화
  GET  /{agent_id}/conversations/{id}     대화 한 건의 기록(에이전트가 권한 검사)
  POST /{agent_id}/conversations/{id}/close

  · 에이전트마다 스위치(enabled) — 꺼지면 모든 경로 404(AIDE 채팅이 조건 없이 열렸던 P0 을 되풀이하지 않는다).
  · 경로 함수는 `def` 다 — 무거운 계산·DB 가 동기라 `async def` 면 이벤트 루프가 막혀 서버 전체가 멈춘다([PY-01]).
  · 오류 detail 은 {code, message} — 화면은 message 를 보여 주고 code 로만 갈래를 탄다.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agent_kit.base import Agent, TurnInput, get_agent
from agent_kit.conversation import ConversationError
from agent_kit.runtime import TurnError, run_turn, working
from db.client2 import get_db
from routers.auth import require_current_user
from schemas.auth_schema import User

router = APIRouter(prefix="/api/agents", tags=["agents"])

MAX_TEXT = 1000


class InputBody(BaseModel):
    kind: str = Field(..., min_length=1, max_length=20)
    text: Optional[str] = Field(None, max_length=MAX_TEXT)
    action: Optional[str] = Field(None, max_length=50)
    params: Dict[str, Any] = {}


class TurnBody(BaseModel):
    conversation_id: Optional[str] = Field(None, max_length=64)
    #: 화면이 지금 보는 대상(근무표 수정이면 {"schedule_id"}) — 에이전트가 검사한다
    context: Dict[str, Any] = {}
    input: InputBody


class TurnResponse(BaseModel):
    agent: str
    conversation_id: str
    turn_no: int
    context: Dict[str, Any]
    blocks: List[Dict[str, Any]]
    effects: List[Dict[str, Any]] = []
    status: Dict[str, Any] = {}


def _agent(agent_id: str) -> Agent:
    agent = get_agent(agent_id)
    if agent is None or not agent.enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return agent


def _fail(exc: TurnError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


#: 대상 검사 실패의 code — 대화 code(not_found 등)와 겹치지 않게 따로 둔다
_ACCESS_CODES = {403: "no_access", 404: "no_target"}


def _authorize(agent: Agent, db: Session, user: Any, context: Dict[str, Any]) -> Dict[str, Any]:
    """에이전트는 HTTPException(문장)으로 막는다 — 여기서 {code, message} 로 맞춘다."""
    try:
        return agent.authorize(db, user, context)
    except HTTPException as exc:
        if exc.status_code in _ACCESS_CODES and isinstance(exc.detail, str):
            raise HTTPException(status_code=exc.status_code,
                                detail={"code": _ACCESS_CODES[exc.status_code], "message": exc.detail}) from exc
        raise


@router.get("/{agent_id}/availability")
def availability(agent_id: str, request: Request, current_user: User = Depends(require_current_user),
                 db: Session = Depends(get_db)):
    """패널을 보일지 — 켜져 있으면 이 대상에서 쓸 수 있는지(오류 대신 값으로)와 처음 화면."""
    agent = _agent(agent_id)
    try:
        context = _authorize(agent, db, current_user, dict(request.query_params))
    except HTTPException as exc:
        if exc.status_code in _ACCESS_CODES:
            reason = exc.detail.get("message") if isinstance(exc.detail, dict) else exc.detail
            return {"enabled": True, "can_use": False, "reason": reason, "intro": None}
        raise
    return {"enabled": True, "can_use": True, "reason": None, "intro": agent.intro(db, current_user, context)}


@router.post("/{agent_id}/turn", response_model=TurnResponse)
def turn(agent_id: str, body: TurnBody, current_user: User = Depends(require_current_user),
         db: Session = Depends(get_db)):
    agent = _agent(agent_id)
    deadline = time.monotonic() + agent.budget_seconds          # 들어온 순간부터 센다
    inp = TurnInput(kind=body.input.kind, text=(body.input.text or "").strip() or None,
                    action=body.input.action, params=dict(body.input.params or {}))
    if inp.kind not in agent.accepts:
        raise HTTPException(status_code=400, detail={"code": "unsupported_input",
                                                     "message": "이 도우미가 받지 않는 입력입니다."})
    if inp.kind == "text" and not inp.text:
        raise HTTPException(status_code=400, detail={"code": "empty", "message": "바꿀 내용을 적어 주세요."})
    if inp.kind == "action" and not inp.action:
        raise HTTPException(status_code=400, detail={"code": "bad_action", "message": "알 수 없는 동작입니다."})
    context = _authorize(agent, db, current_user, body.context)
    try:
        # 끝난 대화는 말이면 새 대화로 이어 받는다(화면이 다시 보낼 필요 없이) — 버튼은 그 대화의 결과라 거절
        with working(agent, db, current_user, body.conversation_id, context, deadline,
                     renew=inp.kind == "text") as (conv, renewed):
            return run_turn(agent, db, current_user, conv, inp, context=context, deadline=deadline, renewed=renewed)
    except TurnError as exc:
        raise _fail(exc) from exc


@router.get("/{agent_id}/conversations")
def my_conversations(agent_id: str, current_user: User = Depends(require_current_user)):
    """메모리에 올라 있는 내 대화(이어 가기용). 내려간 대화도 id 로 부르면 기록에서 다시 선다."""
    agent = _agent(agent_id)
    return {"conversations": [c.info() for c in agent.store.active(current_user.account_id)]}


@router.get("/{agent_id}/conversations/{conversation_id}")
def conversation_detail(agent_id: str, conversation_id: str, current_user: User = Depends(require_current_user),
                        db: Session = Depends(get_db)):
    agent = _agent(agent_id)
    detail = agent.transcript(db, current_user, conversation_id)
    live = [c.info() for c in agent.store.active() if c.id == conversation_id]
    return {"conversation": live[0] if live else {"conversation_id": conversation_id, "in_memory": False}, **detail}


@router.post("/{agent_id}/conversations/{conversation_id}/close", status_code=204)
def close(agent_id: str, conversation_id: str, current_user: User = Depends(require_current_user),
          db: Session = Depends(get_db)):
    """닫기 — 처리 중인 턴이 있으면 409(그 턴이 적용이면 닫은 대화에서 결과가 생긴다)."""
    agent = _agent(agent_id)
    try:   # 만료·턴 초과여도 닫을 수 있게 한도는 보지 않는다
        conv = agent.store.checkout(db, conversation_id, current_user.account_id, check_limits=False)
    except ConversationError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    try:
        agent.on_close(db, current_user, conv)
        agent.store.close(conversation_id)
    finally:
        agent.store.checkin(conv)
