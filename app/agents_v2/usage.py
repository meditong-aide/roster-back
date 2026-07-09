"""LLM 사용량 기록·집계 — 병동(group)·간호사(user_id)별 비용 추적.

매 LLM 호출의 토큰/모델/용도를 agent_llm_usage 에 1행씩 적재하고, cost.compute_cost 로
비용(USD)을 산출해 함께 저장한다. 집계 리포트(usage_summary)로 병동/간호사/모델별 비용 조회.

견고성: middleware._write_skill_audit 와 동일 패턴 — has_table 프로브로 테이블 부재 시
INSERT 자체를 회피(영구 skip)해 세션 오염을 방지한다(운영 마이그레이션 전 무해).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from agents_v2.cost import compute_cost

logger = logging.getLogger(__name__)

_usage_table_present: bool | None = None
_BY_COLUMN = {"group": "group_id", "nurse": "user_id", "model": "model", "purpose": "purpose"}


def record_llm_usage(
    db: Session,
    *,
    conversation_id: str | None,
    group_id: str | None,
    user_id: str | None,
    model: str | None,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """LLM 호출 1건의 사용량/비용 기록. 실패/테이블 부재 시 무해하게 skip."""
    global _usage_table_present
    if _usage_table_present is False:
        return
    if not group_id:
        return  # group_id NOT NULL — 없으면 기록 생략
    if (input_tokens or 0) <= 0 and (output_tokens or 0) <= 0:
        return  # 0-토큰(예: 호출 실패) 기록 생략
    try:
        from db.models import AgentLlmUsage

        if _usage_table_present is None:
            _usage_table_present = sa_inspect(db.get_bind()).has_table(
                AgentLlmUsage.__tablename__
            )
            if not _usage_table_present:
                logger.warning(
                    "[usage] agent_llm_usage 테이블이 없습니다 — LLM 사용량 기록 비활성화 "
                    "(이후 skip). 마이그레이션(2026_05_29_add_agent_llm_usage) 적용 필요."
                )
                return

        row = AgentLlmUsage(
            conversation_id=conversation_id,
            group_id=group_id,
            user_id=user_id,
            model=model,
            purpose=purpose,
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            cost_usd=compute_cost(model, input_tokens or 0, output_tokens or 0),
        )
        db.add(row)
        db.flush()
    except Exception as e:  # noqa: BLE001
        # 테이블 부재는 위 프로브가 이미 차단하므로 여기 도달 드묾. db.rollback() 은
        # 하지 않는다(턴 내 미커밋 변경 clobber 방지 — _write_skill_audit 와 동일 정책).
        logger.warning("[usage] llm usage insert failed: %s", e)


def usage_summary(
    db: Session,
    *,
    by: str = "group",
    group_id: str | None = None,
    since: datetime | None = None,
) -> list[dict]:
    """사용량 집계 리포트. by='group'|'nurse'|'model'.

    반환: [{key, calls, input_tokens, output_tokens, cost_usd}, ...] (비용 내림차순).
    """
    from db.models import AgentLlmUsage

    col_name = _BY_COLUMN.get(by)
    if col_name is None:
        raise ValueError(f"by must be one of {list(_BY_COLUMN)}, got {by!r}")
    col = getattr(AgentLlmUsage, col_name)

    q = db.query(
        col.label("key"),
        func.count(AgentLlmUsage.id).label("calls"),
        func.sum(AgentLlmUsage.input_tokens).label("in_tok"),
        func.sum(AgentLlmUsage.output_tokens).label("out_tok"),
        func.sum(AgentLlmUsage.cost_usd).label("cost_usd"),
    )
    if group_id:
        q = q.filter(AgentLlmUsage.group_id == group_id)
    if since:
        q = q.filter(AgentLlmUsage.timestamp >= since)
    q = q.group_by(col).order_by(func.sum(AgentLlmUsage.cost_usd).desc())

    return [
        {
            "key": r.key,
            "calls": int(r.calls or 0),
            "input_tokens": int(r.in_tok or 0),
            "output_tokens": int(r.out_tok or 0),
            "cost_usd": round(float(r.cost_usd or 0.0), 6),
        }
        for r in q.all()
    ]
