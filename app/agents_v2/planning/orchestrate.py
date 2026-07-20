"""DAG 계획 오케스트레이션 glue — Planner → Executor → Joiner.

LLMCompiler 전체 파이프라인의 진입점. 의존 복합이면 plan 실행 결과(+승인대상 previews)를
반환, 아니면 None(호출부가 기존 ReAct 로 처리). agent_v3 통합은 opt-in 플래그 뒤.

설계: docs/AGENT_DAG_PLANNING_DESIGN.md.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from agents_v2.planning.executor import PlanExecResult, execute_plan
from agents_v2.planning.plan import Plan
from agents_v2.planning.planner import build_plan

logger = logging.getLogger(__name__)


@dataclass
class PlanRun:
    plan: Plan
    exec: PlanExecResult

    @property
    def needs_approval(self) -> bool:
        return bool(self.exec.previews) and self.exec.failed is None

    @property
    def failed(self) -> bool:
        return self.exec.failed is not None


def try_plan_run(
    db: Any, user_message: str, ctx: Any,
    planner_llm: Any, skill_tools: list[dict],
    execute_fn: Callable[[Any, str, dict, Any], Any],
    *,
    session_factory: Callable[[], Any] | None = None,
) -> PlanRun | None:
    """의존 복합이면 plan 생성·실행(mutate=dry-run). 아니면 None(ReAct fallback).

    session_factory: 주면 레벨 내 read 를 세션 격리 병렬 실행(mutate 는 순차).
    """
    plan = build_plan(planner_llm, user_message, skill_tools)
    if plan is None:
        return None
    logger.info("[plan] %d tasks: %s", len(plan.tasks),
                [(t.id, t.skill, t.kind, t.deps) for t in plan.tasks])
    return PlanRun(plan=plan, exec=execute_plan(
        db, plan, ctx, execute_fn, session_factory=session_factory))


def join_answer(llm: Any, user_message: str, run: PlanRun) -> str:
    """전 task 출력으로 최종 답변 합성 (LLMCompiler [4] Joiner). 데이터에만 근거."""
    payload = {tid: out for tid, out in run.exec.outputs.items()}
    blob = json.dumps(payload, ensure_ascii=False, default=str)[:4000]
    sys = ("너는 계획 실행 결과를 사용자에게 자연어로 답하는 역할이다. "
           "아래 task 출력 데이터에만 근거해 간결·정확히 답하라. 데이터에 없는 것을 지어내지 마라.")
    user = f"[사용자 요청]\n{user_message}\n\n[task 실행 결과]\n{blob}"
    try:
        resp = llm.chat([{"role": "system", "content": sys},
                         {"role": "user", "content": user}], tools=[])
        return (getattr(resp, "text", None) or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("[plan] join 실패: %s", e)
        return ""
