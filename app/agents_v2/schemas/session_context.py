"""Session context — state passed from HTTP request through to agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class SessionContext:
    """Everything the agent needs to know about the current session."""

    # Required — selected at login / request time
    office_id: str
    group_id: str
    year: int
    month: int

    # User identity
    nurse_id: str | None = None
    nurse_name: str | None = None
    user_role: str = "nurse"  # "HN" | "nurse"
    group_name: str | None = None

    # Auto-computed
    today: str = field(default_factory=lambda: date.today().isoformat())

    # Multi-turn conversation state
    conversation_id: str | None = None
    messages: list[dict] = field(default_factory=list)

    # Approval flow (preview → confirm)
    pending_approval: dict | None = None

    # Variable Memory (Routine step 간 파라미터 전달)
    variable_memory: dict = field(default_factory=dict)
