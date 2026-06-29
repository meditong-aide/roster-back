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
from agents_v2.llm_client import get_llm_client, get_router_llm_client
from agents_v2.schemas.session_context import SessionContext
from agents_v2.security import (
    InputVerdict,
    check_output,
    classify_input,
)
from db.client2 import get_db
from db.models import Group, Office
from routers.auth import get_current_user_from_cookie
from services.group_access import resolve_managed_group_ids
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
        # router_llm 주입 → 2단계 tool 스코핑 ON (prod). 라우터는 저렴·빠른 전용 모델
        # (gpt-5.4-nano, 벤치 100% 정확도/최저가). 메인 turn 은 gpt-5.5.
        _agent = SchedulingAgent(
            get_llm_client(), router_llm=get_router_llm_client()
        )
    return _agent


def _resolve_role(user: User) -> str:
    if user.is_master_admin:
        return "ADM"
    if user.is_head_nurse or (user.hn_auth or "").upper() == "HN":
        return "HN"
    return "NURSE"


def _resolve_org_names(
    db: Session, office_id: Optional[str], group_id: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """병동/병원 id → 사람이 읽는 이름. 조회 실패해도 채팅은 막지 않는다."""
    group_name: Optional[str] = None
    office_name: Optional[str] = None
    try:
        if group_id:
            group_name = (
                db.query(Group.group_name)
                .filter(Group.group_id == group_id)
                .scalar()
            )
        if office_id:
            office_name = (
                db.query(Office.office_name)
                .filter(Office.office_id == office_id)
                .scalar()
            )
    except Exception:  # noqa: BLE001 — 이름 조회 실패가 채팅을 깨면 안 됨
        logger.warning(
            "[chat] org name lookup failed office_id=%s group_id=%s",
            office_id, group_id, exc_info=True,
        )
    return group_name, office_name


def _resolve_managed_group_names(
    db: Session, user: User, current_group_id: Optional[str]
) -> list[str]:
    """HN 이 관리하는 병동 이름들. 단일(현재 병동뿐)이면 빈 리스트(중복 안내 방지)."""
    try:
        managed_ids = resolve_managed_group_ids(db, user)
        if len(managed_ids) <= 1:
            return []
        rows = (
            db.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(managed_ids))
            .all()
        )
        # resolve_managed_group_ids 순서(home 우선) 유지
        by_id = {str(gid): name for gid, name in rows}
        return [by_id[gid] for gid in managed_ids if gid in by_id]
    except Exception:  # noqa: BLE001 — 관리 병동 조회 실패가 채팅을 깨면 안 됨
        logger.warning(
            "[chat] managed group lookup failed nurse_id=%s", user.nurse_id,
            exc_info=True,
        )
        return []


def _build_session_ctx(
    db: Session, user: User, conv_id: str, year: Optional[int], month: Optional[int]
) -> SessionContext:
    today = date.today()
    role = _resolve_role(user)
    group_name, office_name = _resolve_org_names(db, user.office_id, user.group_id)
    managed_group_names = (
        _resolve_managed_group_names(db, user, user.group_id) if role == "HN" else []
    )
    return SessionContext(
        office_id=user.office_id,
        office_name=office_name,
        group_id=user.group_id,
        group_name=group_name,
        managed_group_names=managed_group_names,
        year=year or today.year,
        month=month or today.month,
        user_role=role,
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
    # 프론트 현재 화면 컨텍스트 (예: {"current_route": "/roster_view", "month": 5})
    ui_metadata: Optional[dict] = None


class ChatResponse(BaseModel):
    answer: str
    conversation_id: str
    awaiting_approval: bool = False
    preview: Optional[dict] = None
    # client-action(navigate/prefill) — 프론트가 실행할 UI 의도. 없으면 빈 리스트.
    ui_actions: list[dict] = []
    # 답형 조회결과 (인라인 렌더용). 현재 미사용 — 후속 단계에서 채움.
    data: Optional[dict] = None


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

    # Layer A — 입력 분류. MALICIOUS 는 LLM/도구 호출 전에 차단.
    input_check = classify_input(req.message)
    if input_check.verdict is InputVerdict.MALICIOUS:
        logger.warning(
            "[security] input blocked user=%s reasons=%s",
            current_user.nurse_id, input_check.reason_codes,
        )
        return ChatResponse(
            answer=input_check.block_message or "처리할 수 없는 요청입니다.",
            conversation_id=req.conversation_id or str(uuid.uuid4()),
            awaiting_approval=False,
            preview=None,
            ui_actions=[],
        )
    if input_check.verdict is InputVerdict.SUSPICIOUS:
        logger.info(
            "[security] input suspicious user=%s reasons=%s",
            current_user.nurse_id, input_check.reason_codes,
        )

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
    ctx = _build_session_ctx(db, current_user, conv_id, req.year, req.month)
    ctx.messages = conv.messages
    ctx.variable_memory = conv.variable_memory or {}
    ctx.pending_approval = conv.pending_approval
    ctx.ui_metadata = req.ui_metadata

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

    # Layer B — 출력 누출 검사. 시스템 sentinel/내부 경로/크로스-테넌트 group_id 등.
    raw_answer = result.answer or ""
    output_check = check_output(raw_answer, user_group_id=current_user.group_id)
    if output_check.reason_codes:
        logger.warning(
            "[security] output flagged user=%s verdict=%s reasons=%s",
            current_user.nurse_id, output_check.verdict.value, output_check.reason_codes,
        )

    # B7: 답형 조회 결과를 인라인 렌더용 data 로 노출. output 검열이 BLOCKED 면
    # data 도 보내지 않음 (전체 응답이 generic 으로 치환된 상태).
    safe_data: Any | None = None
    if output_check.verdict.value != "blocked":
        raw_data = result.data
        # dict 면 그대로, list 면 {"items": [...]} 래핑 (스키마 일관성).
        if isinstance(raw_data, dict):
            safe_data = raw_data
        elif isinstance(raw_data, list):
            safe_data = {"items": raw_data}

    return ChatResponse(
        answer=output_check.answer,
        conversation_id=conv_id,
        awaiting_approval=bool(result.awaiting_approval),
        preview=result.preview if result.awaiting_approval else None,
        ui_actions=result.ui_actions,
        data=safe_data,
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
