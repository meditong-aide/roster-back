"""Workflow grounder — resolve action-intent expressions (적용 안되게, 다시 생성해, etc.)
to canonical mutation action chains.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from agents_v2.schemas.grounded_concept import (
    ConceptType,
    GroundedConcept,
    GroundingCandidate,
    GroundingStatus,
)


# Workflow intent patterns → action chain templates.
# Each entry: (trigger_tokens, action_chain, description)
_WORKFLOW_PATTERNS: list[tuple[list[str], list[dict], str]] = [
    (
        ["적용 안", "반영 안", "빼줘", "제외"],
        [{"action": "set_field", "field": "is_applied", "value": False}],
        "Mark entries as not-applied",
    ),
    (
        ["적용", "반영해", "반영 해", "넣어"],
        [{"action": "set_field", "field": "is_applied", "value": True}],
        "Mark entries as applied",
    ),
    (
        ["다시 생성", "재생성", "새로 만들", "돌려"],
        [{"action": "generate", "mode": "regenerate"}],
        "Trigger roster regeneration",
    ),
    (
        ["생성해", "생성 해", "만들어"],
        [{"action": "generate", "mode": "new"}],
        "Trigger new roster generation",
    ),
    (
        ["취소", "철회", "되돌려"],
        [{"action": "cancel"}],
        "Cancel/retract a submission or action",
    ),
    (
        ["마감", "연장", "데드라인"],
        [{"action": "update_deadline"}],
        "Update campaign deadline",
    ),
    (
        ["변경", "바꿔", "수정"],
        [{"action": "update_entry"}],
        "Modify a schedule entry or attribute",
    ),
    (
        ["삭제", "지워"],
        [{"action": "delete"}],
        "Delete/remove entries",
    ),
    (
        ["발행", "배포", "공표"],
        [{"action": "publish"}],
        "Publish/issue a schedule",
    ),
]


def ground_workflow(
    db: Session,
    group_id: str,
    surface_text: str,
) -> GroundedConcept:
    """Resolve a workflow intent expression to an action chain.

    Strategy:
    1. Scan surface_text for trigger tokens.
    2. If matched, return the action chain template.
    3. Negation-aware: "적용 안되게" matches the negated pattern first.

    Note: db is accepted for interface consistency but not always needed here.
    """
    text = surface_text.strip()
    candidates: list[GroundingCandidate] = []

    for triggers, action_chain, description in _WORKFLOW_PATTERNS:
        score = _calc_trigger_score(text, triggers)
        if score > 0:
            candidates.append(
                GroundingCandidate(
                    value={
                        "action_chain": action_chain,
                        "description": description,
                    },
                    confidence=score,
                    evidence=[f"trigger matched in '{text}': {triggers}"],
                )
            )

    # Sort by confidence descending
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    if not candidates:
        return GroundedConcept(
            concept_type=ConceptType.WORKFLOW,
            input_text=surface_text,
            status=GroundingStatus.UNRESOLVED,
            evidence=[f"no workflow intent matched for '{surface_text}'"],
        )

    # If top candidate is clearly dominant, ground it
    if len(candidates) == 1 or candidates[0].confidence > candidates[1].confidence:
        return GroundedConcept(
            concept_type=ConceptType.WORKFLOW,
            input_text=surface_text,
            status=GroundingStatus.GROUNDED,
            grounded_value=candidates[0].value,
            evidence=candidates[0].evidence,
        )

    # Multiple equally strong matches — ambiguous
    return GroundedConcept(
        concept_type=ConceptType.WORKFLOW,
        input_text=surface_text,
        status=GroundingStatus.AMBIGUOUS,
        candidates=candidates[:3],
        requires_clarification=True,
        clarification_question=(
            f"'{surface_text}'의 의도를 명확히 해주세요: "
            + " / ".join(c.value["description"] for c in candidates[:3])
        ),
    )


def _calc_trigger_score(text: str, triggers: list[str]) -> float:
    """Calculate how well the text matches the trigger tokens.

    Longer trigger matches get higher scores (more specific = more confident).
    """
    best = 0.0
    for trigger in triggers:
        if trigger in text:
            # Score proportional to trigger length (specificity)
            score = min(0.5 + len(trigger) * 0.1, 1.0)
            best = max(best, score)
    return best
