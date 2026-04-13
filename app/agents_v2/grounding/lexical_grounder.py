"""Lexical grounder — resolve short code aliases (V, OFF, D, E, N, etc.)
against the group's actual shift definitions.

Never hard-code mappings; always look up via read_shift_definitions.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from agents_v2.schemas.grounded_concept import (
    ConceptType,
    GroundedConcept,
    GroundingCandidate,
    GroundingStatus,
)
from agents_v2.tools.shift_tools import read_shift_definitions


def ground_lexical(
    db: Session,
    group_id: str,
    surface_text: str,
) -> GroundedConcept:
    """Resolve a short code/alias token to a canonical shift definition.

    Strategy:
    1. Fetch all shift definitions for the group.
    2. Match surface_text against shift_id, name, shift_gb, and default_shift.
    3. If exactly one match → GROUNDED.
    4. If multiple matches → AMBIGUOUS with candidates.
    5. If no match → UNRESOLVED.
    """
    shifts = read_shift_definitions(db, group_id)
    token = surface_text.strip().upper()

    candidates: list[GroundingCandidate] = []

    for s in shifts:
        score = _match_score(token, s)
        if score > 0:
            candidates.append(
                GroundingCandidate(
                    value={
                        "shift_id": s["shift_id"],
                        "name": s["name"],
                        "shift_gb": s["shift_gb"],
                        "type": s["type"],
                    },
                    confidence=score,
                    evidence=[f"matched against shift_id={s['shift_id']}, name={s['name']}"],
                )
            )

    # Sort by confidence descending
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    if len(candidates) == 1:
        return GroundedConcept(
            concept_type=ConceptType.LEXICAL,
            input_text=surface_text,
            status=GroundingStatus.GROUNDED,
            grounded_value=candidates[0].value,
            evidence=candidates[0].evidence,
        )

    if len(candidates) > 1 and candidates[0].confidence > candidates[1].confidence:
        # Clear winner
        return GroundedConcept(
            concept_type=ConceptType.LEXICAL,
            input_text=surface_text,
            status=GroundingStatus.GROUNDED,
            grounded_value=candidates[0].value,
            evidence=candidates[0].evidence + [
                f"runner-up: {candidates[1].value} (conf={candidates[1].confidence})"
            ],
        )

    if len(candidates) > 1:
        return GroundedConcept(
            concept_type=ConceptType.LEXICAL,
            input_text=surface_text,
            status=GroundingStatus.AMBIGUOUS,
            candidates=candidates,
            requires_clarification=True,
            clarification_question=(
                f"'{surface_text}'에 해당하는 근무코드가 여러 개입니다: "
                + ", ".join(f"{c.value['name']}({c.value['shift_id']})" for c in candidates[:5])
                + ". 어떤 것을 말씀하시나요?"
            ),
        )

    return GroundedConcept(
        concept_type=ConceptType.LEXICAL,
        input_text=surface_text,
        status=GroundingStatus.UNRESOLVED,
        evidence=[f"no shift matching '{surface_text}' found in group {group_id}"],
    )


def _match_score(token: str, shift: dict) -> float:
    """Calculate match score between a token and a shift definition."""
    sid = (shift.get("shift_id") or "").strip().upper()
    name = (shift.get("name") or "").strip().upper()
    gb = (shift.get("shift_gb") or "").strip().upper()
    default = (shift.get("default_shift") or "").strip().upper()

    # Exact shift_id match — highest confidence
    if token == sid:
        return 1.0
    # Exact name match
    if token == name:
        return 0.95
    # default_shift match (e.g., "D" maps to a specific shift via default_shift)
    if token == default:
        return 0.9
    # shift_gb (category) match
    if token == gb:
        return 0.7
    # Partial containment
    if token in name or token in sid:
        return 0.5
    if name and name in token:
        return 0.4
    return 0.0
