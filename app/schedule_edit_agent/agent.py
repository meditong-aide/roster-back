"""근무표 수정 한 턴의 핵심 — 말(chat)은 LLM 이 스킬을 고르고, 버튼(act)은 LLM 없이 스킬을 바로 부른다.
처음 화면 예시처럼 해석이 이미 붙은 말(chat 의 call)도 LLM 없이 그 도구를 부른다(사용자 지시 11-06 · 토큰 0).
스킬 실행은 늘 `middleware.execute_skill`(AIDE `execute_skill` 의 얇은 껍데기 — ⓪ 범위 → ① 권한 → 문맥 주입 → 이름 확정 → 실행 → 감사)을 거친다.

턴마다: 판정 재료 준비(없으면 열고, 근무표가 그사이 바뀌었으면 다시 연다) → LLM 1번(말일 때만) → 스킬 → 기록
(message·propose·clarify·…). 턴 번호·최근 대화·흔적·대상 이동(적용 → 새 버전)은 공통 틀(`agent_kit.runtime`)이
하고, 여기는 결과(Reply)만 돌려준다 — 화면 블록은 `view.py` 가 만든다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from agent_kit.conversation import Conversation
from schedule_edit_agent import registry
from schedule_edit_agent.answer import current_request
from schedule_edit_agent.descriptions import SKILL_TOOLS
from schedule_edit_agent.llm import CALL_TIMEOUT, EditLLM
from schedule_edit_agent.middleware import execute_skill
from schedule_edit_agent.prompt import build_messages
from schedule_edit_agent.skills import CHAT_BUDGET, MIN_SOLVE, OUT_OF_TIME, Outcome, SkillError, Turn, reply_text
from services.schedule_edit.journal import record_event
from services.schedule_edit.precheck import finding_from_json
from services.schedule_edit.serialize import allow_from_json, requests_from_json
from services.schedule_edit.session import EditSession, is_stale, open_session

logger = logging.getLogger(__name__)

LLM_FAILED = "지금 말씀을 해석하지 못했습니다. 잠시 뒤 다시 말씀해 주세요."
#: 버튼을 최근 대화에 남길 때의 말 — 다음 말('그 안에서 15일은 빼줘')이 무엇을 가리키는지 LLM 이 알게
BUTTON_TEXT = {"apply_edit": "{rank}번 안 적용", "reject_candidate": "{rank}번 안 넘기기",
               "keep_candidate": "{rank}번 안을 기준으로 다듬기", "choose_lever": "레버 고르기",
               "choose_transfer": "메우는 방식 고르기({choice_id})", "confirm_request": "그래도 진행",
               "cancel_request": "바꾸지 않기", "back_to_candidates": "다른 안 보기",
               "retry_request": "같은 요청으로 다시 찾기", "open_version": "그 버전 열기",
               "choose_staffing": "인원 계획 고르기({choice_id})"}
#: 말이 흔적을 바꾸는 방식 — 새 요청은 흔적을 새로, 고쳐 말하면 단계로. 그 밖(상태 질문·안내)은 그대로
#: (실화면 확인 11-03: '반영 안 된 거지?' 가 요청 줄을 덮어썼다)
TRAIL = {"propose_edit": "new", "revise_edit": "step", "run_postprocess": "new", "answer_confirm": "step",
         "revert_version": "new"}


@dataclass
class Reply:
    answer: str
    preview: Optional[Dict[str, Any]] = None
    effects: List[Dict[str, Any]] = field(default_factory=list)
    #: 최근 대화에 남길 사용자 쪽 말(버튼) — 말이면 None(원문을 쓴다)
    said: Optional[str] = None
    #: 적용으로 대상이 바뀌면 새 대상 {"schedule_id"}
    context: Optional[Dict[str, Any]] = None
    focus: Optional[int] = None
    failed: bool = False
    #: 말 턴이 흔적을 어떻게 바꾸나(agent_kit TurnResult.trail) — 스킬로 정한다(TRAIL)
    trail: str = "keep"
    #: 새 요청이면 흔적의 요청 줄(해석한 요청)
    request_line: Optional[str] = None


def schedule_of(conv: Conversation) -> str:
    return str(conv.context.get("schedule_id") or "")


class EditAgent:
    def __init__(self, llm_factory):
        self._llm_factory = llm_factory
        self._llm: Optional[EditLLM] = None

    @property
    def llm(self) -> EditLLM:
        if self._llm is None:
            self._llm = self._llm_factory()
        return self._llm

    @staticmethod
    def is_button(action: Optional[str]) -> bool:
        return registry.kind_of(action) == "button"

    @staticmethod
    def is_tool(name: Optional[str]) -> bool:
        """LLM 이 고르는 도구인가 — 해석이 붙은 말(예시)이 부를 수 있는 것."""
        return registry.kind_of(name) == "tool"

    def chat(self, db: Session, user, conv: Conversation, text: str, today: date, *,
             deadline: Optional[float] = None, call: Optional[Tuple[str, Dict[str, Any]]] = None,
             verified: Optional[bool] = None) -> Reply:
        """deadline: 경로에 들어온 순간 + CHAT_BUDGET(time.monotonic 기준) — 없으면 지금부터.
        call: 해석이 이미 붙은 말(처음 화면 예시 — 우리 코드가 근무표에서 문장과 도구 호출을 함께 만든다)의
        (도구 이름, 인자). 주면 LLM 을 부르지 않는다. 기록·흔적에는 문장을 사용자가 한 말로 남긴다.
        verified: 그 호출이 서버가 이 근무표에서 만든 예시 목록에 그대로 있었나(`spec._is_shown_example`) — 기록에만
        남긴다(example_verified · 검토 L1). 실행은 같다(인자는 LLM 인자와 같은 신뢰 수준)."""
        turn = self._turn(db, user, conv, today, deadline)
        started = time.monotonic()
        room = turn.remaining() - MIN_SOLVE
        if room < 1.0:      # 판정 재료 로드·대기로 이미 시간을 다 썼다 — LLM 도 솔버도 부르지 않는다
            turn.record("error", {"text": text, "stage": "budget", "remaining_ms": int(turn.remaining() * 1000)})
            return self._reply(turn, Outcome(answer=OUT_OF_TIME))
        if call is not None:
            name, args, source = call[0], dict(call[1] or {}), "example"
            turn.record("message", {"text": text, "tool": name, "args": args, "source": source,
                                    "example_verified": bool(verified)}, elapsed_ms=_ms(started))
        else:
            try:
                # 언제나 도구 하나 — 글로 답하면 사실을 지어낼 수 있다(바꾸지 않는 답은 reply_only 의 정해진 문장)
                response = self.llm.chat(build_messages(conv, turn.session, text, today), list(SKILL_TOOLS),
                                         tool_choice="required", timeout=min(CALL_TIMEOUT, room))
            except Exception as exc:   # noqa: BLE001 — 공급자 오류·키 없음 등. 대화는 이어 간다.
                logger.exception("[schedule_edit] LLM 호출 실패 conv=%s", conv.id)
                turn.record("error", {"text": text, "stage": "llm", "error": f"{type(exc).__name__}: {exc}"[:500]},
                            elapsed_ms=_ms(started))
                return self._reply(turn, Outcome(answer=LLM_FAILED))
            usage = dict(getattr(self.llm, "last_usage", {}) or {})
            if not response.is_tool_call:
                # 도구를 강제했는데도 글이 왔다 — 그 글은 보여 주지 않고(분석용으로만 남긴다) 정해진 문장으로
                reply = reply_text("unclear", turn.session)
                turn.record("message", {"text": text, "reply": reply, "llm_text": (response.text or "")[:500],
                                        "usage": usage}, elapsed_ms=_ms(started))
                return self._reply(turn, Outcome(answer=reply))
            picked = response.tool_calls[0]
            turn.record("message", {"text": text, "tool": picked.name, "args": picked.args, "usage": usage,
                                    "ignored_calls": [c.name for c in response.tool_calls[1:]]},
                        elapsed_ms=_ms(started))
            name, args, source = picked.name, picked.args or {}, "llm"
        result = execute_skill(turn, name, args, source=source)
        reply = self._reply(turn, result.data)
        if result.blocked:          # 수정 도우미에 없는 도구·버튼을 골랐다 — 흔적은 그대로
            return reply
        reply.trail = TRAIL.get(result.skill_name, "keep")
        if reply.trail == "new":
            reply.request_line = current_request(turn.session) or None
        return reply

    def act(self, db: Session, user, conv: Conversation, action: str, params: Dict[str, Any],
            today: date, *, deadline: Optional[float] = None, session: Optional[EditSession] = None) -> Reply:
        """session: 이 턴에 이미 연 판정 재료(`session_for`) — 주면 다시 열거나 최신인지 묻지 않는다."""
        if not self.is_button(action):
            raise SkillError("알 수 없는 동작입니다.")
        turn = self._turn(db, user, conv, today, deadline, session)
        label = BUTTON_TEXT.get(action, action).format(rank=params.get("rank"), choice_id=params.get("choice_id"))
        reply = self._reply(turn, execute_skill(turn, action, params, source="button").data)
        reply.said = f"[버튼] {label}"
        return reply

    def session_for(self, db: Session, conv: Conversation) -> EditSession:
        """판정 재료 — 없으면 열고, 근무표가 그사이 바뀌었으면 다시 연다(요청·레버·넘긴 묶음·고칠 수 있는 첫날은 이어 간다)."""
        session = conv.state
        schedule_id = schedule_of(conv)
        if session is not None and session.schedule_id == schedule_id and not is_stale(db, session):
            return session
        same = session is not None and session.schedule_id == schedule_id
        carry = conv.restored or ({"requests": session.requests, "allow": session.allow,
                                   "avoid": session.avoid_people} if same else None)
        fresh = open_session(db, schedule_id)
        held = conv.carry.get("edit_from")
        if held and tuple(held[:2]) == (fresh.ctx.year, fresh.ctx.month):
            # 고칠 수 있는 첫날은 대화 내내 — 칸이 바뀌어 다시 열거나 적용 뒤 새 버전으로 옮겨 가도(사용자 결정 11-05).
            # 같은 대화로 다른 달 근무표로 옮겨 가면 그 달 날짜가 아니라 쓰지 않는다
            fresh.ctx.edit_from = held[2]
        if same:
            fresh.shown_notes = set(session.shown_notes)          # 같은 근무표면 참고 문구를 다시 붙이지 않는다(검토 L2)
            # 동의한 것·묻는 중인 확인도 이어 간다(칸이 바뀌어 다시 열어도 같은 대화다). 되돌릴 앞 후보는 버린다 —
            # 옛 판정 재료로 찾은 후보라 지금 근무표와 '전' 값이 다를 수 있다(검토 M2)
            fresh.confirmed, fresh.asking = set(session.confirmed), list(session.asking)
            fresh.held = (list(session.held[0]), None, list(session.held[2])) if session.held else None
        if carry:
            fresh.requests = (requests_from_json(carry["requests"]) if conv.restored else list(carry["requests"]))
            fresh.allow = (allow_from_json(carry["allow"]) if conv.restored else dict(carry["allow"]))
            fresh.avoid_people = [frozenset(p) for p in carry["avoid"]]
        if conv.restored:
            fresh.confirmed = set(conv.restored.get("confirmed") or [])
            fresh.asking = [finding_from_json(f) for f in conv.restored.get("asking") or []]
            held = conv.restored.get("held")
            if held:        # 후보는 다시 풀어야 해서 요청만 되살린다(검토 M2)
                fresh.held = (requests_from_json(held.get("requests") or []), None,
                              [frozenset(p) for p in held.get("avoid") or []])
        conv.state, conv.restored = fresh, None
        return fresh

    # ── 내부 ────────────────────────────────────────────────────────────────

    def _turn(self, db: Session, user, conv: Conversation, today: date, deadline: Optional[float],
              session: Optional[EditSession] = None) -> Turn:
        # 기한은 판정 재료를 열기 전에 정한다 — 로드 시간도 예산 안이다(검토 HIGH-2)
        deadline = deadline if deadline is not None else time.monotonic() + CHAT_BUDGET
        session = session if session is not None else self.session_for(db, conv)
        return Turn(db=db, user=user, conversation_id=conv.id, turn_no=conv.turn_no, session=session,
                    today=today, deadline=deadline, carry=conv.carry)

    @staticmethod
    def _reply(turn: Turn, outcome: Outcome) -> Reply:
        moved = {"schedule_id": turn.switched_to} if turn.switched_to else None
        return Reply(answer=outcome.answer, preview=outcome.preview, effects=list(outcome.effects),
                     context=moved, focus=outcome.focus, failed=outcome.failed)


def close_conversation(db: Session, user, conv: Conversation) -> None:
    record_event(db, schedule_of(conv), "close", {"turn_no": conv.turn_no},
                 created_by=getattr(user, "account_id", None), conversation_id=conv.id, turn_no=conv.turn_no)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
