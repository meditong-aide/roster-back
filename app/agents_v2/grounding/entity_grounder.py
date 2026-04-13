"""Entity grounder — resolve person/team references (김민지, A팀, 9병동, etc.)
to concrete nurse_id / team_id / group_id values via DB lookup.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from agents_v2.schemas.grounded_concept import (
    ConceptType,
    GroundedConcept,
    GroundingCandidate,
    GroundingStatus,
)
from agents_v2.tools.nurse_tools import (
    search_nurses_by_name,
    get_nurses_in_group,
    filter_nurses,
)


def ground_entity(
    db: Session,
    group_id: str,
    surface_text: str,
    *,
    entity_type_hint: str | None = None,
) -> GroundedConcept:
    """Resolve a person/team/group reference to concrete IDs.

    Strategy:
    1. If hint suggests nurse → search by name.
    2. If hint suggests team → filter by team name/id.
    3. If no hint, try nurse name search first, then team.
    4. Handle: exact match, multiple matches (clarify), zero matches.
    """
    text = surface_text.strip()

    # Try nurse name search
    if entity_type_hint in (None, "nurse"):
        result = _ground_nurse(db, group_id, text)
        if result.status != GroundingStatus.UNRESOLVED:
            return result

    # Try team/group matching
    if entity_type_hint in (None, "team"):
        result = _ground_team(db, group_id, text)
        if result.status != GroundingStatus.UNRESOLVED:
            return result

    return GroundedConcept(
        concept_type=ConceptType.ENTITY,
        input_text=surface_text,
        status=GroundingStatus.UNRESOLVED,
        evidence=[f"no nurse or team matched '{surface_text}' in group {group_id}"],
    )


def _ground_nurse(
    db: Session,
    group_id: str,
    name_query: str,
) -> GroundedConcept:
    """Search nurses by name and return grounded result."""
    matches = search_nurses_by_name(db, group_id, name_query)

    if len(matches) == 1:
        nurse = matches[0]
        return GroundedConcept(
            concept_type=ConceptType.ENTITY,
            input_text=name_query,
            status=GroundingStatus.GROUNDED,
            grounded_value={
                "entity_type": "nurse",
                "nurse_id": nurse["nurse_id"],
                "name": nurse["name"],
                "grade": nurse["grade"],
                "team_id": nurse["team_id"],
            },
            evidence=[f"exact name match: {nurse['name']} (id={nurse['nurse_id']})"],
        )

    if len(matches) > 1:
        candidates = [
            GroundingCandidate(
                value={
                    "entity_type": "nurse",
                    "nurse_id": m["nurse_id"],
                    "name": m["name"],
                    "grade": m["grade"],
                    "team_id": m["team_id"],
                },
                confidence=0.8,
                evidence=[f"name match: {m['name']} (id={m['nurse_id']})"],
            )
            for m in matches[:10]
        ]
        return GroundedConcept(
            concept_type=ConceptType.ENTITY,
            input_text=name_query,
            status=GroundingStatus.AMBIGUOUS,
            candidates=candidates,
            requires_clarification=True,
            clarification_question=(
                f"'{name_query}'에 해당하는 간호사가 {len(matches)}명입니다: "
                + ", ".join(f"{m['name']}(등급{m['grade']})" for m in matches[:5])
                + ". 누구를 말씀하시나요?"
            ),
        )

    return GroundedConcept(
        concept_type=ConceptType.ENTITY,
        input_text=name_query,
        status=GroundingStatus.UNRESOLVED,
        evidence=[f"no nurse named '{name_query}' found in group {group_id}"],
    )


def _ground_team(
    db: Session,
    group_id: str,
    text: str,
) -> GroundedConcept:
    """Try to match a team reference.

    Looks for patterns like "A팀", "1팀", "팀 A" etc.
    Resolves by checking which team_ids exist in the group's nurse list.
    """
    # Extract potential team identifier
    team_id_candidate = None
    for part in [text.replace("팀", "").strip(), text]:
        if part.isdigit():
            team_id_candidate = int(part)
            break

    if team_id_candidate is None and "팀" not in text:
        return GroundedConcept(
            concept_type=ConceptType.ENTITY,
            input_text=text,
            status=GroundingStatus.UNRESOLVED,
        )

    # Get all nurses and find unique team IDs
    all_nurses = get_nurses_in_group(db, group_id)
    team_ids = sorted({n["team_id"] for n in all_nurses if n["team_id"] is not None})

    if team_id_candidate is not None and team_id_candidate in team_ids:
        members = [n for n in all_nurses if n["team_id"] == team_id_candidate]
        return GroundedConcept(
            concept_type=ConceptType.ENTITY,
            input_text=text,
            status=GroundingStatus.GROUNDED,
            grounded_value={
                "entity_type": "team",
                "team_id": team_id_candidate,
                "member_count": len(members),
                "member_ids": [n["nurse_id"] for n in members],
            },
            evidence=[f"team {team_id_candidate} has {len(members)} members in group {group_id}"],
        )

    if team_ids:
        return GroundedConcept(
            concept_type=ConceptType.ENTITY,
            input_text=text,
            status=GroundingStatus.AMBIGUOUS,
            candidates=[
                GroundingCandidate(
                    value={"entity_type": "team", "team_id": tid},
                    confidence=0.5,
                    evidence=[f"team {tid} exists in group"],
                )
                for tid in team_ids[:10]
            ],
            requires_clarification=True,
            clarification_question=(
                f"어떤 팀을 말씀하시나요? 이 병동의 팀: {', '.join(str(t) for t in team_ids)}"
            ),
        )

    return GroundedConcept(
        concept_type=ConceptType.ENTITY,
        input_text=text,
        status=GroundingStatus.UNRESOLVED,
        evidence=[f"no teams found in group {group_id}"],
    )
