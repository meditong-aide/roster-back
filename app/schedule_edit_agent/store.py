"""대화 보관 — 실행 중 상태는 서버 메모리, 모든 턴은 `schedule_edit_log`(사용자 결정 11-02).

  · 메모리에 두는 것: 판정 재료(EditSession — 수 MB, 근무표·사본에서 언제든 다시 만든다)와 최근 대화.
  · 기록에서 다시 세우는 것: 대상 근무표·턴 번호·지금 요청·허용 레버·넘긴 묶음·최근 대화 — 메모리에서
    내렸거나 서버가 재기동된 뒤에도 대화가 이어진다. 후보 자체는 다시 찾는다(솔버가 다시 낸다).

유지·관리(사용자 지시 11-02 · 값은 환경변수로):
  · 마지막 활동 뒤 IDLE 분이 지나면 메모리에서 내린다(기록이 있어 다시 이어진다).
  · 대화 시작 뒤 MAX_HOURS 가 지나면 만료 — 새 대화로 시작한다.
  · 한 대화는 MAX_TURNS 턴까지 — 길어지면 새 대화(지금 요청은 기록에 남는다).
  · 메모리에는 최대 MAX_ITEMS 개, 한 사람당 MAX_PER_USER 개 — 넘으면 가장 오래 쉰 대화부터 내린다.
★ 운영 API 는 EC2 1대·uvicorn 1프로세스라 메모리 보관으로 충분하다. 여러 대로 늘리면 기록 복원으로 이어지지만
  같은 대화가 두 서버에 동시에 올라가지 않게 고정 라우팅이 필요하다.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from services.schedule_edit.journal import read_conversation, read_conversation_head
from services.schedule_edit.session import EditSession


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


IDLE_SECONDS = _env_float("SCHEDULE_EDIT_IDLE_MIN", 30) * 60
MAX_AGE_SECONDS = _env_float("SCHEDULE_EDIT_MAX_HOURS", 8) * 3600
MAX_TURNS = int(_env_float("SCHEDULE_EDIT_MAX_TURNS", 40))
MAX_ITEMS = 200
MAX_PER_USER = 3
#: 메모리에 들고 있는 최근 대화 수(LLM 에는 그중 일부만 — prompt.HISTORY_TURNS)
HISTORY_KEEP = 8


class ConversationError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class Conversation:
    id: str
    owner: str
    schedule_id: str
    created_at: float
    touched_at: float
    turn_no: int = 0
    #: 최근 대화 [{"user": 원문, "answer": 답}] — 오래된 것부터
    history: List[Dict[str, str]] = field(default_factory=list)
    session: Optional[EditSession] = None
    #: 기록에서 다시 세운 상태 — 판정 재료를 열 때 세션에 얹는다 {"requests", "allow", "avoid"}
    restored: Optional[Dict[str, Any]] = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def remember(self, user_text: str, answer: str) -> None:
        self.history.append({"user": user_text, "answer": answer})
        del self.history[:-HISTORY_KEEP]

    def info(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "conversation_id": self.id, "schedule_id": self.schedule_id, "turn_no": self.turn_no,
            "turns_left": max(0, MAX_TURNS - self.turn_no),
            "idle_expires_in_s": int(max(0, self.touched_at + IDLE_SECONDS - now)),
            "expires_in_s": int(max(0, self.created_at + MAX_AGE_SECONDS - now)),
            "in_memory": True,
        }


class ConversationStore:
    def __init__(self):
        self._items: "OrderedDict[str, Conversation]" = OrderedDict()
        self._guard = threading.Lock()

    def start(self, owner: str, schedule_id: str) -> Conversation:
        """새 대화 — 잠근 채로 돌려준다(`checkin` 으로 푼다)."""
        now = time.time()
        conv = Conversation(id=str(uuid.uuid4()), owner=owner, schedule_id=schedule_id, created_at=now,
                            touched_at=now)
        conv.lock.acquire()
        with self._guard:
            self._sweep(now)
            self._items[conv.id] = conv
            self._limit(owner)
        return conv

    def checkout(self, db: Session, conversation_id: str, owner: str, *, check_limits: bool = True) -> Conversation:
        """대화를 잠가서 꺼낸다 — 메모리에 있으면 그것, 없으면 기록에서 다시 세운다.

        꺼내기와 잠그기를 `_guard` 안에서 한 번에 한다 — 그 틈에 메모리에서 내려가 같은 대화가 두 객체로
        동시에 도는 일을 막는다(검토 M2). 처리 중이면 busy(409), 남의 대화·만료·턴 초과는 ConversationError.
        check_limits=False — 닫기처럼 만료·턴 초과와 상관없이 그 대화를 찾기만 할 때.
        """
        now = time.time()
        with self._guard:
            self._sweep(now)
            conv = self._items.get(conversation_id)
            if conv is not None:
                self._take(conv, owner)
        if conv is None:
            fresh = restore(db, conversation_id)
            if fresh.owner != owner:      # 남의 대화는 메모리에 올리지 않는다
                raise ConversationError("forbidden", "다른 사람의 대화입니다.", 403)
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
        if now - conv.created_at > MAX_AGE_SECONDS:
            self.close(conv.id)
            raise ConversationError("expired", "대화가 오래되어 끝났습니다. 새 대화로 다시 요청해 주세요.", 410)
        if conv.turn_no >= MAX_TURNS:
            raise ConversationError("too_long", f"한 대화는 {MAX_TURNS}번까지 주고받을 수 있습니다. "
                                                "새 대화로 이어서 요청해 주세요.", 410)

    def _sweep(self, now: float) -> None:
        for cid in [cid for cid, c in self._items.items()
                    if now - c.touched_at > IDLE_SECONDS and not c.lock.locked()]:
            del self._items[cid]

    def _limit(self, owner: str) -> None:
        """한도는 처리 중인 대화까지 세고, 내리는 건 쉬는 대화만(오래 쉰 것부터)."""
        mine = [cid for cid, c in self._items.items() if c.owner == owner]
        resting = [cid for cid in mine if not self._items[cid].lock.locked()]
        for cid in resting[:max(0, len(mine) - MAX_PER_USER)]:
            del self._items[cid]
        resting = [cid for cid, c in self._items.items() if not c.lock.locked()]
        for cid in resting[:max(0, len(self._items) - MAX_ITEMS)]:
            del self._items[cid]


def restore(db: Session, conversation_id: str) -> Conversation:
    """기록에서 대화를 다시 세운다. 마지막 제안(propose) 기록에 그때의 요청·허용 레버·넘긴 묶음이 들어 있다.

    주인·시작 시각은 대화의 첫 기록에서(긴 대화도 최근 기록만 읽으니 따로 읽는다 · 검토 L1), 대상 버전은
    마지막 기록에서 — 화면에서 다른 버전으로 옮겨 간 턴은 그 뒤 기록의 schedule_id 에 남는다(검토 M5).
    버전이 바뀐 지점에서는 앞 버전의 요청·레버·넘긴 묶음을 버린다.
    """
    rows = read_conversation(db, conversation_id)
    head = read_conversation_head(db, conversation_id)
    if not rows or head is None:
        raise ConversationError("not_found", "대화를 찾을 수 없습니다. 새 대화로 시작해 주세요.", 404)
    if any(row.event == "close" for row in rows):
        raise ConversationError("closed", "닫은 대화입니다. 새 대화로 시작해 주세요.", 410)
    conv = Conversation(id=conversation_id, owner=str(head.created_by or ""), schedule_id=rows[0].schedule_id,
                        created_at=head.created_at.timestamp(), touched_at=time.time())
    empty = {"requests": [], "allow": [], "avoid": []}
    state: Dict[str, Any] = dict(empty)
    said: Optional[str] = None
    for row in rows:
        if row.schedule_id != conv.schedule_id:
            conv.schedule_id, state = row.schedule_id, dict(empty)
        conv.turn_no = max(conv.turn_no, int(row.turn_no or 0))
        payload = row.payload or {}
        if row.event == "message":
            said = payload.get("text")
            if payload.get("reply") is not None:          # 도구 없이 끝난 턴(정해진 문장)
                conv.remember(said or "", payload["reply"])
                said = None
        elif row.event in ("propose", "clarify"):
            if row.event == "propose":
                state = {k: payload.get(k) or [] for k in ("requests", "allow", "avoid")}
            if said is not None:
                conv.remember(said, payload.get("answer", ""))
                said = None
        elif row.event == "apply" and row.new_schedule_id:
            conv.schedule_id, state = row.new_schedule_id, dict(empty)   # 적용 뒤에는 새 버전에서 이어 간다
    conv.restored = state
    return conv
