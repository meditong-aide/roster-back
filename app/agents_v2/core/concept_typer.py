"""Concept typer — uses LLM to classify surface expression fragments
into ConceptType categories before grounding.

This is the FIRST pipeline stage. It decomposes the user message into
typed fragments without resolving them. Resolution happens in the grounding layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents_v2.schemas.grounded_concept import ConceptType


@dataclass
class TypedFragment:
    """A fragment of user input with its classified concept type."""
    text: str
    concept_type: ConceptType
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


# System prompt for LLM-based concept classification.
CONCEPT_TYPING_PROMPT = """\
You are a concept classifier for a nurse scheduling system.

Given a user message about nurse scheduling, decompose it into typed fragments.

## Concept Types

- **lexical**: Short shift code aliases — D, E, N, V, OFF, 데이, 이브, 나이트, etc.
- **temporal**: Time-of-day expressions — 아침 근무, 오전, 야간, 이브닝, etc.
- **status**: Status-based expressions — 쉬는사람, 근무자, 휴가자, 미제출자, 신규 간호사, etc.
- **workflow**: Action/intent expressions — 적용 안되게, 다시 생성해, 취소해줘, 변경해, etc.
- **entity**: Person/team/group references — 김민지, 박지은, A팀, 9병동, etc.

## Rules

1. One fragment can have exactly ONE concept type.
2. Extract ALL meaningful fragments (skip filler words like 의, 를, 에서, 좀, etc.).
3. Keep fragments as short as possible while preserving meaning.
4. Context words like year/month are NOT fragments — they are session context.
5. Return as JSON array of objects with "text" and "type" fields.

## Examples

User: "김민지 아침 근무 OFF로 변경해줘"
→ [
    {"text": "김민지", "type": "entity"},
    {"text": "아침 근무", "type": "temporal"},
    {"text": "OFF", "type": "lexical"},
    {"text": "변경해줘", "type": "workflow"}
  ]

User: "이번달 나이트 3회 이상인 사람"
→ [
    {"text": "나이트", "type": "temporal"},
    {"text": "3회 이상인 사람", "type": "status"}
  ]

User: "조정판에서 쉬는사람 적용 안되게 해줘"
→ [
    {"text": "조정판", "type": "lexical"},
    {"text": "쉬는사람", "type": "status"},
    {"text": "적용 안되게", "type": "workflow"}
  ]
"""


def classify_concepts(
    user_message: str,
    llm_call: Any,
    *,
    session_context: dict | None = None,
) -> list[TypedFragment]:
    """Classify user message into typed concept fragments using LLM.

    Args:
        user_message: Raw user input.
        llm_call: Callable that takes (system_prompt, user_prompt) → str (JSON).
                  This is injected to keep the core layer LLM-agnostic.
        session_context: Optional context (year, month, group_id) for the LLM.

    Returns:
        List of TypedFragment with concept types assigned.
    """
    context_hint = ""
    if session_context:
        parts = []
        if session_context.get("year"):
            parts.append(f"year={session_context['year']}")
        if session_context.get("month"):
            parts.append(f"month={session_context['month']}")
        if session_context.get("group_id"):
            parts.append(f"group={session_context['group_id']}")
        if parts:
            context_hint = f"\n\nSession context: {', '.join(parts)}"

    user_prompt = f"User message: \"{user_message}\"{context_hint}\n\nExtract typed fragments as JSON array:"

    raw_response = llm_call(CONCEPT_TYPING_PROMPT, user_prompt)
    return _parse_llm_response(raw_response, user_message)


def _parse_llm_response(raw: str, original_message: str) -> list[TypedFragment]:
    """Parse LLM JSON response into TypedFragment list."""
    import json

    # Extract JSON array from response (handle markdown code blocks)
    text = raw.strip()
    if "```" in text:
        # Extract content between code fences
        parts = text.split("```")
        for part in parts[1:]:
            cleaned = part.strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            if cleaned.startswith("["):
                text = cleaned
                break

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: treat entire message as a single unknown fragment
        return [
            TypedFragment(
                text=original_message,
                concept_type=ConceptType.WORKFLOW,
                confidence=0.3,
                metadata={"parse_error": True},
            )
        ]

    fragments = []
    type_map = {
        "lexical": ConceptType.LEXICAL,
        "temporal": ConceptType.TEMPORAL,
        "status": ConceptType.STATUS,
        "workflow": ConceptType.WORKFLOW,
        "entity": ConceptType.ENTITY,
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        ctype = type_map.get(item.get("type", "").lower())
        if ctype is None:
            continue
        fragments.append(
            TypedFragment(
                text=item.get("text", ""),
                concept_type=ctype,
                confidence=item.get("confidence", 0.8),
            )
        )
    return fragments
