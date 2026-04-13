"""Agents loader — reads .aide/AGENTS.md and fills context placeholders.

AGENTS.md is the always-loaded constitution for the agent.
It contains rules and topic references that are injected into every LLM call.
"""

from __future__ import annotations

from pathlib import Path

# Resolve .aide directory relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_AIDE_DIR = _PROJECT_ROOT / ".aide"
_AGENTS_FILE = _AIDE_DIR / "AGENTS.md"


def load_agents_prompt(
    *,
    year: int | None = None,
    month: int | None = None,
    group_id: str | None = None,
    user_role: str | None = None,
) -> str:
    """Load AGENTS.md and substitute context placeholders.

    Placeholders: {year}, {month}, {group_id}, {user_role}
    """
    if not _AGENTS_FILE.exists():
        return ""

    content = _AGENTS_FILE.read_text(encoding="utf-8")
    replacements = {
        "{year}": str(year) if year else "?",
        "{month}": str(month) if month else "?",
        "{group_id}": group_id or "?",
        "{user_role}": user_role or "nurse",
    }
    for placeholder, value in replacements.items():
        content = content.replace(placeholder, value)
    return content


def get_aide_dir() -> Path:
    """Return the resolved .aide directory path."""
    return _AIDE_DIR
