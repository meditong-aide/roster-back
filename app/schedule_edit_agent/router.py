"""근무표 수정 도우미 채팅 경로 — `/api/schedule-edit`.

  · 기본으로 꺼져 있다 — `SCHEDULE_EDIT_AGENT_ENABLED=1` 일 때만 응답(아니면 404). AIDE 채팅이 조건 없이 열렸던
    일(P0)을 되풀이하지 않는다.
  · 수간호사·그룹관리자·ADM 만(`caller_is_manager` · DB 기준) + 그 근무표 병동에 접근 가능해야 한다.
  · 경로 함수는 `def` 다 — CP-SAT·DB 가 동기라 `async def` 면 이벤트 루프가 막혀 서버 전체가 멈춘다([PY] 규칙).
  · 같은 대화는 한 번에 하나(409), 서버 전체 동시 풀이는 SCHEDULE_EDIT_MAX_CONCURRENCY 개(기본 2 · 넘으면 429).
  · 응답 시간은 경로에 들어온 순간부터 CHAT_BUDGET(25초) — 대기·판정 재료 로드·LLM·솔버 모두 이 안이다.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.client2 import get_db
from db.models import Schedule
from routers.auth import require_current_user
from schedule_edit_agent.agent import EditAgent, close_conversation
from schedule_edit_agent.llm import OpenAIEditLLM
from schedule_edit_agent.skills import CHAT_BUDGET, MIN_SOLVE, SkillError
from schedule_edit_agent.store import Conversation, ConversationError, ConversationStore
from schemas.auth_schema import User
from services.group_access import assert_caller_can_access_group, caller_is_manager
from services.schedule_edit.journal import read_conversation, read_conversation_head

KST = timezone(timedelta(hours=9))
SLOT_WAIT_SECONDS = 10.0

router = APIRouter(prefix="/api/schedule-edit", tags=["schedule_edit_agent"])

_store = ConversationStore()
_agent = EditAgent(OpenAIEditLLM)
_slots = threading.BoundedSemaphore(max(1, int(os.getenv("SCHEDULE_EDIT_MAX_CONCURRENCY") or 2)))


def _enabled() -> None:
    if os.getenv("SCHEDULE_EDIT_AGENT_ENABLED") != "1":
        raise HTTPException(status_code=404, detail="Not Found")


# ── 요청·응답 ───────────────────────────────────────────────────────────────


class ChatBody(BaseModel):
    schedule_id: str
    message: str = Field(..., min_length=1, max_length=1000)
    conversation_id: Optional[str] = None


class ActionBody(BaseModel):
    conversation_id: str
    action: Literal["apply_edit", "reject_candidate", "keep_candidate", "choose_lever"]
    rank: Optional[int] = None
    lever_id: Optional[str] = None
    name: Optional[str] = Field(None, max_length=100)
    request_text: Optional[str] = Field(None, max_length=1000)


class EditChatResponse(BaseModel):
    """AIDE `ChatResponse` 와 같은 칸 + 카드(preview)·화면 지시(ui_actions)·대화 상태(data)."""

    answer: str
    conversation_id: str
    awaiting_approval: bool = False
    preview: Optional[Dict[str, Any]] = None
    ui_actions: List[Dict[str, Any]] = []
    data: Dict[str, Any] = {}


# ── 경로 ────────────────────────────────────────────────────────────────────


@router.post("/chat", response_model=EditChatResponse, dependencies=[Depends(_enabled)])
def chat(body: ChatBody, current_user: User = Depends(require_current_user), db: Session = Depends(get_db)):
    deadline = time.monotonic() + CHAT_BUDGET          # 들어온 순간부터 센다(검토 HIGH-2)
    schedule = _authorize(db, current_user, body.schedule_id)
    with _working(db, current_user, body.conversation_id, schedule.schedule_id, deadline) as conv:
        if conv.schedule_id != schedule.schedule_id:
            # 화면에서 다른 버전으로 옮겨 갔다 — 그 버전에서 새 요청으로 이어 간다(잠근 뒤에 바꾼다 · 검토 M1)
            conv.schedule_id, conv.session, conv.restored = schedule.schedule_id, None, None
        reply = _agent.chat(db, current_user, conv, body.message.strip(), _today(), deadline=deadline)
    return EditChatResponse(**reply.__dict__)


@router.post("/action", response_model=EditChatResponse, dependencies=[Depends(_enabled)])
def action(body: ActionBody, current_user: User = Depends(require_current_user), db: Session = Depends(get_db)):
    deadline = time.monotonic() + CHAT_BUDGET
    params = {k: v for k, v in body.model_dump().items() if k not in ("conversation_id", "action") and v is not None}
    with _working(db, current_user, body.conversation_id, None, deadline) as conv:
        _authorize(db, current_user, conv.schedule_id)
        try:
            reply = _agent.act(db, current_user, conv, body.action, params, _today(), deadline=deadline)
        except SkillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return EditChatResponse(**reply.__dict__)


@router.get("/conversations", dependencies=[Depends(_enabled)])
def my_conversations(current_user: User = Depends(require_current_user)):
    """메모리에 올라 있는 내 대화(이어 가기용). 내려간 대화도 id 로 부르면 기록에서 다시 선다."""
    return {"conversations": [c.info() for c in _store.active(current_user.account_id)]}


@router.get("/conversation/{conversation_id}", dependencies=[Depends(_enabled)])
def conversation_detail(conversation_id: str, current_user: User = Depends(require_current_user),
                        db: Session = Depends(get_db)):
    """대화 한 건의 모든 턴(기록 그대로) + 지금 상태. 본인 대화(지금도 관리자일 때), 또는 그 병동에 접근 가능한 ADM."""
    if not caller_is_manager(db, current_user):        # 권한이 없어진 사람은 자기 대화도 못 본다(검토 L2)
        raise HTTPException(status_code=403, detail="근무표 수정 도우미는 수간호사·그룹관리자만 쓸 수 있습니다.")
    head = read_conversation_head(db, conversation_id)
    rows = read_conversation(db, conversation_id)
    if head is None or not rows:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다.")
    if str(head.created_by or "") != current_user.account_id:
        if not current_user.is_master_admin:
            raise HTTPException(status_code=403, detail="다른 사람의 대화입니다.")
        _authorize(db, current_user, head.schedule_id)
    live = [c.info() for c in _store.active() if c.id == conversation_id]
    return {
        "conversation": live[0] if live else {"conversation_id": conversation_id, "in_memory": False},
        "turns": [{"log_id": r.log_id, "turn_no": r.turn_no, "event": r.event, "schedule_id": r.schedule_id,
                   "new_schedule_id": r.new_schedule_id, "elapsed_ms": r.elapsed_ms,
                   "created_at": r.created_at.isoformat() if r.created_at else None, "payload": r.payload}
                  for r in rows],
    }


@router.post("/conversation/{conversation_id}/close", status_code=204, dependencies=[Depends(_enabled)])
def close(conversation_id: str, current_user: User = Depends(require_current_user), db: Session = Depends(get_db)):
    """닫기 — 처리 중인 턴이 있으면 409(그 턴이 적용이면 닫은 대화에서 새 버전이 생긴다 · 검토 L3)."""
    conv = _checkout(db, current_user, conversation_id, None, check_limits=False)
    try:
        close_conversation(db, current_user, conv)
        _store.close(conversation_id)
    finally:
        _store.checkin(conv)


# ── 내부 ────────────────────────────────────────────────────────────────────


def _authorize(db: Session, user: User, schedule_id: str) -> Schedule:
    if not caller_is_manager(db, user):
        raise HTTPException(status_code=403, detail="근무표 수정 도우미는 수간호사·그룹관리자만 쓸 수 있습니다.")
    schedule = db.query(Schedule).filter(Schedule.schedule_id == schedule_id).first()
    if schedule is None or getattr(schedule, "dropped", False):
        raise HTTPException(status_code=404, detail="근무표를 찾을 수 없습니다.")
    if not schedule.group_id:
        # 병동 없는 근무표는 접근 검사가 통과돼 버린다(group_id 가 비면 통과) — 여기서는 막는다(검토 L4)
        raise HTTPException(status_code=403, detail="병동이 없는 근무표는 수정 도우미로 다룰 수 없습니다.")
    assert_caller_can_access_group(db, user, schedule.group_id)
    return schedule


def _checkout(db: Session, user: User, conversation_id: Optional[str], schedule_id: Optional[str], *,
              check_limits: bool = True) -> Conversation:
    """대화를 잠가서 꺼낸다(없으면 새로). 반드시 `_store.checkin` 으로 푼다."""
    if not conversation_id:
        return _store.start(user.account_id, schedule_id)
    try:
        return _store.checkout(db, conversation_id, user.account_id, check_limits=check_limits)
    except ConversationError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message}) from exc


@contextmanager
def _working(db: Session, user: User, conversation_id: Optional[str], schedule_id: Optional[str],
             deadline: float):
    """대화 잠금 + 서버 풀이 슬롯. 슬롯 대기도 기한 안에서만(남은 시간이 솔버 최소보다 적으면 바로 429)."""
    conv = _checkout(db, user, conversation_id, schedule_id)
    try:
        wait = min(SLOT_WAIT_SECONDS, deadline - time.monotonic() - MIN_SOLVE)
        if wait <= 0 or not _slots.acquire(timeout=wait):
            if not conversation_id:
                _store.close(conv.id)     # 아무것도 못 한 새 대화는 남기지 않는다(목록·사람당 한도)
            raise HTTPException(status_code=429, detail="다른 수정 요청을 처리하고 있습니다. 잠시 뒤 다시 보내 주세요.")
        try:
            yield conv
        finally:
            _slots.release()
    finally:
        _store.checkin(conv)


def _today():
    return datetime.now(KST).date()
