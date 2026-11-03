"""한 턴 실행 — 모든 에이전트가 같은 순서로 돈다.

  대화 꺼내기(잠금) → [끝난·남의 대화면: 말은 새 대화로 이어 받고, 버튼은 거절] → 서버 슬롯(기한 안에서만 대기)
  → 대상 확인(화면이 다른 대상으로 옮겼으면: 말은 그 대상으로 옮겨 잇고, 버튼은 거절) → 턴 번호 → handle
  → 최근 대화·흔적·대상 이동 → 응답 조립(흔적 블록을 맨 위에).
화면이 하던 판단(끝난 대화 다시 보내기·다른 버전의 버튼 막기·흔적 줄 만들기)을 여기로 모았다.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple

from sqlalchemy.orm import Session

from agent_kit import blocks as B
from agent_kit.base import Agent, TurnCall, TurnInput
from agent_kit.conversation import ENDED, Conversation, ConversationError

RENEWED = "이전 대화가 끝나 새 대화로 이어 갑니다."
#: 말(text)이면 새 대화로 이어 받는 code — 끝난 대화 + 남의 대화(같은 브라우저에서 다른 계정으로 다시 로그인하면
#: 앞 사람의 대화 id 가 남아 있다. 그 대화는 건드리지 않고 이 사람의 새 대화를 연다 · 검토 M1)
RENEWABLE = ENDED + ("forbidden",)


class TurnError(Exception):
    """요청을 처리하지 않고 돌려보낸다 — 화면에는 code·message 로 간다(detail)."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def checkout(agent: Agent, db: Session, user: Any, conversation_id: Optional[str], context: Dict[str, Any], *,
             renew: bool) -> Tuple[Conversation, bool]:
    """대화를 잠가서 꺼낸다(없으면 새로). renew=True 면 끝난 대화 대신 새 대화 — (대화, 새로 이어 받았나)."""
    owner = user.account_id
    if not conversation_id:
        return agent.store.start(owner, context), False
    try:
        return agent.store.checkout(db, conversation_id, owner), False
    except ConversationError as exc:
        if renew and exc.code in RENEWABLE:
            return agent.store.start(owner, context), True
        raise TurnError(exc.code, exc.message, exc.status) from exc


@contextmanager
def working(agent: Agent, db: Session, user: Any, conversation_id: Optional[str], context: Dict[str, Any],
            deadline: float, *, renew: bool) -> Iterator[Tuple[Conversation, bool]]:
    """대화 잠금 + 서버 슬롯. 슬롯 대기도 기한 안에서만(남은 시간이 최소 작업 시간보다 적으면 바로 429)."""
    conv, renewed = checkout(agent, db, user, conversation_id, context, renew=renew)
    fresh = renewed or not conversation_id
    try:
        wait = min(agent.slot_wait_seconds, deadline - time.monotonic() - agent.min_work_seconds)
        if wait <= 0 or not agent.slots.acquire(timeout=wait):
            if fresh:
                agent.store.close(conv.id)     # 아무것도 못 한 새 대화는 남기지 않는다(목록·사람당 한도)
            raise TurnError("server_busy", "다른 요청을 처리하고 있습니다. 잠시 뒤 다시 보내 주세요.", 429)
        try:
            yield conv, renewed
        finally:
            agent.slots.release()
    finally:
        agent.store.checkin(conv)


def run_turn(agent: Agent, db: Session, user: Any, conv: Conversation, inp: TurnInput, *,
             context: Dict[str, Any], deadline: float, renewed: bool = False) -> Dict[str, Any]:
    """잠근 대화에서 한 턴. context = 화면이 지금 보는 대상(authorize 를 거친 값)."""
    if conv.context != context:
        if inp.kind != "text":
            # 버튼은 그 대상에서 낸 결과에 대한 것 — 다른 버전에서 누른 '적용'이 엉뚱한 근무표를 바꾸지 않게
            raise TurnError("context_changed", agent.moved_message, 409)
        conv.move_to(context)           # 말은 새 대상에서 이어 간다
    agent.check_input(inp)
    conv.begin_turn()
    result = agent.handle(TurnCall(db=db, user=user, conv=conv, input=inp, today=agent.today(), deadline=deadline))
    conv.remember(result.said or inp.text or f"[{inp.kind}] {inp.action}", result.answer)
    if inp.kind == "text" and result.trail == "new":
        conv.request, conv.steps = result.request_line or inp.text, []
    elif inp.kind == "text" and result.trail == "step":
        conv.steps.append(inp.text or "")
    if result.step:
        conv.steps.append(result.step)
    if result.context is not None and result.context != conv.context:
        conv.move_to(result.context)
    shown = [B.notice(RENEWED)] if renewed else []
    if conv.request:
        shown.append(B.trail(conv.request, conv.steps))
    info = conv.info()
    return {
        "agent": agent.id, "conversation_id": conv.id, "turn_no": conv.turn_no, "context": dict(conv.context),
        "blocks": shown + list(result.blocks), "effects": list(result.effects),
        "status": {k: info[k] for k in ("turns_left", "idle_expires_in_s", "expires_in_s")},
    }
