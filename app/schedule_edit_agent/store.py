"""근무표 수정 대화의 보관 값과 복원 — 보관·잠금·한도 자체는 공통 틀(`agent_kit.conversation`).

  · 실행 중 상태는 서버 메모리, 모든 턴은 `schedule_edit_log`(사용자 결정 11-02).
  · 기록에서 다시 세우는 것: 대상 근무표·턴 번호·지금 요청·허용 레버·넘긴 묶음·최근 대화 — 메모리에서
    내렸거나 서버가 재기동된 뒤에도 대화가 이어진다. 후보 자체는 다시 찾는다(솔버가 다시 낸다).
한도(사용자 지시 11-02 · 환경변수): 유휴 SCHEDULE_EDIT_IDLE_MIN 분 · 대화 SCHEDULE_EDIT_MAX_HOURS 시간 ·
SCHEDULE_EDIT_MAX_TURNS 턴 · 메모리 200개 · 사람당 3개.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from agent_kit.conversation import Conversation, ConversationError, Limits
from schedule_edit_agent.answer import SOURCE_WORD
from services.schedule_edit.journal import read_conversation, read_conversation_head

AGENT_ID = "schedule-edit"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


LIMITS = Limits(
    idle_seconds=_env_float("SCHEDULE_EDIT_IDLE_MIN", 30) * 60,
    max_age_seconds=_env_float("SCHEDULE_EDIT_MAX_HOURS", 8) * 3600,
    max_turns=int(_env_float("SCHEDULE_EDIT_MAX_TURNS", 40)),
    max_items=200,
    max_per_user=3,
    history_keep=8,
)


def restore(db: Session, conversation_id: str) -> Conversation:
    """기록에서 대화를 다시 세운다. 마지막 제안(propose) 기록에 그때의 요청·허용 레버·넘긴 묶음이 들어 있다.

    주인·시작 시각은 대화의 첫 기록에서(긴 대화도 최근 기록만 읽으니 따로 읽는다 · 검토 L1), 대상 버전은
    마지막 기록에서 — 화면에서 다른 버전으로 옮겨 간 턴은 그 뒤 기록의 schedule_id 에 남는다(검토 M5).
    버전이 바뀐 지점에서는 앞 버전의 요청·레버·넘긴 묶음을 버린다. 흔적은 마지막 말 한 줄만 되살린다.
    """
    rows = read_conversation(db, conversation_id)
    head = read_conversation_head(db, conversation_id)
    if not rows or head is None:
        raise ConversationError("not_found", "대화를 찾을 수 없습니다. 새 대화로 시작해 주세요.", 404)
    if any(row.event == "close" for row in rows):
        raise ConversationError("closed", "닫은 대화입니다. 새 대화로 시작해 주세요.", 410)
    conv = Conversation(id=conversation_id, agent=AGENT_ID, owner=str(head.created_by or ""),
                        context={"schedule_id": rows[0].schedule_id}, created_at=head.created_at.timestamp(),
                        touched_at=time.time(), limits=LIMITS)
    empty = {"requests": [], "allow": [], "avoid": [], "confirmed": [], "asking": [], "held": None}
    state: Dict[str, Any] = dict(empty)
    said: Optional[str] = None
    for row in rows:
        if row.schedule_id != conv.context["schedule_id"]:
            conv.context, state = {"schedule_id": row.schedule_id}, dict(empty)
        conv.turn_no = max(conv.turn_no, int(row.turn_no or 0))
        payload = row.payload or {}
        if row.event == "message":
            said = payload.get("text")
            if payload.get("tool") in ("propose_edit", "run_postprocess"):   # 흔적의 요청 줄은 새 요청만
                conv.request = said or conv.request
            if payload.get("reply") is not None:          # 도구 없이 끝난 턴(정해진 문장)
                conv.remember(said or "", payload["reply"])
                said = None
        elif row.event in ("propose", "clarify"):
            if row.event == "propose":
                result = payload.get("result") or {}
                asking = result.get("findings") or [] if result.get("status") == "confirm" else []
                # 바꾸기 전 확인을 묻던 중이면 그 확인과, [바꾸지 않기]면 돌아갈 앞 요청(held)을 되살린다
                held = (state.get("held") or {k: state.get(k) or [] for k in ("requests", "allow", "avoid")}) \
                    if asking else None
                state = {**{k: payload.get(k) or [] for k in ("requests", "allow", "avoid")},
                         "confirmed": state.get("confirmed") or [], "asking": asking, "held": held}
                # 흔적의 요청 줄은 해석한 요청으로(화면에 보였던 것과 같게 · 평가 11-03 U1)
                line = SOURCE_WORD.get(payload["postprocess"]) if payload.get("postprocess") else payload.get("described")
                conv.request = line or conv.request
            if said is not None:
                conv.remember(said, payload.get("answer", ""))
                said = None
        elif row.event == "confirm":
            # 동의로 넘긴 허용량도 함께 — 다음 제안 기록 전에 끊기면(시간 부족·재기동) 동의만 남고 양이 빠진다(검토 M1)
            state = {**state, "confirmed": list(state.get("confirmed") or []) + list(payload.get("ids") or []),
                     "allow": payload.get("allow_total") or state.get("allow") or [], "asking": []}
        elif row.event == "cancel":
            # [바꾸지 않기] — 묻기 전 요청으로 돌아갔다
            state = {**{k: payload.get(k) or [] for k in ("requests", "allow", "avoid")},
                     "confirmed": state.get("confirmed") or [], "asking": [], "held": None}
        elif row.event == "apply" and row.new_schedule_id:
            # 적용 뒤에는 새 버전에서 이어 간다
            conv.context, state = {"schedule_id": row.new_schedule_id}, dict(empty)
    conv.restored = state
    return conv
