"""Temporal grounder — resolve time-of-day expressions (아침, 야간, 이브닝, etc.)
to concrete shift codes by looking up start_time/end_time ranges.

Never assume "아침 = D". Each hospital defines shifts with different hours.
"""

from __future__ import annotations

from datetime import time

from sqlalchemy.orm import Session

from agents_v2.schemas.grounded_concept import (
    ConceptType,
    GroundedConcept,
    GroundingCandidate,
    GroundingStatus,
)
from agents_v2.tools.shift_tools import read_shift_definitions


# Time-of-day buckets used only to compute overlap, not to hard-code shift names.
_TIME_BUCKETS: dict[str, tuple[time, time]] = {
    "morning":   (time(5, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(18, 0)),
    "evening":   (time(14, 0), time(23, 0)),
    "night":     (time(21, 0), time(8, 0)),   # wraps midnight
}

# Korean surface tokens → bucket key (loose mapping for bucket selection only)
_KO_BUCKET_MAP: dict[str, str] = {
    "아침": "morning",
    "오전": "morning",
    "주간": "morning",
    "데이": "morning",
    "낮": "afternoon",
    "오후": "afternoon",
    "점심": "afternoon",
    "이브닝": "evening",
    "저녁": "evening",
    "야간": "night",
    "나이트": "night",
    "밤": "night",
    "심야": "night",
}


def ground_temporal(
    db: Session,
    group_id: str,
    surface_text: str,
) -> GroundedConcept:
    """Resolve a time-of-day expression to matching shift codes.

    Strategy:
    1. Map surface_text to a time bucket (morning/afternoon/evening/night).
    2. Fetch shift definitions and compare start_time against bucket range.
    3. Return all matching working shifts.
    """
    token = surface_text.strip()
    bucket_key = _resolve_bucket(token)

    if not bucket_key:
        return GroundedConcept(
            concept_type=ConceptType.TEMPORAL,
            input_text=surface_text,
            status=GroundingStatus.UNRESOLVED,
            evidence=[f"cannot map '{surface_text}' to a time-of-day bucket"],
        )

    bucket_start, bucket_end = _TIME_BUCKETS[bucket_key]
    shifts = read_shift_definitions(db, group_id)
    working_shifts = [s for s in shifts if s.get("is_working")]

    candidates: list[GroundingCandidate] = []
    for s in working_shifts:
        start_str = s.get("start_time")
        if not start_str:
            continue
        try:
            parts = start_str.split(":")
            shift_start = time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            continue

        if _time_in_bucket(shift_start, bucket_start, bucket_end):
            candidates.append(
                GroundingCandidate(
                    value={
                        "shift_id": s["shift_id"],
                        "name": s["name"],
                        "start_time": s["start_time"],
                        "end_time": s["end_time"],
                    },
                    confidence=0.9,
                    evidence=[
                        f"shift start {s['start_time']} falls in {bucket_key} bucket "
                        f"({bucket_start.strftime('%H:%M')}~{bucket_end.strftime('%H:%M')})"
                    ],
                )
            )

    if len(candidates) == 1:
        return GroundedConcept(
            concept_type=ConceptType.TEMPORAL,
            input_text=surface_text,
            status=GroundingStatus.GROUNDED,
            grounded_value=candidates[0].value,
            evidence=candidates[0].evidence,
        )

    if len(candidates) > 1:
        # Multiple shifts in the same time bucket — return all as grounded set
        return GroundedConcept(
            concept_type=ConceptType.TEMPORAL,
            input_text=surface_text,
            status=GroundingStatus.GROUNDED,
            grounded_value=[c.value for c in candidates],
            evidence=[
                f"found {len(candidates)} shifts in {bucket_key} bucket: "
                + ", ".join(c.value["shift_id"] for c in candidates)
            ],
        )

    return GroundedConcept(
        concept_type=ConceptType.TEMPORAL,
        input_text=surface_text,
        status=GroundingStatus.UNRESOLVED,
        evidence=[f"no working shifts match {bucket_key} time range in group {group_id}"],
    )


def _resolve_bucket(token: str) -> str | None:
    """Map a Korean/English token to a time bucket key."""
    lower = token.lower()
    # Direct match in Korean map
    for ko, bucket in _KO_BUCKET_MAP.items():
        if ko in lower:
            return bucket
    # English fallback
    for eng_key in _TIME_BUCKETS:
        if eng_key in lower:
            return eng_key
    return None


def _time_in_bucket(t: time, bucket_start: time, bucket_end: time) -> bool:
    """Check if a time falls within a bucket range (handles midnight wrap)."""
    if bucket_start <= bucket_end:
        return bucket_start <= t < bucket_end
    # Wraps midnight (e.g., night: 21:00 ~ 08:00)
    return t >= bucket_start or t < bucket_end
