"""Canonicalizer — assembles grounded concepts into a CanonicalQuery.

Takes the typed + grounded fragments and the session context,
then fills in scope, subject, operation, filters, and mutation fields.
Uses LLM to resolve ambiguities in how fragments compose together.
"""

from __future__ import annotations

from typing import Any

from agents_v2.schemas.grounded_concept import (
    ConceptType,
    GroundedConcept,
    GroundingStatus,
)
from agents_v2.schemas.canonical_query import (
    CanonicalQuery,
    GroundedPredicate,
    MutationAction,
    Operation,
    Scope,
    SlotStatus,
    Subject,
)


CANONICALIZE_PROMPT = """\
You are a query canonicalizer for a nurse scheduling system.

Given grounded concept fragments and session context, produce a canonical query.

## Fields to determine

- **scope**: wanted_submissions | wanted_adjustment | draft_schedule | published_schedule | shift_definitions | generation_job | nurse_info | constraint_config
- **subject**: person | request_row | schedule_row | shift_code | generation_job | constraint | team
- **operation**: list | count | summarize | compare | validate | bulk_update | single_update | generate | explain | recommend | cancel

## Scope inference rules
- "조정판/확정본" + wanted context → wanted_adjustment
- "원티드/희망근무" → wanted_submissions
- "근무표" without specific qualifier → draft_schedule (check if published exists)
- "발행/배포된" → published_schedule
- "설정/제약" → constraint_config
- "생성/진행" → generation_job

## Operation inference
- Questions (몇 명, 누가, 어떤) → list or count
- Action words (변경, 수정, 바꿔) → single_update or bulk_update
- "생성/만들어" → generate
- "취소" → cancel
- "분석/비교" → summarize or compare

Return JSON with fields: scope, subject, operation, and any additional filters.
"""


def build_canonical_query(
    grounded_concepts: list[GroundedConcept],
    session_context: dict,
    llm_call: Any,
    *,
    user_message: str = "",
) -> CanonicalQuery:
    """Build a CanonicalQuery from grounded concepts + context.

    Args:
        grounded_concepts: List of grounded concept results.
        session_context: Dict with group_id, year, month, user_role, etc.
        llm_call: Callable (system_prompt, user_prompt) → str for ambiguity resolution.
        user_message: Original user message for context.

    Returns:
        CanonicalQuery with all fields populated.
    """
    query = CanonicalQuery(
        group_id=session_context.get("group_id"),
        year=session_context.get("year"),
        month=session_context.get("month"),
    )

    slot_status = SlotStatus()
    notes: list[str] = []

    # Pass 1: Extract concrete values from grounded concepts
    for gc in grounded_concepts:
        if gc.status == GroundingStatus.UNRESOLVED:
            slot_status.missing.append(gc.input_text)
            notes.append(f"UNRESOLVED: {gc.input_text}")
            continue
        if gc.status == GroundingStatus.AMBIGUOUS:
            slot_status.ambiguous.append(gc.input_text)
            notes.append(f"AMBIGUOUS: {gc.input_text}")
            continue

        _apply_grounded_concept(query, gc, slot_status, notes)

    # Pass 2: Use LLM to determine scope/subject/operation if not yet set
    if not query.scope or not query.operation:
        _resolve_via_llm(query, grounded_concepts, session_context, llm_call, user_message)

    # Fill slot status
    query.slot_status = slot_status
    query.notes = notes

    # Calculate confidence
    total = len(grounded_concepts)
    grounded = sum(1 for gc in grounded_concepts if gc.is_grounded)
    query.confidence = grounded / total if total > 0 else 0.0

    if slot_status.filled:
        notes.append(f"filled slots: {', '.join(slot_status.filled)}")

    return query


