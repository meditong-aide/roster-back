"""Agent Floating Chat — 인증된 사용자 컨텍스트 자동 활용.

`/agent/test/*` (dev tool) 와 별개로, 실 서비스 페이지의 floating widget 이
호출하는 production 라우터.

특징:
  - get_current_user_from_cookie 의존성 — 로그인 안 한 사용자는 401
  - SessionContext (office_id/group_id/nurse_id/role) 를 인증 user 에서 자동 구성
  - conversation_id 는 클라이언트에서 sessionStorage 로 관리 (페이지 reload 안전)
  - SessionMemoryRepo (MSSQL SOT + Redis hot cache, 24h sliding) 자동 적용
  - 모든 skill 호출은 AgentSkillInvocation 에 audit 됨
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.conversation import ConversationStore
from agents_v2.llm_client import get_llm_client
from agents_v2.schemas.session_context import SessionContext
from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent/chat", tags=["agent_chat"])

_store: Optional[ConversationStore] = None
_agent: Optional[SchedulingAgent] = None


def _get_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store


def _get_agent() -> SchedulingAgent:
    global _agent
    if _agent is None:
        _agent = SchedulingAgent(get_llm_client())
    return _agent


def _resolve_role(user: User) -> str:
    if user.is_master_admin:
        return "ADM"
    if user.is_head_nurse or (user.hn_auth or "").upper() == "HN":
        return "HN"
    return "NURSE"


def _build_session_ctx(
    user: User, conv_id: str, year: Optional[int], month: Optional[int]
) -> SessionContext:
    today = date.today()
    return SessionContext(
        office_id=user.office_id,
        group_id=user.group_id,
        year=year or today.year,
        month=month or today.month,
        user_role=_resolve_role(user),
        nurse_id=user.nurse_id,
        nurse_name=user.name,
        conversation_id=conv_id,
    )


# ── Schemas ──────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    year: Optional[int] = None
    month: Optional[int] = None


class ChatResponse(BaseModel):
    answer: str
    conversation_id: str
    awaiting_approval: bool = False
    preview: Optional[dict] = None


class WhoAmIResponse(BaseModel):
    nurse_id: str
    name: str
    group_id: str
    office_id: str
    role: str


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/whoami", response_model=WhoAmIResponse)
async def whoami(
    current_user: Optional[User] = Depends(get_current_user_from_cookie),
):
    """현재 로그인 사용자 컨텍스트 — floating widget 초기화용."""
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다.",
        )
    return WhoAmIResponse(
        nurse_id=current_user.nurse_id,
        name=current_user.name,
        group_id=current_user.group_id,
        office_id=current_user.office_id,
        role=_resolve_role(current_user),
    )


@router.post("/send", response_model=ChatResponse)
async def send_message(
    req: ChatRequest,
    current_user: Optional[User] = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """인증된 사용자 메시지를 agent 에 전달하고 응답 반환.

    SessionContext 는 current_user 에서 자동 구성. conversation_id 가 비어 있으면
    새 UUID 발급 (sessionStorage 에 저장하여 다음 요청에 재사용).
    """
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다.",
        )

    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="메시지가 비어 있습니다.")

    conv_id = req.conversation_id or str(uuid.uuid4())
    store = _get_store()
    agent = _get_agent()

    # 세션 로드 (Redis HIT / MSSQL fallback)
    conv = store.get_or_create(
        db, conv_id,
        user_id=current_user.nurse_id,
        group_id=current_user.group_id,
    )

    # SessionContext 자동 구성 + 이전 상태 복원
    ctx = _build_session_ctx(current_user, conv_id, req.year, req.month)
    ctx.messages = conv.messages
    ctx.variable_memory = conv.variable_memory or {}
    ctx.pending_approval = conv.pending_approval

    # Agent run
    try:
        result = agent.run(db, req.message, ctx)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[chat] agent.run failed conv_id=%s", conv_id)
        raise HTTPException(
            status_code=500,
            detail=f"처리 중 오류가 발생했습니다: {exc}",
        ) from exc

    # 세션 영속화 (write-through MSSQL + Redis)
    try:
        store.save_messages(
            db, conv_id, result.messages,
            user_id=current_user.nurse_id,
            group_id=current_user.group_id,
        )
        if result.variable_memory:
            store.save_variable_memory(
                db, conv_id, result.variable_memory,
                user_id=current_user.nurse_id,
                group_id=current_user.group_id,
            )
        # pending_approval — preview 가 있고 awaiting 인 경우만 set, 아니면 None 으로 clear
        pending = result.preview if result.awaiting_approval else None
        store.set_pending_approval(
            db, conv_id, pending,
            user_id=current_user.nurse_id,
            group_id=current_user.group_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[chat] save failed (silent) conv_id=%s: %s", conv_id, exc)

    return ChatResponse(
        answer=result.answer or "",
        conversation_id=conv_id,
        awaiting_approval=bool(result.awaiting_approval),
        preview=result.preview if result.awaiting_approval else None,
    )


@router.post("/reset", status_code=204)
async def reset_conversation(
    conversation_id: str,
    current_user: Optional[User] = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """세션을 강제 리셋 (pending_approval clear). 새 conversation_id 로 시작 권장."""
    if current_user is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    store = _get_store()
    try:
        store.set_pending_approval(
            db, conversation_id, None,
            user_id=current_user.nurse_id,
            group_id=current_user.group_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[chat] reset failed conv_id=%s: %s", conversation_id, exc)
    return None
