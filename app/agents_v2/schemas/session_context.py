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

    # Clarify flow (다중 되물음 → clarify_form → 답변 병합 재개)
    #   pending_clarify: {"plan": <plan dict>, "user_message": str} — clarify_form 방출 시 저장
    #   clarify_answers: [{"task","param","value"}, ...] — 프론트가 채워 보내면 재개 트리거
    pending_clarify: dict | None = None
    clarify_answers: list[dict] | None = None

    # 직전 턴의 라우팅 결과 — 후속 턴 스코프 재사용용.
    #   {"categories": [...], "tool_names": [...]}
    # 직전 턴이 사용자에게 되물었다면(awaited_reply) 이번 발화는 그 답이므로 주제가
    # 같다. 라우터를 다시 부르지 않고 이 스코프를 그대로 쓴다(LLM 호출 0회).
    last_route: dict | None = None
    # 직전 턴이 clarification 으로 끝났는가(= 사용자에게 되물었는가).
    # 승인대기(pending_approval)는 별도 필드가 이미 담당한다.
    awaited_reply: bool = False

    # 자율성 모드 (HITL 3-tier). mutation 승인 정책을 고른다.
    #   "manual"      — 모든 mutation 은 preview→사용자 승인 (현행, 안전 기본)
    #   "auto"        — read/navigate 자동, mutation 은 여전히 승인 (추후)
    #   "auto_accept" — mutation 도 자동 실행 (HN 한정, 위험 — 추후)
    # 지금은 manual 만 활성. auto tier 는 스캐폴드.
    autonomy_mode: str = "manual"

    # Variable Memory (Routine step 간 파라미터 전달)
    variable_memory: dict = field(default_factory=dict)

    # 프론트 현재 화면 컨텍스트 (front→back readable) — 상대적 안내·프리필터용.
    # 예: {"current_route": "/roster_view", "month": 5}
    ui_metadata: dict | None = None
