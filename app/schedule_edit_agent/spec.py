"""근무표 수정 도우미를 공통 틀(agent_kit)에 꽂는다 — 경로 `/api/agents/schedule-edit/…`.

  · 기본으로 꺼져 있다 — `SCHEDULE_EDIT_AGENT_ENABLED=1` 일 때만 응답(아니면 404).
  · 수간호사·그룹관리자·ADM 만(`caller_is_manager` · DB 기준) + 그 근무표 병동에 접근 가능해야 한다.
  · 대상(context) = {"schedule_id"}. 적용하면 대화가 새 버전으로 옮겨 간다(효과 switch_version 으로 화면도).
  · 서버 전체 동시 풀이는 SCHEDULE_EDIT_MAX_CONCURRENCY 개 — 기본은 코어 2개 이하면 1, 아니면 2(운영 API 가
    vCPU 2 라 둘을 같이 풀면 둘 다 느려진다).
"""

from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from agent_kit import blocks as B
from agent_kit.base import Agent, TurnCall, TurnInput, TurnResult, env_enabled, register
from agent_kit.conversation import Conversation
from agent_kit.runtime import TurnError
from db.models import Schedule
from schedule_edit_agent import view
from schedule_edit_agent.agent import EditAgent, close_conversation
from schedule_edit_agent.llm import OpenAIEditLLM
from schedule_edit_agent.skills import CHAT_BUDGET, MIN_SOLVE, UNSUPPORTED, SkillError
from schedule_edit_agent.store import AGENT_ID, LIMITS, restore
from services.group_access import assert_caller_can_access_group, caller_is_manager
from services.schedule_edit.journal import read_conversation, read_conversation_head

MANAGERS_ONLY = "근무표 수정 도우미는 수간호사·그룹관리자만 쓸 수 있습니다."
EXAMPLES = ["○○○ 14일 오프로", "15일 ○○○, ○○○ 근무 바꿔줘", "○○○ 25일 나이트 빼줘", "○○○ N 2개만 줄여줘",
            "○○○ 20~22일 오프", "남는 OFF 연차로 전환해줘"]


