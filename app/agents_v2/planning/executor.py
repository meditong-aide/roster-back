"""Plan Executor — 위상순서로 task 실행, 참조 치환, mutate 는 dry-run 수집.

LLMCompiler [3] Executor. 기존 조각 재사용:
  - kind=mutate → preview_only=True(dry-run) → previews 수집 → 기존 consolidated 승인 게이트가
    위상순서로 commit(승인 후).
  - 실패 task 발생 시 후속 스킵하고 중단(상위에서 replan/ReAct fallback).

설계: docs/AGENT_DAG_PLANNING_DESIGN.md §3·§5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agents_v2.errors import ErrorType, classify
from agents_v2.planning.plan import Plan, resolve_args


@dataclass
class PlanExecResult:
    outputs: dict[str, Any] = field(default_factory=dict)   # task_id → 결과 data
    previews: list[dict] = field(default_factory=list)       # mutate dry-run(승인 대상)
    order: list[str] = field(default_factory=list)           # 실행 순서(trace)
    failed: dict | None = None                               # {task, data} 실패 시


# 실행 중단시키는 outcome(사용자 개입/오류 필요).
_STOP = {ErrorType.GENERIC_ERROR, ErrorType.VERIFICATION_FAILED,
         ErrorType.PERMISSION_DENIED, ErrorType.CLARIFICATION}


def execute_plan(
    db: Any,
    plan: Plan,
    ctx: Any,
    execute_fn: Callable[[Any, str, dict, Any], Any],
    *,
    dry_run_mutations: bool = True,
) -> PlanExecResult:
    """plan 을 위상순서로 실행.

    execute_fn(db, skill, args, ctx) → SkillResult(.data) 또는 data. (기본은 execute_skill)
    dry_run_mutations=True: mutate task 에 preview_only=True 주입(승인 전 미리보기).
    """
    plan.validate()
    res = PlanExecResult()
    for level in plan.topo_levels():
        for task in level:
            args = resolve_args(task.args, res.outputs)
            if task.kind == "mutate" and dry_run_mutations:
                args = {**args, "preview_only": True}
            raw = execute_fn(db, task.skill, args, ctx)
            data = getattr(raw, "data", raw)
            res.outputs[task.id] = data
            res.order.append(task.id)
            if classify(data) in _STOP:
                res.failed = {"task": task.id, "skill": task.skill, "data": data}
                return res  # 후속 task 스킵
            if task.kind == "mutate" and dry_run_mutations:
                res.previews.append({"task": task.id, "skill": task.skill,
                                     "args": args, "data": data})
    return res
