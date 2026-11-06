"""대화 보관 — 실행 중 상태는 서버 메모리, 다시 세우기는 에이전트의 기록에서(근무표 수정은 `schedule_edit_log`).

  · 대화는 '대상(context)'을 하나 든다 — 근무표 수정이면 {"schedule_id"}. 대상이 바뀌면 에이전트 상태를 버린다.
  · 메모리에 두는 것: 에이전트 실행 상태(state — 근무표 수정은 판정 재료)·최근 대화·대화 흔적(trail)·마지막 블록.
  · 화면은 대화 흐름을 그대로 보인다(사용자 결정 11-03 '대화 흐름 그대로') — 응답마다 지난 턴들(말·답 문장)을 싣고,
    새로 고친 뒤에는 마지막 블록(버튼 포함)까지 다시 그린다. 지난 턴은 최근 history_keep 개만.
  · 메모리에서 내렸거나 서버가 재기동된 뒤에는 에이전트가 기록으로 다시 세운다(`Agent.restore`).

유지·관리(사용자 지시 11-02 · 에이전트마다 Limits 로):
  · 마지막 활동 뒤 idle 이 지나면 메모리에서 내린다(기록이 있어 다시 이어진다).
  · 시작 뒤 max_age 가 지나면 만료, max_turns 를 넘으면 끝 — 말은 서버가 새 대화로 이어 받는다(runtime).
  · 메모리에는 최대 max_items 개, 한 사람당 max_per_user 개 — 넘으면 가장 오래 쉰 대화부터 내린다.
★ 운영 API 는 EC2 1대·uvicorn 1프로세스라 메모리 보관으로 충분하다. 여러 대로 늘리면 기록 복원으로 이어지지만
  같은 대화가 두 서버에 동시에 올라가지 않게 고정 라우팅이 필요하다.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Limits:
    idle_seconds: float = 30 * 60
    max_age_seconds: float = 8 * 3600
    max_turns: int = 40
    max_items: int = 200
    max_per_user: int = 3
    #: 메모리에 들고 있는 최근 대화 수(LLM 에 얼마나 보낼지는 에이전트가 정한다)
    history_keep: int = 8


class ConversationError(Exception):
    """대화를 쓸 수 없다 — code: forbidden · busy · not_found · closed · expired · too_long."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


#: 이 code 면 대화가 끝난 것 — 말(text)은 새 대화로 이어 받을 수 있다
ENDED = ("not_found", "closed", "expired", "too_long")


#: 버튼 턴을 최근 대화(LLM 문맥)에 남길 때의 머리 — 화면에는 떼고 보인다
BUTTON_MARK = "[버튼] "


def said_view(user_text: str) -> Dict[str, str]:
    """최근 대화의 사용자 쪽 말 → 화면에 보일 말 {kind: text|action, text}. 기록에서 다시 세운 턴도 이걸로."""
    if user_text.startswith(BUTTON_MARK):
        return {"kind": "action", "text": user_text[len(BUTTON_MARK):]}
    if user_text.startswith("[action] "):
        return {"kind": "action", "text": user_text[len("[action] "):]}
    return {"kind": "text", "text": user_text}