def _apply_grounded_concept(
    query: CanonicalQuery,
    gc: GroundedConcept,
    slot_status: SlotStatus,
    notes: list[str],
) -> None:
    """Apply a single grounded concept to the canonical query."""
    val = gc.grounded_value

    if gc.concept_type == ConceptType.ENTITY:
        if isinstance(val, dict):
            if val.get("entity_type") == "nurse":
                query.nurse_ids.append(val["nurse_id"])
                slot_status.filled.append(f"nurse:{val.get('name', val['nurse_id'])}")
            elif val.get("entity_type") == "team":
                member_ids = val.get("member_ids", [])
                query.nurse_ids.extend(member_ids)
                slot_status.filled.append(f"team:{val.get('team_id')}")

    elif gc.concept_type == ConceptType.LEXICAL:
        if isinstance(val, dict) and "shift_id" in val:
            query.shift_codes.append(val["shift_id"])
            slot_status.filled.append(f"shift:{val['shift_id']}")

    elif gc.concept_type == ConceptType.TEMPORAL:
        if isinstance(val, list):
            for v in val:
                if isinstance(v, dict) and "shift_id" in v:
                    query.shift_codes.append(v["shift_id"])
            slot_status.filled.append(f"temporal_shifts:{len(val)}")
        elif isinstance(val, dict) and "shift_id" in val:
            query.shift_codes.append(val["shift_id"])
            slot_status.filled.append(f"temporal_shift:{val['shift_id']}")

    elif gc.concept_type == ConceptType.STATUS:
        if isinstance(val, dict):
            predicate = val.get("predicate", "")
            query.grounded_predicate = GroundedPredicate(
                name=predicate,
                arguments=val,
                evidence=gc.evidence,
            )
            slot_status.filled.append(f"status:{predicate}")
            # If status has shift_ids, add them to filter
            if "shift_ids" in val:
                query.shift_codes.extend(val["shift_ids"])

    elif gc.concept_type == ConceptType.WORKFLOW:
        if isinstance(val, dict):
            chain = val.get("action_chain", [])
            if chain:
                first_action = chain[0]
                query.mutation = MutationAction(
                    action=first_action.get("action", ""),
                    target_field=first_action.get("field"),
                    target_value=first_action.get("value"),
                    followup_actions=[
                        a.get("action", "") for a in chain[1:]
                    ],
                )
                slot_status.filled.append(f"workflow:{first_action.get('action')}")


def _resolve_via_llm(
    query: CanonicalQuery,
    grounded_concepts: list[GroundedConcept],
    session_context: dict,
    llm_call: Any,
    user_message: str,
) -> None:
    """Use LLM to fill in scope/subject/operation from context."""
    import json

    concept_summary = []
    for gc in grounded_concepts:
        concept_summary.append({
            "type": gc.concept_type.value,
            "text": gc.input_text,
            "status": gc.status.value,
            "value": str(gc.grounded_value)[:200] if gc.grounded_value else None,
        })

    user_prompt = (
        f"Original message: \"{user_message}\"\n"
        f"Session: year={session_context.get('year')}, month={session_context.get('month')}\n"
        f"Grounded concepts: {json.dumps(concept_summary, ensure_ascii=False)}\n"
        f"Current query state: scope={query.scope}, operation={query.operation}, "
        f"mutation={query.mutation.action if query.mutation else None}\n\n"
        f"Determine scope, subject, and operation. Return JSON."
    )

    raw = llm_call(CANONICALIZE_PROMPT, user_prompt)

    # Parse LLM response
    text = raw.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts[1:]:
            cleaned = part.strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            if cleaned.startswith("{"):
                text = cleaned
                break

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return

    # Apply LLM-resolved fields
    scope_map = {s.value: s for s in Scope}
    subject_map = {s.value: s for s in Subject}
    operation_map = {o.value: o for o in Operation}

    if not query.scope and result.get("scope"):
        query.scope = scope_map.get(result["scope"])
    if not query.operation and result.get("operation"):
        query.operation = operation_map.get(result["operation"])
    if result.get("subject"):
        query.subject = subject_map.get(result["subject"])
