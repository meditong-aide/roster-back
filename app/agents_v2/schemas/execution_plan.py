"""Execution plan — what the planner produces for the executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(str, Enum):
    NONE = "none"          # read-only
    LOW = "low"            # single write
    MEDIUM = "medium"      # bulk write
    HIGH = "high"          # bulk write + generate / publish


@dataclass
class PlanStep:
    """A single step in the execution plan."""
    skill_name: str
    tool_calls: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    depends_on: list[int] = field(default_factory=list)  # indices of prior steps


@dataclass
class ExecutionPlan:
    """The full execution plan for a canonical query."""

    steps: list[PlanStep] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.NONE
    needs_preview: bool = False
    needs_approval: bool = False
    estimated_affected_count: int | None = None

    # Clarification needed before execution
    pending_clarification: str | None = None

    # ── helpers ──────────────────────────────────────────────

    @property
    def is_blocked(self) -> bool:
        return self.pending_clarification is not None

    @property
    def skill_names(self) -> list[str]:
        return [s.skill_name for s in self.steps]

    def to_dict(self) -> dict:
        return {
            "steps": [
                {
                    "skill_name": s.skill_name,
                    "tool_calls": s.tool_calls,
                    "parameters": s.parameters,
                    "description": s.description,
                    "depends_on": s.depends_on,
                }
                for s in self.steps
            ],
            "risk_level": self.risk_level.value,
            "needs_preview": self.needs_preview,
            "needs_approval": self.needs_approval,
            "estimated_affected_count": self.estimated_affected_count,
            "pending_clarification": self.pending_clarification,
        }