class ScheduleEditAgent(Agent):
    id = AGENT_ID
    title = "근무표 수정 도우미"
    budget_seconds = CHAT_BUDGET
    min_work_seconds = MIN_SOLVE
    limits = LIMITS
    moved_message = "보던 근무표가 바뀌어 이 버튼은 쓸 수 없습니다. 바꿀 내용을 다시 말씀해 주세요."

    def __init__(self, llm_factory=OpenAIEditLLM):
        self.core = EditAgent(llm_factory)
        super().__init__()

    def enabled(self) -> bool:
        return env_enabled("SCHEDULE_EDIT_AGENT_ENABLED")

    def concurrency(self) -> int:
        return int(os.getenv("SCHEDULE_EDIT_MAX_CONCURRENCY") or (1 if (os.cpu_count() or 2) <= 2 else 2))

    def authorize(self, db: Session, user: Any, context: Dict[str, Any]) -> Dict[str, Any]:
        schedule = authorize_schedule(db, user, str(context.get("schedule_id") or ""))
        return {"schedule_id": schedule.schedule_id}

    def intro(self, db: Session, user: Any, context: Dict[str, Any]) -> Dict[str, Any]:
        schedule = db.query(Schedule).filter(Schedule.schedule_id == context["schedule_id"]).first()
        version = getattr(schedule, "version", None)
        return {
            "title": self.title,
            "subtitle": (f"VER{version} 기준 · " if version is not None else "") + "규칙을 지키며 가장 적게 바꾸는 안을 찾습니다",
            "blocks": [
                B.notice("하드락과 병동 설정을 모두 지키면서 가장 적게 바꾸는 안을 3개까지 찾아 표에 미리 보여 드립니다. "
                         "고른 안은 새 버전으로 저장되고, 이 버전은 그대로 남습니다.",
                         tone="normal", title="바꿀 내용을 말로 적어 주세요"),
                B.examples("이렇게 말할 수 있어요", EXAMPLES),
                B.notice(UNSUPPORTED, tone="muted", title="아직 못 하는 것"),
            ],
            "composer": {"label": "바꿀 내용", "placeholder": "예: 15일 나이트를 데이로 바꿔줘",
                         "max_length": 1000, "hint": "적용을 누르기 전까지 근무표는 바뀌지 않습니다",
                         "working": "규칙을 지키는 안을 찾고 있습니다…"},
        }

    def check_input(self, inp: TurnInput) -> None:
        if inp.kind == "action" and not self.core.is_button(inp.action):
            raise TurnError("bad_action", "알 수 없는 동작입니다.", 400)

    def handle(self, call: TurnCall) -> TurnResult:
        conv, inp = call.conv, call.input
        step = None
        if inp.kind == "text":
            reply = self.core.chat(call.db, call.user, conv, inp.text or "", call.today, deadline=call.deadline)
        else:
            params = dict(inp.params)
            if inp.action == "apply_edit":
                params.setdefault("request_text", conv.request or "")   # 변경 이력의 '요청' 칸 — 화면이 보낼 필요 없다
            # 흔적 줄은 누르기 전 상태로 만든다 — 연 판정 재료를 act 에 넘겨 최신 확인을 한 번만(검토 L3)
            session = self.core.session_for(call.db, conv)
            step = view.step_label(inp.action or "", params, session)
            try:
                reply = self.core.act(call.db, call.user, conv, inp.action or "", params, call.today,
                                      deadline=call.deadline, session=session)
            except SkillError as exc:
                raise TurnError("bad_action", str(exc), 400) from exc
        return TurnResult(blocks=view.blocks_for(reply, conv.state), effects=reply.effects, answer=reply.answer,
                          said=reply.said, step=None if reply.failed else step, context=reply.context,
                          trail=reply.trail)

    def restore(self, db: Session, conversation_id: str) -> Conversation:
        return restore(db, conversation_id)

    def transcript(self, db: Session, user: Any, conversation_id: str) -> Dict[str, Any]:
        """대화 한 건의 모든 턴(기록 그대로). 본인 대화(지금도 관리자일 때), 또는 그 병동에 접근 가능한 ADM."""
        if not caller_is_manager(db, user):        # 권한이 없어진 사람은 자기 대화도 못 본다(검토 L2)
            raise HTTPException(status_code=403, detail=MANAGERS_ONLY)
        head = read_conversation_head(db, conversation_id)
        rows = read_conversation(db, conversation_id)
        if head is None or not rows:
            raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다.")
        if str(head.created_by or "") != user.account_id:
            if not user.is_master_admin:
                raise HTTPException(status_code=403, detail="다른 사람의 대화입니다.")
            authorize_schedule(db, user, head.schedule_id)
        return {"turns": [{"log_id": r.log_id, "turn_no": r.turn_no, "event": r.event, "schedule_id": r.schedule_id,
                           "new_schedule_id": r.new_schedule_id, "elapsed_ms": r.elapsed_ms,
                           "created_at": r.created_at.isoformat() if r.created_at else None, "payload": r.payload}
                          for r in rows]}

    def on_close(self, db: Session, user: Any, conv: Conversation) -> None:
        close_conversation(db, user, conv)


def authorize_schedule(db: Session, user: Any, schedule_id: str) -> Schedule:
    if not caller_is_manager(db, user):
        raise HTTPException(status_code=403, detail=MANAGERS_ONLY)
    schedule = db.query(Schedule).filter(Schedule.schedule_id == schedule_id).first() if schedule_id else None
    if schedule is None or getattr(schedule, "dropped", False):
        raise HTTPException(status_code=404, detail="근무표를 찾을 수 없습니다.")
    if not schedule.group_id:
        # 병동 없는 근무표는 접근 검사가 통과돼 버린다(group_id 가 비면 통과) — 여기서는 막는다(검토 L4)
        raise HTTPException(status_code=403, detail="병동이 없는 근무표는 수정 도우미로 다룰 수 없습니다.")
    assert_caller_can_access_group(db, user, schedule.group_id)
    return schedule


AGENT = register(ScheduleEditAgent())
