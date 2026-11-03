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
from agent_kit.conversation import ENDED, Conversation, ConversationError, said_view

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
    # 화면에 보일 사용자 쪽 말 — 버튼은 흔적 한 줄('그래도 진행')이 누른 것을 가장 잘 말한다
    said = result.said or inp.text or f"[{inp.kind}] {inp.action}"
    shown = ({"kind": "text", "text": inp.text or ""} if inp.kind == "text"
             else {"kind": "action", "text": result.step or said_view(said)["text"]})
    conv.remember(said, result.answer, shown)
    if inp.kind == "text" and result.trail == "new":
        conv.request, conv.steps = result.request_line or inp.text, []
    elif inp.kind == "text" and result.trail == "step":
        conv.steps.append(inp.text or "")
    if result.step:
        conv.steps.append(result.step)
    if result.context is not None and result.context != conv.context:
        conv.move_to(result.context)
    conv.last_blocks = list(result.blocks)
    return _response(agent, conv, list(result.blocks), list(result.effects), renewed=renewed)


@contextmanager
def viewing(agent: Agent, db: Session, user: Any, conversation_id: str) -> Iterator[Conversation]:
    """이어 보기용 대화 잠금 — 턴 한도·만료는 보지 않는다(다 쓴 대화도 마지막 결과는 다시 본다 · 버튼은 그 뒤 턴이
    거절한다). 서버 슬롯은 잡지 않는다 — 메모리 블록을 다시 보이는 데 풀이 자리를 기다리게 하지 않는다(검토 11-03)."""
    try:
        conv = agent.store.checkout(db, conversation_id, user.account_id, check_limits=False)
    except ConversationError as exc:
        raise TurnError(exc.code, exc.message, exc.status) from exc
    try:
        yield conv
    finally:
        agent.store.checkin(conv)


def view_turn(agent: Agent, db: Session, user: Any, conv: Conversation, context: Dict[str, Any],
              deadline: float) -> Dict[str, Any]:
    """새로 고친 뒤 이어 보기 — 턴을 쓰지 않고 지난 턴들과 마지막 블록(버튼 포함)을 다시 그린다.
    메모리에 블록이 없으면(재기동·내려간 대화) 에이전트가 지금 상태로 다시 그린다(`Agent.current` · 판정 재료를 다시
    읽을 수 있어 그때만 서버 슬롯을 잡는다)."""
    if conv.context != context:
        raise TurnError("context_changed", agent.moved_message, 409)
    blocks = conv.last_blocks
    if not blocks and conv.history:
        wait = min(agent.slot_wait_seconds, deadline - time.monotonic() - agent.min_work_seconds)
        if wait <= 0 or not agent.slots.acquire(timeout=wait):
            raise TurnError("server_busy", "다른 요청을 처리하고 있습니다. 잠시 뒤 다시 열어 주세요.", 429)
        try:
            blocks = agent.current(db, user, conv)
        finally:
            agent.slots.release()
    conv.last_blocks = list(blocks)
    return _response(agent, conv, list(blocks), [])


def _response(agent: Agent, conv: Conversation, blocks, effects, *, renewed: bool = False) -> Dict[str, Any]:
    shown = [B.notice(RENEWED)] if renewed else []
    if conv.request:
        shown.append(B.trail(conv.request, conv.steps))
    info = conv.info()
    last = conv.history[-1] if conv.history else None
    return {
        "agent": agent.id, "conversation_id": conv.id, "turn_no": conv.turn_no, "context": dict(conv.context),
        "blocks": shown + blocks, "effects": effects,
        "status": {k: info[k] for k in ("turns_left", "idle_expires_in_s", "expires_in_s")},
        # 대화 흐름 — 지난 턴(말·답 문장)과 이 턴의 사용자 쪽 말. 버튼·카드는 이 턴 블록에만 있다
        "history": conv.past(), "said": (last.get("shown") or said_view(last["user"])) if last else None,
        "history_cut": conv.cut(),
    }
