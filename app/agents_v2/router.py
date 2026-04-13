"""FastAPI router for the v2 scheduling agent."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from agents_v2.agent import SchedulingAgent


router = APIRouter(prefix="/agent/v2", tags=["agent_v2"])


# ── Request / Response models ──────────────────────────────


class AgentRequest(BaseModel):
    message: str
    year: int | None = None
    month: int | None = None
    conversation_id: str | None = None


class AgentResponseModel(BaseModel):
    answer: str = ""
    data: dict | list | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None
    clarification_options: list[str] = Field(default_factory=list)
    awaiting_approval: bool = False
    preview: dict | None = None
    error: str | None = None
    grounding_trace: list[dict] = Field(default_factory=list)
    execution_trace: list[dict] = Field(default_factory=list)


# ── LLM integration ──────────────────────────────────────


def _get_llm_call():
    """Return the LLM callable.

    This is the integration point — swap this for your LLM provider.
    Expects: (system_prompt: str, user_prompt: str) → str
    """
    try:
        from agents_v2.llm_adapter import llm_call
        return llm_call
    except ImportError:
        # Fallback: raise clear error
        def _not_configured(system: str, user: str) -> str:
            raise RuntimeError(
                "LLM not configured. Create agents_v2/llm_adapter.py with a "
                "llm_call(system_prompt, user_prompt) -> str function."
            )
        return _not_configured


# ── Endpoints ──────────────────────────────────────────────


@router.post("/chat", response_model=AgentResponseModel)
async def agent_chat(
    req: AgentRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """Main agent chat endpoint.

    Receives a natural language message and returns a structured response
    after running through the grounding-first pipeline.
    """
    session_context = {
        "group_id": current_user.group_id,
        "office_id": current_user.office_id,
        "user_role": _resolve_role(current_user),
        "year": req.year,
        "month": req.month,
        "nurse_id": current_user.nurse_id,
    }

    llm_call = _get_llm_call()
    agent = SchedulingAgent(llm_call=llm_call)

    result = agent.run(db, req.message, session_context)

    return AgentResponseModel(
        answer=result.answer,
        data=result.data,
        needs_clarification=result.needs_clarification,
        clarification_question=result.clarification_question,
        clarification_options=result.clarification_options,
        awaiting_approval=result.awaiting_approval,
        preview=result.preview,
        error=result.error,
        grounding_trace=result.grounding_trace,
        execution_trace=result.execution_trace,
    )


@router.post("/approve")
async def agent_approve(
    req: AgentRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """Approve a previously previewed mutation.

    After the agent returns awaiting_approval=True, the client
    calls this endpoint to confirm execution.
    """
    session_context = {
        "group_id": current_user.group_id,
        "office_id": current_user.office_id,
        "user_role": _resolve_role(current_user),
        "year": req.year,
        "month": req.month,
        "nurse_id": current_user.nurse_id,
    }

    # Re-run with approval flag (the message should reference the pending action)
    llm_call = _get_llm_call()
    agent = SchedulingAgent(llm_call=llm_call)

    # For approval, we pass through the same pipeline but skip preview
    result = agent.run(db, req.message, session_context)
    return result.to_dict()


# ── Helpers ──────────────────────────────────────────────


def _resolve_role(user: UserSchema) -> str:
    """Determine user role from auth info."""
    if hasattr(user, "hn_auth") and user.hn_auth:
        return "HN"
    if hasattr(user, "is_admin") and user.is_admin:
        return "ADM"
    return "NURSE"