@dataclass
class Conversation:
    id: str
    agent: str
    owner: str
    context: Dict[str, Any]
    created_at: float
    touched_at: float
    limits: Limits = field(default_factory=Limits)
    turn_no: int = 0
    #: 최근 대화 [{"user": 원문, "answer": 답, "shown": 화면에 보일 사용자 쪽 말}] — 오래된 것부터
    history: List[Dict[str, Any]] = field(default_factory=list)
    #: 마지막 턴에 보인 블록(흔적 빼고) — 새로 고친 뒤 이어 볼 때 그대로 다시 그린다
    last_blocks: List[Dict[str, Any]] = field(default_factory=list)
    #: 최근 대화 창에서 밀려난 턴 수 — 화면의 '앞의 대화 n개'. 턴 번호와 빼서 세면 기록이 없는 턴(버튼 등)이
    #: 복원 대화에서 함께 셈해진다(검토 11-03)
    dropped: int = 0
    #: 화면에 보일 흔적 — 지금 요청 한 줄과 그 뒤 고른 것들
    request: Optional[str] = None
    steps: List[str] = field(default_factory=list)
    #: 에이전트 실행 상태(근무표 수정은 판정 재료 EditSession) — 대상이 바뀌면 버린다
    state: Any = None
    #: 기록에서 다시 세운 상태 — 에이전트가 다음 턴에 state 를 열 때 얹는다
    restored: Any = None
    #: 대상이 바뀌어도(적용 → 새 버전 · 다른 버전 열기) 이어 가는 에이전트 값 — move_to 가 지우지 않는다
    #: (근무표 수정은 사용자가 정한 고칠 수 있는 첫날 · 기록에서 다시 세울 때도 여기에 얹는다)
    carry: Dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def begin_turn(self) -> int:
        self.turn_no += 1
        return self.turn_no

    def remember(self, user_text: str, answer: str, shown: Optional[Dict[str, str]] = None) -> None:
        self.history.append({"user": user_text, "answer": answer, "shown": shown or said_view(user_text)})
        over = len(self.history) - self.limits.history_keep
        if over > 0:
            self.dropped += over
            del self.history[:over]

    def past(self) -> List[Dict[str, Any]]:
        """화면에 보일 지난 턴(마지막 턴 빼고) — [{"said": {kind, text}, "answer": 문장}]."""
        return [{"said": h.get("shown") or said_view(h["user"]), "answer": h["answer"]} for h in self.history[:-1]]

    def cut(self) -> int:
        """최근 대화 창에서 밀려나 화면에 못 보이는 앞 턴 수."""
        return self.dropped

    def move_to(self, context: Dict[str, Any]) -> None:
        """대상을 옮긴다(화면에서 다른 버전으로 갔거나 적용으로 새 버전이 생김) — 실행 상태는 새로 연다(carry 는 남긴다)."""
        self.context, self.state, self.restored = dict(context), None, None

    def to_json(self, state_json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """저장용 — 메타만. lock·last_blocks·state·restored 는 싣지 않는다(state 는 에이전트가 직렬화해 state_json 으로 준다).

        history 는 최근 history_keep 개만 — 넘치는 만큼은 dropped 에 더한다. carry 의 튜플은 JSON 이라 목록이 된다.
        """
        keep = self.limits.history_keep
        over = max(0, len(self.history) - keep)
        return {
            "id": self.id, "agent": self.agent, "owner": self.owner, "context": dict(self.context),
            "turn_no": self.turn_no, "history": [dict(h) for h in self.history[over:]], "dropped": self.dropped + over,
            "request": self.request, "steps": list(self.steps), "carry": dict(self.carry),
            "created_at": self.created_at, "touched_at": self.touched_at, "state": state_json,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any], limits: Limits) -> "Conversation":
        """저장본 → 대화(실행 상태 없음). data["state"] 는 에이전트가 따로 읽어 state 를 세운다."""
        return cls(id=data["id"], agent=data["agent"], owner=data["owner"], context=dict(data.get("context") or {}),
                   created_at=float(data["created_at"]), touched_at=float(data["touched_at"]), limits=limits,
                   turn_no=int(data.get("turn_no") or 0), history=[dict(h) for h in data.get("history") or []],
                   dropped=int(data.get("dropped") or 0), request=data.get("request"),
                   steps=list(data.get("steps") or []), carry=dict(data.get("carry") or {}))

    def info(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "conversation_id": self.id, "agent": self.agent, "context": dict(self.context),
            "turn_no": self.turn_no, "turns_left": max(0, self.limits.max_turns - self.turn_no),
            "idle_expires_in_s": int(max(0, self.touched_at + self.limits.idle_seconds - now)),
            "expires_in_s": int(max(0, self.created_at + self.limits.max_age_seconds - now)),
            "in_memory": True,
        }


Restorer = Callable[[Session, str], Conversation]


