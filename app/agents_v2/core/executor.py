"""Executor — runs an ExecutionPlan by dispatching each step to its skill.

The executor is the bridge between the planning layer and the skill layer.
It handles step sequencing, dependency resolution, and result collection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from agents_v2.schemas.execution_plan import ExecutionPlan, PlanStep
from agents_v2.schemas.agent_response import AgentResponse


@dataclass
class StepResult:
    """Result of executing a single plan step."""
    step_index: int
    skill_name: str
    success: bool
    data: Any = None
    error: str | None = None


SkillRunner = Callable[[Session, str, dict], Any]
"""Signature: (db, skill_name, parameters) → result data or raises."""


def execute_plan(
    db: Session,
    plan: ExecutionPlan,
    skill_runner: SkillRunner,
    *,
    llm_call: Any = None,
    dry_run: bool = False,
) -> AgentResponse:
    """Execute an execution plan step by step.

    Args:
        db: Database session.
        plan: The execution plan to run.
        skill_runner: Callable that dispatches a skill by name with parameters.
        llm_call: LLM callable for answer generation.
        dry_run: If True, only preview without committing.

    Returns:
        AgentResponse with results or clarification/approval requests.
    """
    # Handle blocked plans
    if plan.is_blocked:
        return AgentResponse(
            needs_clarification=True,
            clarification_question=plan.pending_clarification,
        )

    # Handle approval flow
    if plan.needs_approval and not dry_run:
        # First run as preview
        preview_results = _run_steps(db, plan, skill_runner, preview=True)
        if any(not r.success for r in preview_results):
            errors = [r.error for r in preview_results if r.error]
            return AgentResponse(error="; ".join(errors))
        return AgentResponse(
            awaiting_approval=True,
            preview={
                "steps": [
                    {
                        "skill": r.skill_name,
                        "preview_data": r.data,
                    }
                    for r in preview_results
                ],
                "risk_level": plan.risk_level.value,
            },
            execution_trace=[_step_trace(r) for r in preview_results],
        )

    # Handle preview-only flow
    if plan.needs_preview and not dry_run:
        preview_results = _run_steps(db, plan, skill_runner, preview=True)
        # For preview operations, show what would happen, then proceed
        # The actual execution happens after user confirms

    # Execute
    results = _run_steps(db, plan, skill_runner, preview=dry_run)

    # Check for errors
    errors = [r for r in results if not r.success]
    if errors:
        return AgentResponse(
            error="; ".join(r.error or "unknown error" for r in errors),
            execution_trace=[_step_trace(r) for r in results],
        )

    # Collect data from all steps
    all_data = [r.data for r in results if r.data is not None]
    merged_data = all_data[0] if len(all_data) == 1 else all_data

    return AgentResponse(
        data=merged_data,
        execution_trace=[_step_trace(r) for r in results],
    )


def _run_steps(
    db: Session,
    plan: ExecutionPlan,
    skill_runner: SkillRunner,
    *,
    preview: bool = False,
) -> list[StepResult]:
    """Run all steps respecting dependencies."""
    results: list[StepResult] = []
    completed: set[int] = set()

    for i, step in enumerate(plan.steps):
        # Check dependencies
        for dep in step.depends_on:
            if dep not in completed:
                results.append(
                    StepResult(
                        step_index=i,
                        skill_name=step.skill_name,
                        success=False,
                        error=f"dependency step {dep} not completed",
                    )
                )
                continue

        params = dict(step.parameters)
        if preview:
            params["preview_only"] = True

        try:
            data = skill_runner(db, step.skill_name, params)
            results.append(
                StepResult(
                    step_index=i,
                    skill_name=step.skill_name,
                    success=True,
                    data=data,
                )
            )
            completed.add(i)
        except Exception as exc:
            results.append(
                StepResult(
                    step_index=i,
                    skill_name=step.skill_name,
                    success=False,
                    error=str(exc),
                )
            )

    return results


def _step_trace(result: StepResult) -> dict:
    return {
        "step": result.step_index,
        "skill": result.skill_name,
        "success": result.success,
        "error": result.error,
    }
