"""SessionMemoryRepo — MSSQL(SOT) + Redis(hot cache) write-through.

Key layout:
  sess:{group_id}:{session_id}:msgs  → List<JSON-serialized message dict>
  sess:{group_id}:{session_id}:vm    → Hash<field_name, JSON value>

TTL: 24h sliding — 모든 write 시 EXPIRE 갱신.
모든 read/write 는 group_id 격리 강제.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from db.models import AgentConversation, AgentConversationMessage
from services.memory.redis_client import get_redis

logger = logging.getLogger(__name__)


class SessionMemoryRepo:
    """Agent 세션 메모리 write-through repo.

    - save_messages / load_messages: 메시지 리스트(대화 turn 전체)
    - save_variable_memory / load_variable_memory: skill 간 cross-turn state
    - touch_ttl: 활성 시점 갱신 (Redis EXPIRE + MSSQL last_active_at/ttl_until)

    group_id 는 key prefix + MSSQL row 양쪽에 모두 강제된다.
    """

    TTL_SECONDS = 86400  # 24h sliding

    def __init__(self, db: Session, redis_client=None):
        self.db = db
        self.redis = redis_client if redis_client is not None else get_redis()

    # ── key helpers ──────────────────────────────────────────────

    @staticmethod
    def _msgs_key(group_id: str, session_id: str) -> str:
        return f"sess:{group_id}:{session_id}:msgs"

    @staticmethod
    def _vm_key(group_id: str, session_id: str) -> str:
        return f"sess:{group_id}:{session_id}:vm"

    # ── conversation row 보장 ─────────────────────────────────────

    def _ensure_conversation(
        self, session_id: str, user_id: str, group_id: str
    ) -> AgentConversation:
        conv = (
            self.db.query(AgentConversation)
            .filter(AgentConversation.session_id == session_id)
            .one_or_none()
        )
        now = datetime.utcnow()
        if conv is None:
            conv = AgentConversation(
                session_id=session_id,
                user_id=user_id,
                group_id=group_id,
                created_at=now,
                last_active_at=now,
                ttl_until=now + timedelta(seconds=self.TTL_SECONDS),
            )
            self.db.add(conv)
            self.db.flush()
            return conv

        # group_id 격리 — 다른 group 으로 같은 session_id 재사용 금지
        if conv.group_id != group_id:
            raise ValueError(
                f"session_id={session_id} group_id mismatch "
                f"(existing={conv.group_id}, requested={group_id})"
            )
        conv.last_active_at = now
        conv.ttl_until = now + timedelta(seconds=self.TTL_SECONDS)
        self.db.flush()
        return conv

    # ── internal helpers ─────────────────────────────────────────

    def _resolve_conv(
        self, session_id: str, group_id: str | None
    ) -> tuple["AgentConversation | None", str | None]:
        """Fetch conversation row and compute effective group_id.

        Returns (conv, effective_group_id).
        If the row doesn't exist or group_id mismatches, returns (None, None).
        """
        conv = (
            self.db.query(AgentConversation)
            .filter(AgentConversation.session_id == session_id)
            .one_or_none()
        )
        if conv is None:
            return None, None
        if group_id is not None and conv.group_id != group_id:
            return None, None
        return conv, group_id or conv.group_id

    # ── messages ─────────────────────────────────────────────────

    def save_messages(
        self,
        session_id: str,
        user_id: str,
        group_id: str,
        messages: list[dict],
    ) -> None:
        """Write-through: MSSQL UPSERT 먼저 → 성공 시 Redis 갱신 + EXPIRE."""
        self._ensure_conversation(session_id, user_id, group_id)

        # full-replace 시맨틱: 기존 row 삭제 후 재삽입 (turn_idx 순서 보존)
        self.db.query(AgentConversationMessage).filter(
            AgentConversationMessage.session_id == session_id
        ).delete(synchronize_session=False)

        for idx, msg in enumerate(messages):
            tool_calls = msg.get("tool_calls")
            row = AgentConversationMessage(
                session_id=session_id,
                turn_idx=idx,
                role=str(msg.get("role", "user")),
                content=msg.get("content"),
                tool_calls_json=(
                    json.dumps(tool_calls, ensure_ascii=False)
                    if tool_calls is not None
                    else None
                ),
            )
            self.db.add(row)
        self.db.flush()
        self.db.commit()

        # MSSQL 성공 후 Redis 갱신
        key = self._msgs_key(group_id, session_id)
        pipe = self.redis.pipeline()
        pipe.delete(key)
        if messages:
            pipe.rpush(
                key,
                *[json.dumps(m, ensure_ascii=False) for m in messages],
            )
        pipe.expire(key, self.TTL_SECONDS)
        pipe.execute()

    def load_messages(
        self, session_id: str, group_id: str | None = None
    ) -> list[dict]:
        """Redis HIT → 즉시 / MISS → MSSQL fetch → Redis 채움 + EXPIRE."""
        conv, effective_group = self._resolve_conv(session_id, group_id)
        if conv is None:
            return []

        key = self._msgs_key(effective_group, session_id)

        cached = self.redis.lrange(key, 0, -1)
        if cached:
            self.redis.expire(key, self.TTL_SECONDS)
            return [json.loads(item) for item in cached]

        # MSSQL fallback
        rows = (
            self.db.query(AgentConversationMessage)
            .filter(AgentConversationMessage.session_id == session_id)
            .order_by(AgentConversationMessage.turn_idx.asc())
            .all()
        )
        messages: list[dict] = []
        for r in rows:
            m: dict[str, Any] = {"role": r.role, "content": r.content}
            if r.tool_calls_json:
                try:
                    m["tool_calls"] = json.loads(r.tool_calls_json)
                except json.JSONDecodeError:
                    logger.warning(
                        "[session_repo] tool_calls_json decode failed session=%s turn=%s",
                        session_id,
                        r.turn_idx,
                    )
            messages.append(m)

        # Redis 캐시 채우기 + EXPIRE
        if messages:
            pipe = self.redis.pipeline()
            pipe.delete(key)
            pipe.rpush(
                key, *[json.dumps(m, ensure_ascii=False) for m in messages]
            )
            pipe.expire(key, self.TTL_SECONDS)
            pipe.execute()

        return messages

    # ── variable memory ───────────────────────────────────────────

    def save_variable_memory(
        self,
        session_id: str,
        user_id: str,
        group_id: str,
        vm_dict: dict,
    ) -> None:
        """VM dict 전체를 MSSQL Conversation.vm_json + Redis Hash 에 저장."""
        conv = self._ensure_conversation(session_id, user_id, group_id)
        conv.vm_json = json.dumps(vm_dict, ensure_ascii=False)
        self.db.flush()
        self.db.commit()

        key = self._vm_key(group_id, session_id)
        pipe = self.redis.pipeline()
        pipe.delete(key)
        if vm_dict:
            mapping = {k: json.dumps(v, ensure_ascii=False) for k, v in vm_dict.items()}
            pipe.hset(key, mapping=mapping)
        pipe.expire(key, self.TTL_SECONDS)
        pipe.execute()

    def load_variable_memory(
        self, session_id: str, group_id: str | None = None
    ) -> dict:
        """Redis HIT → 즉시 / MISS → MSSQL → Redis."""
        conv, effective_group = self._resolve_conv(session_id, group_id)
        if conv is None:
            return {}

        key = self._vm_key(effective_group, session_id)

        cached = self.redis.hgetall(key)
        if cached:
            self.redis.expire(key, self.TTL_SECONDS)
            return {k: json.loads(v) for k, v in cached.items()}

        # MSSQL fallback
        if not conv.vm_json:
            return {}
        try:
            vm = json.loads(conv.vm_json)
        except json.JSONDecodeError:
            logger.warning(
                "[session_repo] vm_json decode failed session=%s", session_id
            )
            return {}

        if vm:
            mapping = {k: json.dumps(v, ensure_ascii=False) for k, v in vm.items()}
            pipe = self.redis.pipeline()
            pipe.delete(key)
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, self.TTL_SECONDS)
            pipe.execute()

        return vm

    # ── TTL refresh ──────────────────────────────────────────────

    def touch_ttl(self, session_id: str, group_id: Optional[str] = None) -> None:
        """활성 시점 갱신 — Redis EXPIRE + MSSQL last_active_at, ttl_until."""
        conv = (
            self.db.query(AgentConversation)
            .filter(AgentConversation.session_id == session_id)
            .one_or_none()
        )
        if conv is None:
            return
        if group_id is not None and conv.group_id != group_id:
            return

        now = datetime.utcnow()
        conv.last_active_at = now
        conv.ttl_until = now + timedelta(seconds=self.TTL_SECONDS)
        self.db.flush()
        self.db.commit()

        effective_group = group_id or conv.group_id
        self.redis.expire(
            self._msgs_key(effective_group, session_id), self.TTL_SECONDS
        )
        self.redis.expire(
            self._vm_key(effective_group, session_id), self.TTL_SECONDS
        )
