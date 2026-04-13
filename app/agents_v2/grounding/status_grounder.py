"""Status grounder — resolve status-based expressions (쉬는사람, 근무자, 휴가자, etc.)
to concrete predicates by checking shift type categories and schedule state.

The meaning of "쉬는사람" depends on the scope (schedule vs. wanted vs. adjustment).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from agents_v2.schemas.grounded_concept import (
    ConceptType,
    GroundedConcept,
    GroundingStatus,
)
from agents_v2.tools.shift_tools import read_shift_definitions


# Korean status tokens → (predicate_type, filter_logic_hint)
# predicate_type is used downstream to build the canonical query filter.
_STATUS_PATTERNS: dict[str, dict] = {
    "쉬는": {"predicate": "off", "shift_filter": "type != '근무'"},
    "오프": {"predicate": "off", "shift_filter": "type != '근무'"},
    "휴무": {"predicate": "off", "shift_filter": "type == '오프'"},
    "휴가": {"predicate": "leave", "shift_filter": "type == '휴가'"},
    "공가": {"predicate": "leave", "shift_filter": "type == '공가'"},
    "근무": {"predicate": "working", "shift_filter": "type == '근무'"},
    "일하는": {"predicate": "working", "shift_filter": "type == '근무'"},
    "나이트": {"predicate": "night_shift", "shift_filter": "shift_gb == '나이트'"},
    "야근": {"predicate": "night_shift", "shift_filter": "shift_gb == '나이트'"},
    "미제출": {"predicate": "not_submitted", "scope_hint": "wanted"},
    "제출": {"predicate": "submitted", "scope_hint": "wanted"},
    "신규": {"predicate": "new_nurse", "scope_hint": "nurse_attr"},
    "경력": {"predicate": "experienced", "scope_hint": "nurse_attr"},
    "프리셉터": {"predicate": "has_preceptor", "scope_hint": "nurse_attr"},
}


def ground_status(
    db: Session,
    group_id: str,
    surface_text: str,
    *,
    scope_hint: str | None = None,
) -> GroundedConcept:
    """Resolve a status expression to a grounded predicate.

    Strategy:
    1. Match surface_text against known status patterns.
    2. For shift-based statuses, fetch shift definitions to find concrete shift_ids.
    3. Return the predicate + matching shift codes as grounded_value.
    """
    token = surface_text.strip()

    matched_pattern = None
    for pattern_key, pattern_def in _STATUS_PATTERNS.items():
        if pattern_key in token:
            matched_pattern = (pattern_key, pattern_def)
            break

    if not matched_pattern:
        return GroundedConcept(
            concept_type=ConceptType.STATUS,
            input_text=surface_text,
            status=GroundingStatus.UNRESOLVED,
            evidence=[f"no status pattern matched for '{surface_text}'"],
        )

    pattern_key, pattern_def = matched_pattern
    predicate = pattern_def["predicate"]

    # For non-shift predicates (wanted submission status, nurse attributes),
    # we can ground immediately without shift lookup.
    if "scope_hint" in pattern_def:
        return GroundedConcept(
            concept_type=ConceptType.STATUS,
            input_text=surface_text,
            status=GroundingStatus.GROUNDED,
            grounded_value={
                "predicate": predicate,
                "scope_hint": pattern_def["scope_hint"],
            },
            evidence=[f"matched status pattern '{pattern_key}' → predicate '{predicate}'"],
        )

    # For shift-based statuses, resolve concrete shift_ids
    shifts = read_shift_definitions(db, group_id)
    matching_shift_ids = _filter_shifts(shifts, pattern_def.get("shift_filter", ""))

    if not matching_shift_ids:
        return GroundedConcept(
            concept_type=ConceptType.STATUS,
            input_text=surface_text,
            status=GroundingStatus.GROUNDED,
            grounded_value={
                "predicate": predicate,
                "shift_ids": [],
                "warning": "no shifts matched the filter — check shift definitions",
            },
            evidence=[f"pattern '{pattern_key}' matched but no shifts satisfy filter"],
        )

    return GroundedConcept(
        concept_type=ConceptType.STATUS,
        input_text=surface_text,
        status=GroundingStatus.GROUNDED,
        grounded_value={
            "predicate": predicate,
            "shift_ids": matching_shift_ids,
        },
        evidence=[
            f"pattern '{pattern_key}' → {len(matching_shift_ids)} shifts: "
            + ", ".join(matching_shift_ids[:10])
        ],
    )


def _filter_shifts(shifts: list[dict], filter_expr: str) -> list[str]:
    """Apply a simple filter expression to shift definitions.

    Supports: type == 'X', type != 'X', shift_gb == 'X'.
    """
    if not filter_expr:
        return [s["shift_id"] for s in shifts]

    results = []
    for s in shifts:
        if "type !=" in filter_expr:
            val = filter_expr.split("!=")[1].strip().strip("'\"")
            if s.get("type") != val:
                results.append(s["shift_id"])
        elif "type ==" in filter_expr:
            val = filter_expr.split("==")[1].strip().strip("'\"")
            if s.get("type") == val:
                results.append(s["shift_id"])
        elif "shift_gb ==" in filter_expr:
            val = filter_expr.split("==")[1].strip().strip("'\"")
            if s.get("shift_gb") == val:
                results.append(s["shift_id"])

    return results
