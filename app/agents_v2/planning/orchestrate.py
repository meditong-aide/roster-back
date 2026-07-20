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


def preview_fingerprint(previews: list[dict]) -> str:
    """미리보기 요약의 지문 — 승인 시점 대비 커밋 시점 상태 변화(staleness) 감지용."""
    import hashlib

    key = json.dumps(
        [[p.get("task"), (p.get("data") or {}).get("summary")] for p in (previews or [])],
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.md5(key.encode()).hexdigest()


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


def _replan_feedback(run: PlanRun) -> str:
    """실패 관찰 → planner 피드백 문자열. 어느 task 가 왜 실패했는지 + 시도한 구조."""
    f = run.exec.failed or {}
    data = f.get("data") or {}
    reason = data.get("error") or data.get("question") or data.get("block_reason") or "알 수 없는 오류"
    tried = [{"id": t.id, "skill": t.skill, "kind": t.kind, "deps": t.deps, "args": t.args}
             for t in run.plan.tasks]
    return (f"- 실패 task: {f.get('task')} (skill={f.get('skill')})\n"
            f"- 실패 사유: {reason}\n"
            f"- 시도한 계획: {json.dumps(tried, ensure_ascii=False, default=str)[:1500]}")


def try_plan_run(
    db: Any, user_message: str, ctx: Any,
    planner_llm: Any, skill_tools: list[dict],
    execute_fn: Callable[[Any, str, dict, Any], Any],
    *,
    session_factory: Callable[[], Any] | None = None,
    max_replans: int = 1,
) -> PlanRun | None:
    """의존 복합이면 plan 생성·실행(mutate=dry-run). 아니면 None(ReAct fallback).

    session_factory: 주면 레벨 내 read 를 세션 격리 병렬 실행(mutate 는 순차).
    max_replans: dry-run 실행이 실패하면 실패 관찰을 planner 에 피드백해 **교정 plan** 을
        최대 이 횟수만큼 재시도(LLMCompiler replan). 여전히 실패하면 그 실패 run 반환(→ ReAct).
        preview 단계라 mutate 는 preview_only=True → 재시도해도 실제 반영 없음(안전).
    """
    plan = build_plan(planner_llm, user_message, skill_tools)
    if plan is None:
        return None
    logger.info("[plan] %d tasks: %s", len(plan.tasks),
                [(t.id, t.skill, t.kind, t.deps) for t in plan.tasks])
    run = PlanRun(plan=plan, exec=execute_plan(
        db, plan, ctx, execute_fn, session_factory=session_factory))

    attempts = 0
    while run.failed and attempts < max_replans:
        attempts += 1
        feedback = _replan_feedback(run)
        logger.info("[replan] 시도 %d/%d — 실패 task=%s",
                    attempts, max_replans, (run.exec.failed or {}).get("task"))
        new_plan = build_plan(planner_llm, user_message, skill_tools, feedback=feedback)
        if new_plan is None:
            break  # 교정 불가(planner 가 포기) → 기존 실패 run 유지 → ReAct
        logger.info("[replan] 교정 plan %d tasks: %s", len(new_plan.tasks),
                    [(t.id, t.skill, t.kind, t.deps) for t in new_plan.tasks])
        run = PlanRun(plan=new_plan, exec=execute_plan(
            db, new_plan, ctx, execute_fn, session_factory=session_factory))
    return run


def join_answer(llm: Any, user_message: str, run: PlanRun, correction: str | None = None) -> str:
    """전 task 출력으로 최종 답변 합성 (LLMCompiler [4] Joiner). 데이터에만 근거.

    correction: L2 검증이 직전 답변의 불일치를 지적하면, 그 사유를 넣어 데이터에 더 엄격히 재생성.
    """
    payload = {tid: out for tid, out in run.exec.outputs.items()}
    blob = json.dumps(payload, ensure_ascii=False, default=str)[:4000]
    sys = ("너는 계획 실행 결과를 사용자에게 자연어로 답하는 역할이다. "
           "아래 task 출력 데이터에만 근거해 간결·정확히 답하라. 데이터에 없는 것을 지어내지 마라.")
    if correction:
        sys += (f"\n[검증] 직전 답변이 데이터와 어긋났다: {correction}. "
                "데이터에 없는 수치·이름·상태·완료여부를 절대 말하지 마라.")
    user = f"[사용자 요청]\n{user_message}\n\n[task 실행 결과]\n{blob}"
    try:
        resp = llm.chat([{"role": "system", "content": sys},
                         {"role": "user", "content": user}], tools=[])
        return (getattr(resp, "text", None) or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("[plan] join 실패: %s", e)
        return ""
