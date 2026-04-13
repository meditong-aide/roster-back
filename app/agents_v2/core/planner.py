"""Planner — maps a CanonicalQuery to an ExecutionPlan.

Determines which skills to invoke, in what order, with what parameters.
Also assesses risk level and preview/approval requirements.
"""

from __future__ import annotations

from agents_v2.schemas.canonical_query import CanonicalQuery, Operation, Scope
from agents_v2.schemas.execution_plan import ExecutionPlan, PlanStep, RiskLevel


# ── Skill routing table ────────────────────────────────────────
# Maps (scope, operation) → skill_name.
# Fallback rules handle combinations not in the table.

_SKILL_ROUTES: dict[tuple[str | None, str | None], str] = {
    # Query operations
    (Scope.WANTED_SUBMISSIONS.value, Operation.LIST.value): "query-schedule",
    (Scope.WANTED_SUBMISSIONS.value, Operation.COUNT.value): "query-schedule",
    (Scope.WANTED_SUBMISSIONS.value, Operation.SUMMARIZE.value): "analyze-report",
    (Scope.WANTED_ADJUSTMENT.value, Operation.LIST.value): "query-schedule",
    (Scope.WANTED_ADJUSTMENT.value, Operation.COUNT.value): "query-schedule",
    (Scope.DRAFT_SCHEDULE.value, Operation.LIST.value): "query-schedule",
    (Scope.DRAFT_SCHEDULE.value, Operation.COUNT.value): "query-schedule",
    (Scope.DRAFT_SCHEDULE.value, Operation.SUMMARIZE.value): "analyze-report",
    (Scope.PUBLISHED_SCHEDULE.value, Operation.LIST.value): "query-schedule",
    (Scope.PUBLISHED_SCHEDULE.value, Operation.SUMMARIZE.value): "analyze-report",
    (Scope.SHIFT_DEFINITIONS.value, Operation.LIST.value): "query-schedule",
    (Scope.NURSE_INFO.value, Operation.LIST.value): "query-schedule",
    (Scope.CONSTRAINT_CONFIG.value, Operation.LIST.value): "query-schedule",
    (Scope.CONSTRAINT_CONFIG.value, Operation.EXPLAIN.value): "query-schedule",
    (Scope.GENERATION_JOB.value, Operation.LIST.value): "query-schedule",

    # Mutation operations
    (Scope.WANTED_ADJUSTMENT.value, Operation.BULK_UPDATE.value): "bulk-mutation",
    (Scope.WANTED_ADJUSTMENT.value, Operation.SINGLE_UPDATE.value): "bulk-mutation",
    (Scope.WANTED_SUBMISSIONS.value, Operation.CANCEL.value): "bulk-mutation",
    (Scope.DRAFT_SCHEDULE.value, Operation.SINGLE_UPDATE.value): "bulk-mutation",
    (Scope.DRAFT_SCHEDULE.value, Operation.BULK_UPDATE.value): "bulk-mutation",
    (Scope.NURSE_INFO.value, Operation.SINGLE_UPDATE.value): "update-person-attr",
    (Scope.CONSTRAINT_CONFIG.value, Operation.SINGLE_UPDATE.value): "update-constraint",
    (Scope.CONSTRAINT_CONFIG.value, Operation.BULK_UPDATE.value): "update-constraint",

    # Generation
    (Scope.GENERATION_JOB.value, Operation.GENERATE.value): "generate-schedule",

    # Validation
    (Scope.DRAFT_SCHEDULE.value, Operation.VALIDATE.value): "validate-schedule",
    (Scope.PUBLISHED_SCHEDULE.value, Operation.VALIDATE.value): "validate-schedule",

    # Recommendation
    (Scope.DRAFT_SCHEDULE.value, Operation.RECOMMEND.value): "recommend-candidates",
    (Scope.PUBLISHED_SCHEDULE.value, Operation.RECOMMEND.value): "recommend-candidates",

    # Compare
    (Scope.DRAFT_SCHEDULE.value, Operation.COMPARE.value): "analyze-report",
    (Scope.WANTED_SUBMISSIONS.value, Operation.COMPARE.value): "analyze-report",
}

# Operations that require preview
_PREVIEW_OPERATIONS = {
    Operation.BULK_UPDATE,
    Operation.GENERATE,
}

# Operations that require approval
_APPROVAL_OPERATIONS = {
    Operation.GENERATE,
}


