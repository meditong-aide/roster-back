"""사용량 대시보드 엔드포인트 — agent_llm_usage 집계 노출 (HN/ADM 전용).

agents_v2(스케줄링 에이전트)와 원티드 agent 의 LLM 토큰/비용을 한 테이블
(agent_llm_usage)에서 집계한다. `by=purpose` 로 에이전트/용도별 분리 조회
(turn/router/memory/preview/wanted...). 비용 데이터는 민감하므로 수간호사/관리자 전용.

GET /api/agent/usage?by=group|nurse|model|purpose&days=30
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from agents_v2.usage import usage_summary
from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User

router = APIRouter(prefix="/api/agent/usage", tags=["agent_usage"])


def _resolve_role(user: User) -> str:
    """chat_router._resolve_role 와 동일 의미 (ADM / HN / NURSE)."""
    if getattr(user, "is_master_admin", False):
        return "ADM"
    if getattr(user, "is_head_nurse", False) or (getattr(user, "hn_auth", "") or "").upper() == "HN":
        return "HN"
    return "NURSE"


@router.get("")
def get_usage(
    by: str = Query("group", description="group|nurse|model|purpose"),
    days: Optional[int] = Query(None, ge=1, le=365, description="최근 N일 (미지정=전체)"),
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
) -> dict:
    """LLM 사용량/비용 집계. HN=자기 병동, ADM=전체."""
    role = _resolve_role(current_user)
    if role not in ("HN", "ADM"):
        raise HTTPException(
            status_code=403,
            detail="사용량 조회는 수간호사(HN) 또는 관리자(ADM) 전용입니다.",
        )

    since = datetime.utcnow() - timedelta(days=days) if days else None
    # HN 은 자기 병동만, ADM 은 전체(group_id=None)
    group_id = None if role == "ADM" else getattr(current_user, "group_id", None)

    try:
        rows = usage_summary(db, by=by, group_id=group_id, since=since)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    total = {
        "calls": sum(r["calls"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
    }
    return {
        "by": by,
        "since_days": days,
        "scope": "all" if role == "ADM" else group_id,
        "rows": rows,
        "total": total,
    }
