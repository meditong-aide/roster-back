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


def record_graph_usage(
    db: Session,
    *,
    group_id: str | None,
    user_id: str | None,
    usage_metadata: dict | None,
    purpose: str = "wanted",
    conversation_id: str | None = None,
) -> None:
    """LangChain UsageMetadataCallbackHandler.usage_metadata → agent_llm_usage 적재.

    원티드 agent(app/agents, LangGraph)의 LLM 사용량을 agents_v2 와 같은 테이블로 통합해
    사용량 대시보드(by=purpose)에서 함께 보이게 한다. usage_metadata 형식:
        {model_name: {"input_tokens": N, "output_tokens": N, "total_tokens": N, ...}, ...}
    모델별 1행. 실패해도 호출 흐름에 영향 X (record_llm_usage 가 has_table 가드 + graceful).
    """
    if not usage_metadata or not group_id:
        return
    for model, m in usage_metadata.items():
        if not isinstance(m, dict):
            continue
        record_llm_usage(
            db,
            conversation_id=conversation_id,
            group_id=group_id,
            user_id=user_id,
            model=model,
            purpose=purpose,
            input_tokens=int(m.get("input_tokens", 0) or 0),
            output_tokens=int(m.get("output_tokens", 0) or 0),
        )


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


def usage_by_office(db: Session, *, since: datetime | None = None) -> list[dict]:
    """office 별 총합 + 그 안의 group 별 내역(롤업).

    agent_llm_usage 엔 office_id 가 없으므로 group_id 집계 후 groups→offices 로 매핑.
    반환: [{office_id, office_name, calls, input_tokens, output_tokens, cost_usd,
            groups: [{group_id, group_name, calls, ...}]}, ...] (비용 내림차순).
    """
    from db.models import Group, Office

    rows = usage_summary(db, by="group", since=since)
    if not rows:
        return []

    gids = [r["key"] for r in rows if r["key"]]
    groups = {g.group_id: g for g in db.query(Group).filter(Group.group_id.in_(gids)).all()}
    offices = {o.office_id: o.office_name for o in db.query(Office).all()}

    by_office: dict = {}
    for r in rows:
        g = groups.get(r["key"])
        oid = g.office_id if g else None
        oname = offices.get(oid) or ("(병원 미상)" if oid else "(그룹 미매핑)")
        gname = g.group_name if g else (r["key"] or "(미상)")
        o = by_office.setdefault(
            oid or "__none__",
            {
                "office_id": oid,
                "office_name": oname,
                "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                "groups": [],
            },
        )
        o["calls"] += r["calls"]
        o["input_tokens"] += r["input_tokens"]
        o["output_tokens"] += r["output_tokens"]
        o["cost_usd"] += r["cost_usd"]
        o["groups"].append({
            "group_id": r["key"], "group_name": gname,
            "calls": r["calls"], "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"], "cost_usd": r["cost_usd"],
        })

    result = list(by_office.values())
    for o in result:
        o["cost_usd"] = round(o["cost_usd"], 6)
        o["groups"].sort(key=lambda x: x["cost_usd"], reverse=True)
    result.sort(key=lambda x: x["cost_usd"], reverse=True)
    return result


def usage_timeseries(
    db: Session, *, bucket: str = "day", since: datetime | None = None
) -> list[dict]:
    """일자/월 버킷 집계 + 누적. bucket='day'|'month'.

    DB 종속 date 함수(MSSQL FORMAT vs SQLite strftime)를 피하려 Python 에서 버킷팅.
    반환: [{bucket:'YYYY-MM-DD'|'YYYY-MM', calls, input_tokens, output_tokens, cost_usd,
            cum_cost_usd, cum_tokens, cum_calls}, ...] (시간 오름차순).
    """
    from db.models import AgentLlmUsage

    if bucket not in ("day", "month"):
        raise ValueError(f"bucket must be 'day' or 'month', got {bucket!r}")
    fmt = "%Y-%m-%d" if bucket == "day" else "%Y-%m"

    q = db.query(
        AgentLlmUsage.timestamp,
        AgentLlmUsage.input_tokens,
        AgentLlmUsage.output_tokens,
        AgentLlmUsage.cost_usd,
    )
    if since:
        q = q.filter(AgentLlmUsage.timestamp >= since)

    buckets: dict = {}
    for ts, itok, otok, cost in q.all():
        if ts is None:
            continue
        key = ts.strftime(fmt)
        b = buckets.setdefault(
            key, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        b["calls"] += 1
        b["input_tokens"] += itok or 0
        b["output_tokens"] += otok or 0
        b["cost_usd"] += cost or 0.0

    out: list = []
    cum_cost = 0.0
    cum_tok = 0
    cum_calls = 0
    for key in sorted(buckets):
        b = buckets[key]
        cum_cost += b["cost_usd"]
        cum_tok += b["input_tokens"] + b["output_tokens"]
        cum_calls += b["calls"]
        out.append({
            "bucket": key,
            "calls": b["calls"],
            "input_tokens": b["input_tokens"],
            "output_tokens": b["output_tokens"],
            "cost_usd": round(b["cost_usd"], 6),
            "cum_cost_usd": round(cum_cost, 6),
            "cum_tokens": cum_tok,
            "cum_calls": cum_calls,
        })
    return out
