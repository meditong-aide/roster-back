"""대화 한 턴 — 말(chat)은 LLM 이 스킬을 고르고, 버튼(act)은 LLM 없이 스킬을 바로 부른다.

턴마다: 판정 재료 준비(없으면 열고, 근무표가 그사이 바뀌었으면 다시 연다) → LLM 1번(말일 때만) → 스킬 →
기록(message·propose·clarify·…) → 최근 대화에 한 줄. 적용으로 새 버전이 생기면 대화가 그 버전으로 옮겨 간다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from schedule_edit_agent.llm import CALL_TIMEOUT, EditLLM
from schedule_edit_agent.prompt import build_messages
from schedule_edit_agent.skills import (
    CHAT_BUDGET, MIN_SOLVE, OUT_OF_TIME, SKILLS, Outcome, SkillError, Turn, llm_tools, reply_text,
)
from schedule_edit_agent.store import Conversation
from services.schedule_edit.journal import record_event
from services.schedule_edit.serialize import allow_from_json, requests_from_json
from services.schedule_edit.session import EditSession, is_stale, open_session

logger = logging.getLogger(__name__)

LLM_FAILED = "지금 말씀을 해석하지 못했습니다. 잠시 뒤 다시 말씀해 주세요."
#: 버튼을 최근 대화에 남길 때의 말 — 다음 말('그 안에서 15일은 빼줘')이 무엇을 가리키는지 LLM 이 알게
BUTTON_TEXT = {"apply_edit": "{rank}번 안 적용", "reject_candidate": "{rank}번 안 넘기기",
               "keep_candidate": "{rank}번 안을 기준으로 다듬기", "choose_lever": "레버 고르기",
               "choose_transfer": "메우는 방식 고르기({choice_id})"}


@dataclass
class Reply:
    answer: str
    conversation_id: str
    preview: Optional[Dict[str, Any]] = None
    ui_actions: List[Dict[str, Any]] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)


class EditAgent:
    def __init__(self, llm_factory):
        self._llm_factory = llm_factory
        self._llm: Optional[EditLLM] = None

    @property
    def llm(self) -> EditLLM:
        if self._llm is None:
            self._llm = self._llm_factory()
        return self._llm

    def chat(self, db: Session, user, conv: Conversation, text: str, today: date, *,
             deadline: Optional[float] = None) -> Reply:
        """deadline: 경로에 들어온 순간 + CHAT_BUDGET(time.monotonic 기준) — 없으면 지금부터."""
        turn = self._turn(db, user, conv, today, deadline)
        started = time.monotonic()
        room = turn.remaining() - MIN_SOLVE
        if room < 1.0:      # 판정 재료 로드·대기로 이미 시간을 다 썼다 — LLM 도 솔버도 부르지 않는다
            turn.record("error", {"text": text, "stage": "budget", "remaining_ms": int(turn.remaining() * 1000)})
            return self._finish(conv, turn, text, Outcome(answer=OUT_OF_TIME))
        try:
            # 언제나 도구 하나 — 글로 답하면 사실을 지어낼 수 있다(바꾸지 않는 답은 reply_only 의 정해진 문장)
            response = self.llm.chat(build_messages(conv, turn.session, text, today), llm_tools(),
                                     tool_choice="required", timeout=min(CALL_TIMEOUT, room))
        except Exception as exc:   # noqa: BLE001 — 공급자 오류·키 없음 등. 대화는 이어 간다.
            logger.exception("[schedule_edit] LLM 호출 실패 conv=%s", conv.id)
            turn.record("error", {"text": text, "stage": "llm", "error": f"{type(exc).__name__}: {exc}"[:500]},
                        elapsed_ms=_ms(started))
            return self._finish(conv, turn, text, Outcome(answer=LLM_FAILED))
        usage = dict(getattr(self.llm, "last_usage", {}) or {})
        if not response.is_tool_call:
            # 도구를 강제했는데도 글이 왔다 — 그 글은 보여 주지 않고(분석용으로만 남긴다) 정해진 문장으로
            reply = reply_text("unclear", turn.session)
            turn.record("message", {"text": text, "reply": reply, "llm_text": (response.text or "")[:500],
                                    "usage": usage}, elapsed_ms=_ms(started))
            return self._finish(conv, turn, text, Outcome(answer=reply))
        call = response.tool_calls[0]
        turn.record("message", {"text": text, "tool": call.name, "args": call.args, "usage": usage,
                                "ignored_calls": [c.name for c in response.tool_calls[1:]]},
                    elapsed_ms=_ms(started))
        skill = SKILLS.get(call.name)
        if skill is None or not skill.llm:
            return self._finish(conv, turn, text, Outcome(answer="무엇을 바꿀지 다시 말씀해 주세요."))
        return self._finish(conv, turn, text, self._run(turn, skill.run, call.args or {}))

    def act(self, db: Session, user, conv: Conversation, action: str, params: Dict[str, Any],
            today: date, *, deadline: Optional[float] = None) -> Reply:
        skill = SKILLS.get(action)
        if skill is None or skill.llm:
            raise SkillError("알 수 없는 동작입니다.")
        turn = self._turn(db, user, conv, today, deadline)
        label = BUTTON_TEXT.get(action, action).format(rank=params.get("rank"), choice_id=params.get("choice_id"))
        return self._finish(conv, turn, f"[버튼] {label}", self._run(turn, skill.run, params))

    # ── 내부 ────────────────────────────────────────────────────────────────

    def _turn(self, db: Session, user, conv: Conversation, today: date, deadline: Optional[float]) -> Turn:
        # 기한은 판정 재료를 열기 전에 정한다 — 로드 시간도 예산 안이다(검토 HIGH-2)
        deadline = deadline if deadline is not None else time.monotonic() + CHAT_BUDGET
        conv.turn_no += 1
        session = self._session(db, conv)
        return Turn(db=db, user=user, conversation_id=conv.id, turn_no=conv.turn_no, session=session,
                    today=today, deadline=deadline)

    def _session(self, db: Session, conv: Conversation) -> EditSession:
        """판정 재료 — 없으면 열고, 근무표가 그사이 바뀌었으면 다시 연다(요청·레버·넘긴 묶음은 이어 간다)."""
        session = conv.session
        if session is not None and session.schedule_id == conv.schedule_id and not is_stale(db, session):
            return session
        carry = conv.restored or ({"requests": session.requests, "allow": session.allow,
                                   "avoid": session.avoid_people} if session is not None
                                  and session.schedule_id == conv.schedule_id else None)
        fresh = open_session(db, conv.schedule_id)
        if carry:
            fresh.requests = (requests_from_json(carry["requests"]) if conv.restored else list(carry["requests"]))
            fresh.allow = (allow_from_json(carry["allow"]) if conv.restored else dict(carry["allow"]))
            fresh.avoid_people = [frozenset(p) for p in carry["avoid"]]
        conv.session, conv.restored = fresh, None
        return fresh

    def _run(self, turn: Turn, run, params: Dict[str, Any]) -> Outcome:
        try:
            return run(turn, params)
        except SkillError as exc:
            # 버튼이 지금 상태와 안 맞았다(없는 후보 번호 등) — 분석용으로 남긴다
            turn.record("error", {"stage": "button", "params": params, "message": str(exc)})
            return Outcome(answer=str(exc))
        except Exception as exc:   # noqa: BLE001 — 기록을 남기고 위로(500)
            turn.record("error", {"stage": "skill", "error": f"{type(exc).__name__}: {exc}"[:500]})
            raise

    def _finish(self, conv: Conversation, turn: Turn, said: str, outcome: Outcome) -> Reply:
        conv.remember(said, outcome.answer)
        if turn.switched_to:
            conv.schedule_id, conv.session = turn.switched_to, None   # 새 버전에서 새로 시작한다
        return Reply(answer=outcome.answer, conversation_id=conv.id, preview=outcome.preview,
                     ui_actions=outcome.ui_actions,
                     data={"schedule_id": conv.schedule_id, "turn_no": conv.turn_no, **conv.info()})


def close_conversation(db: Session, user, conv: Conversation) -> None:
    record_event(db, conv.schedule_id, "close", {"turn_no": conv.turn_no},
                 created_by=getattr(user, "account_id", None), conversation_id=conv.id, turn_no=conv.turn_no)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
