"""Skill matcher — progressive disclosure of .aide/skills/*/SKILL.md.

Skills are matched by description first (from YAML frontmatter).
Full skill content is loaded only when the skill is actually invoked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agents_v2.harness.agents_loader import get_aide_dir

_SKILLS_DIR = get_aide_dir() / "skills"


@dataclass
class SkillMeta:
    """Parsed SKILL.md frontmatter."""
    name: str
    description: str
    path: Path


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Extract YAML frontmatter from a markdown file."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    frontmatter = {}
    for line in match.group(1).strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            frontmatter[key.strip()] = val.strip()
    return frontmatter


def list_skills() -> list[SkillMeta]:
    """List all available skills with their metadata.

    Reads only frontmatter (name + description) for matching,
    not the full skill body.
    """
    skills = []
    if not _SKILLS_DIR.exists():
        return skills

    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        content = skill_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        if fm.get("name"):
            skills.append(
                SkillMeta(
                    name=fm["name"],
                    description=fm.get("description", ""),
                    path=skill_file,
                )
            )
    return skills


def match_skill(skill_name: str) -> SkillMeta | None:
    """Find a skill by exact name match.

    Args:
        skill_name: The skill name to look up (e.g. "query-schedule").

    Returns:
        SkillMeta if found, None otherwise.
    """
    for skill in list_skills():
        if skill.name == skill_name:
            return skill
    return None


def load_skill(skill_name: str) -> str | None:
    """Load the full SKILL.md content for a skill (progressive disclosure).

    This should only be called when the skill is actually being invoked,
    not during planning/matching.

    Args:
        skill_name: The skill name to load.

    Returns:
        Full markdown content, or None if not found.
    """
    meta = match_skill(skill_name)
    if not meta:
        return None
    return meta.path.read_text(encoding="utf-8")


def get_skill_descriptions() -> list[dict[str, str]]:
    """Get all skill names and descriptions for LLM context.

    Used for skill selection — provides just enough info
    for the LLM/planner to choose the right skill.
    """
    return [
        {"name": s.name, "description": s.description}
        for s in list_skills()
    ]
