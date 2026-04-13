"""Agent response — what the agent returns to the caller."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentResponse:
    """Unified response from the scheduling agent."""

    # Main answer text (natural language)
    answer: str = ""

    # Structured data payload (tables, lists, etc.)
    data: Any = None

    # Clarification flow
    needs_clarification: bool = False
    clarification_question: str | None = None
    clarification_options: list[str] = field(default_factory=list)

    # Approval flow
    awaiting_approval: bool = False
    preview: dict | None = None  # affected rows, summary, etc.

    # Execution trace for transparency
    grounding_trace: list[dict] = field(default_factory=list)
    execution_trace: list[dict] = field(default_factory=list)

    # Error
    error: str | None = None

    # ── helpers ──────────────────────────────────────────────

    @property
    def is_complete(self) -> bool:
        return not self.needs_clarification and not self.awaiting_approval and not self.error

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"answer": self.answer}
        if self.data is not None:
            d["data"] = self.data
        if self.needs_clarification:
            d["needs_clarification"] = True
            d["clarification_question"] = self.clarification_question
            if self.clarification_options:
                d["clarification_options"] = self.clarification_options
        if self.awaiting_approval:
            d["awaiting_approval"] = True
            d["preview"] = self.preview
        if self.error:
            d["error"] = self.error
        if self.grounding_trace:
            d["grounding_trace"] = self.grounding_trace
        if self.execution_trace:
            d["execution_trace"] = self.execution_trace
        return d
