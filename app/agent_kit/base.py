"""에이전트가 구현할 것 — 공통 틀(`runtime`·`router`)이 대화·잠금·시간·경로를 맡고, 에이전트는 이것만 채운다.

  · authorize  : 화면이 보낸 대상(context)을 검사·정규화한다. 안 되면 HTTPException(403·404).
  · intro      : 처음 화면 — 제목·부제·블록(예시·못 하는 것)·입력칸 설정
                 (composer: label·placeholder·max_length·hint·working = 처리 중 문구).
  · handle     : 한 턴 — 말(text)이나 버튼(action)을 받아 블록·효과를 낸다. 턴 번호·최근 대화·흔적·대상 이동은
                 runtime 이 한다(handle 은 TurnResult 에 담기만).
  · restore    : 메모리에서 내려간 대화를 기록에서 다시 세운다. 없으면 ConversationError(not_found·closed).
  · transcript : 대화 한 건의 기록(권한 검사 포함).
새 에이전트 = Agent 를 상속해 위를 채우고 `register()` — 경로·화면은 그대로다.
"""

from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from agent_kit.blocks import Block
from agent_kit.conversation import Conversation, ConversationStore, Limits

KST = timezone(timedelta(hours=9))


@dataclass
class TurnInput:
    kind: str                      # text · action (에이전트가 accepts 로 받을 종류를 정한다)
    text: Optional[str] = None
    action: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnCall:
    db: Session
    user: Any
    conv: Conversation
    input: TurnInput
    today: date
    deadline: float                # time.monotonic() 기준 — 경로에 들어온 순간 + budget_seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


@dataclass
class TurnResult:
    blocks: List[Block]
    effects: List[Dict[str, Any]] = field(default_factory=list)
    #: 최근 대화(LLM 문맥)에 남길 답 — 화면 문장과 같을 필요는 없다
    answer: str = ""
    #: 최근 대화에 남길 사용자 쪽 말 — 버튼은 '[버튼] 1번 안 적용' 처럼 에이전트가 정한다(없으면 입력 원문)
    said: Optional[str] = None
    #: 버튼이면 흔적에 붙일 한 줄('메우는 방식: 같은 팀')
    step: Optional[str] = None
    #: 대화 대상이 바뀌면(적용 → 새 버전) 새 대상
    context: Optional[Dict[str, Any]] = None


class Agent(ABC):
    #: 경로의 {agent_id}
    id: str = ""
    title: str = ""
    accepts: Tuple[str, ...] = ("text", "action")
    #: 한 턴 응답 시간 상한(초) — CloudFront 30초 안. 경로에 들어온 순간부터 센다
    budget_seconds: float = 25.0
    #: 남은 시간이 이보다 적으면 일을 시작하지 않는다(서버 슬롯 대기도 여기까지만)
    min_work_seconds: float = 3.0
    slot_wait_seconds: float = 10.0
    limits: Limits = Limits()
    #: 화면이 다른 대상으로 옮긴 뒤 앞 대상의 버튼을 눌렀을 때(409 context_changed)
    moved_message: str = "보던 대상이 바뀌어 이 버튼은 쓸 수 없습니다. 다시 요청해 주세요."

    def __init__(self) -> None:
        self.store = ConversationStore(self.id, self.limits, self.restore)
        self.slots = threading.BoundedSemaphore(max(1, self.concurrency()))

    def concurrency(self) -> int:
        """서버 전체에서 이 에이전트가 동시에 돌릴 턴 수(무거운 계산이 있으면 코어에 맞춘다)."""
        return 2

    def today(self) -> date:
        return datetime.now(KST).date()

    def check_input(self, inp: TurnInput) -> None:
        """턴 번호를 올리기 전에 거를 입력(없는 버튼 이름 등) — runtime.TurnError 를 올린다."""

    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    def authorize(self, db: Session, user: Any, context: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    def intro(self, db: Session, user: Any, context: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    def handle(self, call: TurnCall) -> TurnResult: ...

    @abstractmethod
    def restore(self, db: Session, conversation_id: str) -> Conversation: ...

    @abstractmethod
    def transcript(self, db: Session, user: Any, conversation_id: str) -> Dict[str, Any]: ...

    def on_close(self, db: Session, user: Any, conv: Conversation) -> None:
        """닫을 때 기록을 남길 에이전트가 채운다."""


def env_enabled(name: str) -> bool:
    return os.getenv(name) == "1"


AGENTS: Dict[str, Agent] = {}


def register(agent: Agent) -> Agent:
    AGENTS[agent.id] = agent
    return agent


def get_agent(agent_id: str) -> Optional[Agent]:
    return AGENTS.get(agent_id)
