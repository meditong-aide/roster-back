"""Grounding dispatcher — routes concept-typed fragments to the right grounder."""

from __future__ import annotations

from sqlalchemy.orm import Session

from agents_v2.schemas.grounded_concept import ConceptType, GroundedConcept
from agents_v2.grounding.lexical_grounder import ground_lexical
from agents_v2.grounding.temporal_grounder import ground_temporal
from agents_v2.grounding.status_grounder import ground_status
from agents_v2.grounding.workflow_grounder import ground_workflow
from agents_v2.grounding.entity_grounder import ground_entity


_GROUNDER_MAP = {
    ConceptType.LEXICAL: ground_lexical,
    ConceptType.TEMPORAL: ground_temporal,
    ConceptType.STATUS: ground_status,
    ConceptType.WORKFLOW: ground_workflow,
    ConceptType.ENTITY: ground_entity,
}


def dispatch_grounding(
    db: Session,
    group_id: str,
    concept_type: ConceptType,
    surface_text: str,
    **kwargs,
) -> GroundedConcept:
    """Route a typed concept fragment to the appropriate grounder.

    Args:
        db: Database session.
        group_id: Hospital group context.
        concept_type: Pre-classified concept type.
        surface_text: The raw user expression fragment.
        **kwargs: Extra arguments forwarded to the grounder
                  (e.g., scope_hint for status, entity_type_hint for entity).

    Returns:
        GroundedConcept with status GROUNDED, AMBIGUOUS, or UNRESOLVED.
    """
    grounder = _GROUNDER_MAP.get(concept_type)
    if not grounder:
        return GroundedConcept(
            concept_type=concept_type,
            input_text=surface_text,
            evidence=[f"no grounder registered for concept type {concept_type}"],
        )
    return grounder(db, group_id, surface_text, **kwargs)
