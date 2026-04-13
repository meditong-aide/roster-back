"""Memory manager — runtime memory for the .aide/memory/ directory.

Provides read/write access to the agent's runtime memory.
Memory stores session-persistent facts like confirmed preferences,
resolved ambiguities, and user corrections during a conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

from agents_v2.harness.agents_loader import get_aide_dir

_MEMORY_DIR = get_aide_dir() / "memory"
_MEMORY_INDEX = _MEMORY_DIR / "MEMORY.md"


@dataclass
class MemoryEntry:
    """A single memory entry."""
    title: str
    filename: str
    hook: str  # one-line description
    content: str = ""


def read_memory_index() -> list[MemoryEntry]:
    """Read the MEMORY.md index and return parsed entries.

    Each line in MEMORY.md follows: - [Title](file.md) — hook
    """
    if not _MEMORY_INDEX.exists():
        return []

    entries = []
    content = _MEMORY_INDEX.read_text(encoding="utf-8")
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"- \[(.+?)\]\((.+?)\)\s*—\s*(.*)", line)
        if match:
            entries.append(
                MemoryEntry(
                    title=match.group(1),
                    filename=match.group(2),
                    hook=match.group(3).strip(),
                )
            )
    return entries


def load_memory(filename: str) -> str | None:
    """Load a specific memory file's content."""
    path = _MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def save_memory(
    title: str,
    filename: str,
    hook: str,
    content: str,
) -> None:
    """Save a memory entry — write file and update index.

    Args:
        title: Display title for the entry.
        filename: File name in memory dir (e.g. "confirmed_nurse.md").
        hook: One-line description for the index.
        content: Full content of the memory file.
    """
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # Write the memory file
    path = _MEMORY_DIR / filename
    path.write_text(content, encoding="utf-8")

    # Update index — append if not already present, update if exists
    existing = read_memory_index()
    entry_line = f"- [{title}]({filename}) — {hook}"

    if any(e.filename == filename for e in existing):
        # Update existing entry in index
        lines = _MEMORY_INDEX.read_text(encoding="utf-8").split("\n")
        updated_lines = []
        for line in lines:
            if f"]({filename})" in line:
                updated_lines.append(entry_line)
            else:
                updated_lines.append(line)
        _MEMORY_INDEX.write_text("\n".join(updated_lines), encoding="utf-8")
    else:
        # Append new entry
        with open(_MEMORY_INDEX, "a", encoding="utf-8") as f:
            f.write(f"\n{entry_line}")


def remove_memory(filename: str) -> bool:
    """Remove a memory entry — delete file and update index."""
    path = _MEMORY_DIR / filename
    if not path.exists():
        return False

    path.unlink()

    # Remove from index
    if _MEMORY_INDEX.exists():
        lines = _MEMORY_INDEX.read_text(encoding="utf-8").split("\n")
        filtered = [l for l in lines if f"]({filename})" not in l]
        _MEMORY_INDEX.write_text("\n".join(filtered), encoding="utf-8")

    return True
