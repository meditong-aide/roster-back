"""Skill registry — central dispatcher that maps skill names to implementations."""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

# Lazy imports to avoid circular dependencies
SkillFunc = Callable[[Session, dict], Any]

SKILL_REGISTRY: dict[str, SkillFunc] = {}


def register(name: str):
    """Decorator to register a skill function."""
    def decorator(func: SkillFunc) -> SkillFunc:
        SKILL_REGISTRY[name] = func
        return func
    return decorator


def run_skill(db: Session, skill_name: str, params: dict) -> Any:
    """Execute a skill by name.

    This is the SkillRunner callable expected by the executor.
    Accepts both hyphenated (query-schedule) and underscored (query_schedule) names.

    Args:
        db: Database session.
        skill_name: Registered skill name (e.g. "query-schedule" or "query_schedule").
        params: Parameters dict from the execution plan.

    Returns:
        Skill execution result (data dict/list).

    Raises:
        KeyError: If skill_name is not registered.
    """
    _ensure_loaded()
    # Normalize: try as-is, then hyphenated, then underscored
    for candidate in (skill_name, skill_name.replace("_", "-"), skill_name.replace("-", "_")):
        if candidate in SKILL_REGISTRY:
            return SKILL_REGISTRY[candidate](db, params)
    raise KeyError(f"Unknown skill: {skill_name}")


_loaded = False

def _ensure_loaded():
    """Import all skill modules to trigger registration."""
    global _loaded
    if _loaded:
        return
    import agents_v2.skills.query_schedule
    import agents_v2.skills.bulk_mutation
    import agents_v2.skills.update_constraint
    import agents_v2.skills.update_person_attr
    import agents_v2.skills.generate_schedule
    import agents_v2.skills.validate_schedule
    import agents_v2.skills.recommend_candidates
    import agents_v2.skills.repair_schedule
    import agents_v2.skills.analyze_report
    import agents_v2.skills.update_monthly_limit
    import agents_v2.skills.manage_grade
    import agents_v2.skills.manage_team_min
    import agents_v2.skills.query_generation_job
    import agents_v2.skills.manage_wanted_deadline
    import agents_v2.skills.manage_teams
    import agents_v2.skills.manage_wanted_limits
    import agents_v2.skills.publish_schedule
    import agents_v2.skills.resolve_infeasibility
    # 매니페스트(@skill) 기반 신규 스킬도 로드 — run_skill 이 핸들러를 찾도록.
    from agents_v2.skills.manifest import load_manifest_skills

    load_manifest_skills()
    _loaded = True
