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
    # 사람이 읽는 이름 — 프롬프트에는 id 가 아니라 이 이름만 노출한다 (DB 조회로 채움)
    group_name: str | None = None
    office_name: str | None = None
    # HN 이 관리하는 병동 이름들 (단일 병동이면 비움). 프롬프트에 이름만 노출.
    managed_group_names: list[str] = field(default_factory=list)

    # Auto-computed
    today: str = field(default_factory=lambda: date.today().isoformat())

    # Multi-turn conversation state
    conversation_id: str | None = None
    messages: list[dict] = field(default_factory=list)

    # Approval flow (preview → confirm)
    pending_approval: dict | None = None

    # Variable Memory (Routine step 간 파라미터 전달)
    variable_memory: dict = field(default_factory=dict)

    # 프론트 현재 화면 컨텍스트 (front→back readable) — 상대적 안내·프리필터용.
    # 예: {"current_route": "/roster_view", "month": 5}
    ui_metadata: dict | None = None
