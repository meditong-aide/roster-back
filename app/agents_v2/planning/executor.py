"""Plan Executor — 위상순서로 task 실행, 참조 치환, mutate 는 dry-run 수집.

LLMCompiler [3] Executor. 기존 조각 재사용:
  - kind=mutate → preview_only=True(dry-run) → previews 수집 → 기존 consolidated 승인 게이트가
    위상순서로 commit(승인 후).
  - 실패 task 발생 시 후속 스킵하고 중단(상위에서 replan/ReAct fallback).

설계: docs/AGENT_DAG_PLANNING_DESIGN.md §3·§5.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from agents_v2.errors import ErrorType, classify
from agents_v2.planning.plan import Plan, PlanTask, resolve_args


# 비동기·외부 부수효과 skill — 트랜잭션 밖·커밋 후에만 실행(플랜 롤백돼도 enqueue 안 되게,
# dry-run 미리보기에서 큐 등록 안 되게). executor 는 이들을 실행 않고 deferred 로 넘긴다.
ASYNC_SKILLS = frozenset({"generate_schedule"})


@dataclass
class PlanExecResult:
    outputs: dict[str, Any] = field(default_factory=dict)   # task_id → 결과 data
    previews: list[dict] = field(default_factory=list)       # mutate dry-run(승인 대상)
    order: list[str] = field(default_factory=list)           # 실행 순서(trace)
    failed: dict | None = None                               # {task, data} 실패 시
    deferred: list[PlanTask] = field(default_factory=list)   # 커밋 후 실행할 async task


# 실행 중단시키는 outcome(사용자 개입/오류 필요).
_STOP = {ErrorType.GENERIC_ERROR, ErrorType.VERIFICATION_FAILED,
         ErrorType.PERMISSION_DENIED, ErrorType.CLARIFICATION}


def _run_task(db: Any, task: PlanTask, ctx: Any, execute_fn, outputs: dict,
              dry_run: bool) -> tuple[Any, dict]:
    """단일 task 실행 — 참조 치환 + mutate 면 preview_only **명시 주입**. (data, args) 반환.

    dry_run=True → preview_only=True(미리보기), False → preview_only=False(실제 적용).
    planner 가 args 에 넣은 값에 의존하지 않도록 실행단계가 강제한다(commit 인데 미리보기만 되는 버그 방지).
    """
    args = resolve_args(task.args, outputs)
    if task.kind == "mutate":
        args = {**args, "preview_only": dry_run}
    raw = execute_fn(db, task.skill, args, ctx)
    return getattr(raw, "data", raw), args


def execute_plan(
    db: Any,
    plan: Plan,
    ctx: Any,
    execute_fn: Callable[[Any, str, dict, Any], Any],
    *,
    dry_run_mutations: bool = True,
    session_factory: Callable[[], Any] | None = None,
    max_workers: int = 4,
    defer_skills: frozenset[str] = ASYNC_SKILLS,
) -> PlanExecResult:
    """plan 을 위상순서로 실행. 레벨 내 **read 는 병렬(세션 격리), mutate 는 순차**.

    execute_fn(db, skill, args, ctx) → SkillResult(.data) 또는 data. (기본은 execute_skill)
    dry_run_mutations=True: mutate 에 preview_only=True(승인 전 미리보기).
    session_factory: 주면 read 병렬(각 read 는 fresh 세션→plain dict 반환→메인이 병합).
        None 이면 전부 순차(하위호환). mutate 는 항상 공유 db 로 순차(override·일관성).

    안전: 병렬 read 는 fresh 세션·plain data 반환이라 cross-session 오염 없음. outputs 쓰기는
    메인 스레드에서만(각 task 자기 키) → race 없음.
    """
    plan.validate()
    res = PlanExecResult()

    def _record(task: PlanTask, data: Any, args: dict | None, is_mutate_dry: bool) -> bool:
        """outputs/order/previews 기록(메인 스레드). STOP outcome 이면 True(중단)."""
        res.outputs[task.id] = data
        res.order.append(task.id)
        if classify(data) in _STOP:
            if res.failed is None:
                res.failed = {"task": task.id, "skill": task.skill, "data": data}
            return True
        if is_mutate_dry:
            res.previews.append({"task": task.id, "skill": task.skill, "args": args, "data": data})
        return False

    for level in plan.topo_levels():
        # async skill 은 실행 않고 deferred 로(커밋 후 실행). 원 순서 보존.
        for t in level:
            if t.skill in defer_skills:
                res.deferred.append(t)
        active = [t for t in level if t.skill not in defer_skills]
        reads = [t for t in active if t.kind == "read"]
        mutates = [t for t in active if t.kind == "mutate"]

        # 1) reads — 세션 격리 병렬(2개+ & factory 있을 때), 아니면 순차.
        if session_factory is not None and len(reads) > 1:
            def _iso(task: PlanTask):
                sess = session_factory()
                try:
                    data, _ = _run_task(sess, task, ctx, execute_fn, res.outputs, dry_run_mutations)
                    return task, data
                finally:
                    try:
                        sess.close()
                    except Exception:  # noqa: BLE001
                        pass
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for task, data in list(ex.map(_iso, reads)):  # 메인에서 병합(순서 무관)
                    _record(task, data, None, False)
            if res.failed is not None:
                return res
        else:
            for task in reads:
                data, _ = _run_task(db, task, ctx, execute_fn, res.outputs, dry_run_mutations)
                if _record(task, data, None, False):
                    return res

        # 2) mutates — 항상 공유 db 순차(위상순서 유지, override·일관성).
        for task in mutates:
            data, args = _run_task(db, task, ctx, execute_fn, res.outputs, dry_run_mutations)
            if _record(task, data, args, dry_run_mutations):
                return res

    return res
