"""Topic selector — selective recall of .aide/topics/ files.

Topics are loaded on demand based on relevance to the current query.
Not all topics are injected every time — only when the concept types
or workflow patterns in the query suggest they're needed.
"""

from __future__ import annotations

from pathlib import Path

from agents_v2.harness.agents_loader import get_aide_dir
from agents_v2.schemas.grounded_concept import ConceptType

_TOPICS_DIR = get_aide_dir() / "topics"

# Mapping: concept types / keywords → relevant topic files
_TOPIC_RELEVANCE: dict[str, list[str]] = {
    "concept-grounding": [
        ConceptType.LEXICAL.value,
        ConceptType.TEMPORAL.value,
        ConceptType.STATUS.value,
        ConceptType.ENTITY.value,
    ],
    "workflow-patterns": [
        ConceptType.WORKFLOW.value,
        "mutation",
        "generate",
        "bulk",
        "update",
    ],
    "safety-and-approval": [
        "mutation",
        "generate",
        "bulk_update",
        "publish",
        "delete",
        "approval",
        "preview",
    ],
}


def select_topics(
    concept_types: list[ConceptType] | None = None,
    keywords: list[str] | None = None,
) -> list[str]:
    """Select which topic files are relevant for the current query.

    Args:
        concept_types: Concept types detected in the query.
        keywords: Additional keywords (operation names, etc.)

    Returns:
        List of topic file names (without extension) to load.
    """
    signals: set[str] = set()
    if concept_types:
        signals.update(ct.value for ct in concept_types)
    if keywords:
        signals.update(k.lower() for k in keywords)

    selected = []
    for topic_name, triggers in _TOPIC_RELEVANCE.items():
        if signals & set(triggers):
            selected.append(topic_name)

    return selected


def load_topic(topic_name: str) -> str | None:
    """Load a topic file's content by name.

    Args:
        topic_name: Name without extension, e.g. "concept-grounding"

    Returns:
        File content as string, or None if not found.
    """
    path = _TOPICS_DIR / f"{topic_name}.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def load_selected_topics(
    concept_types: list[ConceptType] | None = None,
    keywords: list[str] | None = None,
) -> dict[str, str]:
    """Select and load all relevant topics in one call.

    Returns:
        Dict of topic_name → content.
    """
    names = select_topics(concept_types, keywords)
    result = {}
    for name in names:
        content = load_topic(name)
        if content:
            result[name] = content
    return result