def build_execution_plan(query: CanonicalQuery) -> ExecutionPlan:
    """Build an execution plan from a canonical query.

    Args:
        query: Fully populated CanonicalQuery.

    Returns:
        ExecutionPlan with steps, risk level, and approval flags.
    """
    # Check for unresolved slots
    if query.has_missing_slots:
        return ExecutionPlan(
            pending_clarification=(
                "다음 정보가 부족합니다: " + ", ".join(query.slot_status.missing)
            ),
        )

    if query.has_ambiguity:
        return ExecutionPlan(
            pending_clarification=(
                "다음 항목이 모호합니다: " + ", ".join(query.slot_status.ambiguous)
            ),
        )

    # Resolve skill
    scope_val = query.scope.value if query.scope else None
    op_val = query.operation.value if query.operation else None
    skill_name = _SKILL_ROUTES.get((scope_val, op_val))

    if not skill_name:
        skill_name = _fallback_skill(query)

    if not skill_name:
        return ExecutionPlan(
            pending_clarification=(
                f"scope={scope_val}, operation={op_val}에 해당하는 스킬을 찾을 수 없습니다."
            ),
        )

    # Build steps
    steps = [
        PlanStep(
            skill_name=skill_name,
            parameters=_build_skill_params(query),
            description=f"{skill_name} for {op_val} on {scope_val}",
        )
    ]

    # If mutation has followup actions, add steps
    if query.mutation and query.mutation.followup_actions:
        for i, followup in enumerate(query.mutation.followup_actions):
            followup_skill = _resolve_followup_skill(followup)
            if followup_skill:
                steps.append(
                    PlanStep(
                        skill_name=followup_skill,
                        parameters={"action": followup},
                        description=f"followup: {followup}",
                        depends_on=[0],
                    )
                )

    # Risk assessment
    risk_level = _assess_risk(query)
    needs_preview = query.operation in _PREVIEW_OPERATIONS
    needs_approval = query.operation in _APPROVAL_OPERATIONS

    return ExecutionPlan(
        steps=steps,
        risk_level=risk_level,
        needs_preview=needs_preview,
        needs_approval=needs_approval,
    )


def _fallback_skill(query: CanonicalQuery) -> str | None:
    """Fallback skill resolution when exact route not found."""
    if query.operation in (Operation.LIST, Operation.COUNT, Operation.EXPLAIN):
        return "query-schedule"
    if query.operation == Operation.SUMMARIZE:
        return "analyze-report"
    if query.operation in (Operation.BULK_UPDATE, Operation.SINGLE_UPDATE):
        return "bulk-mutation"
    if query.operation == Operation.GENERATE:
        return "generate-schedule"
    if query.operation == Operation.VALIDATE:
        return "validate-schedule"
    if query.operation == Operation.RECOMMEND:
        return "recommend-candidates"
    if query.operation == Operation.CANCEL:
        return "bulk-mutation"
    return None


def _build_skill_params(query: CanonicalQuery) -> dict:
    """Extract parameters from the query for the skill."""
    params: dict = {}
    if query.group_id:
        params["group_id"] = query.group_id
    if query.year:
        params["year"] = query.year
    if query.month:
        params["month"] = query.month
    if query.schedule_id:
        params["schedule_id"] = query.schedule_id
    if query.nurse_ids:
        params["nurse_ids"] = query.nurse_ids
    if query.shift_codes:
        params["shift_codes"] = query.shift_codes
    if query.date:
        params["date"] = query.date
    if query.date_range_start:
        params["date_range_start"] = query.date_range_start
    if query.date_range_end:
        params["date_range_end"] = query.date_range_end
    if query.grounded_predicate:
        params["predicate"] = {
            "name": query.grounded_predicate.name,
            "arguments": query.grounded_predicate.arguments,
        }
    if query.mutation:
        params["mutation"] = {
            "action": query.mutation.action,
            "target_field": query.mutation.target_field,
            "target_value": query.mutation.target_value,
        }
    if query.scope:
        params["scope"] = query.scope.value
    if query.operation:
        params["operation"] = query.operation.value
    if query.aggregation_unit:
        params["aggregation_unit"] = query.aggregation_unit
    if query.sort_by:
        params["sort_by"] = query.sort_by
        params["sort_order"] = query.sort_order
    if query.limit:
        params["limit"] = query.limit
    return params


def _resolve_followup_skill(action: str) -> str | None:
    """Map a followup action to a skill."""
    followup_map = {
        "generate": "generate-schedule",
        "validate": "validate-schedule",
        "publish": "generate-schedule",  # publish is part of generate flow
    }
    return followup_map.get(action)


def _assess_risk(query: CanonicalQuery) -> RiskLevel:
    """Determine the risk level of an operation."""
    if not query.is_mutation:
        return RiskLevel.NONE

    if query.operation == Operation.GENERATE:
        return RiskLevel.HIGH

    if query.operation == Operation.BULK_UPDATE:
        return RiskLevel.MEDIUM

    if query.operation in (Operation.SINGLE_UPDATE, Operation.CANCEL):
        return RiskLevel.LOW

    return RiskLevel.NONE