class ConversationStore:
    def __init__(self, agent: str, limits: Limits, restore: Restorer, *, prefix: str = ""):
        self.agent, self.limits, self._restore = agent, limits, restore
        self.prefix = prefix
        self._items: "OrderedDict[str, Conversation]" = OrderedDict()
        self._guard = threading.Lock()

    def start(self, owner: str, context: Dict[str, Any]) -> Conversation:
        """새 대화 — 잠근 채로 돌려준다(`checkin` 으로 푼다)."""
        now = time.time()
        conv = Conversation(id=f"{self.prefix}{uuid.uuid4()}", agent=self.agent, owner=owner, context=dict(context),
                            created_at=now, touched_at=now, limits=self.limits)
        conv.lock.acquire()
        with self._guard:
            self._sweep(now)
            self._items[conv.id] = conv
            self._limit(owner)
        return conv

    def checkout(self, db: Session, conversation_id: str, owner: str, *, check_limits: bool = True) -> Conversation:
        """대화를 잠가서 꺼낸다 — 메모리에 있으면 그것, 없으면 기록에서 다시 세운다.

        꺼내기와 잠그기를 `_guard` 안에서 한 번에 한다 — 그 틈에 메모리에서 내려가 같은 대화가 두 객체로
        동시에 도는 일을 막는다. 처리 중이면 busy(409), 남의 대화·만료·턴 초과는 ConversationError.
        check_limits=False — 닫기처럼 만료·턴 초과와 상관없이 그 대화를 찾기만 할 때.
        """
        now = time.time()
        with self._guard:
            self._sweep(now)
            conv = self._items.get(conversation_id)
            if conv is not None:
                self._take(conv, owner)
        if conv is None:
            fresh = self._restore(db, conversation_id)
            if fresh.owner != owner:      # 남의 대화는 메모리에 올리지 않는다
                raise ConversationError("forbidden", "다른 사람의 대화입니다.", 403)
            fresh.limits = self.limits
            with self._guard:
                conv = self._items.setdefault(conversation_id, fresh)
                self._take(conv, owner)
                self._limit(conv.owner)
        try:
            if check_limits:
                self._check(conv, now)
        except ConversationError:
            self.checkin(conv)
            raise
        conv.touched_at = now
        return conv

    def checkin(self, conv: Conversation) -> None:
        conv.touched_at = time.time()
        if conv.lock.locked():
            conv.lock.release()

    def close(self, conversation_id: str) -> None:
        with self._guard:
            self._items.pop(conversation_id, None)

    def active(self, owner: Optional[str] = None) -> List[Conversation]:
        with self._guard:
            self._sweep(time.time())
            return [c for c in self._items.values() if owner is None or c.owner == owner]

    # ── 내부 ────────────────────────────────────────────────────────────────

    def _take(self, conv: Conversation, owner: str) -> None:
        if conv.owner != owner:
            raise ConversationError("forbidden", "다른 사람의 대화입니다.", 403)
        if not conv.lock.acquire(blocking=False):
            raise ConversationError("busy", "앞 요청을 처리하고 있습니다. 끝나면 다시 보내 주세요.", 409)
        self._items.move_to_end(conv.id)

    def _check(self, conv: Conversation, now: float) -> None:
        if now - conv.created_at > self.limits.max_age_seconds:
            self.close(conv.id)
            raise ConversationError("expired", "대화가 오래되어 끝났습니다. 새 대화로 다시 요청해 주세요.", 410)
        if conv.turn_no >= self.limits.max_turns:
            raise ConversationError("too_long", f"한 대화는 {self.limits.max_turns}번까지 주고받을 수 있습니다. "
                                                "새 대화로 이어서 요청해 주세요.", 410)

    def _sweep(self, now: float) -> None:
        for cid in [cid for cid, c in self._items.items()
                    if now - c.touched_at > self.limits.idle_seconds and not c.lock.locked()]:
            del self._items[cid]

    def _limit(self, owner: str) -> None:
        """한도는 처리 중인 대화까지 세고, 내리는 건 쉬는 대화만(오래 쉰 것부터)."""
        mine = [cid for cid, c in self._items.items() if c.owner == owner]
        resting = [cid for cid in mine if not self._items[cid].lock.locked()]
        for cid in resting[:max(0, len(mine) - self.limits.max_per_user)]:
            del self._items[cid]
        resting = [cid for cid, c in self._items.items() if not c.lock.locked()]
        for cid in resting[:max(0, len(self._items) - self.limits.max_items)]:
            del self._items[cid]
