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
from typing import Any, Optional

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from db.models import AgentConversation, AgentConversationMessage
from services.memory.redis_client import get_redis

logger = logging.getLogger(__name__)


def _is_missing_table_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        p in msg
        for p in (
            "invalid object name",  # MSSQL
            "does not exist",  # PostgreSQL
            "no such table",  # SQLite
        )
    )


class SessionMemoryRepo:
    """Agent 세션 메모리 write-through repo.

    - save_messages / load_messages: 메시지 리스트(대화 turn 전체)
    - save_variable_memory / load_variable_memory: skill 간 cross-turn state
    - touch_ttl: 활성 시점 갱신 (Redis EXPIRE + MSSQL last_active_at/ttl_until)

    group_id 는 key prefix + MSSQL row 양쪽에 모두 강제된다.

    Graceful degrade: agent_conversation 테이블이 운영 DB 에 없으면 SOT 비활성화하고
    Redis-only 모드로 전환 — 단일 세션 내 multi-turn 은 유지되지만 서버 재시작 후
    히스토리는 휘발된다. 마이그레이션 미적용 운영 환경 보호용.
    """

    TTL_SECONDS = 86400  # 24h sliding
    _sot_disabled = False  # process-wide flag (once disabled, stays disabled)

    def __init__(self, db: Session, redis_client=None):
        self.db = db
        self.redis = redis_client if redis_client is not None else get_redis()

    # ── SOT degrade ────────────────────────────────────────────

    @classmethod
    def _disable_sot(cls, context: str) -> None:
        if not cls._sot_disabled:
            cls._sot_disabled = True
            logger.warning(
                "[session_repo] agent_conversation 테이블이 운영 DB 에 없습니다 — "
                "migrations/2026_05_19_add_agent_memory_tables.sql 적용 필요. "
                "세션 메모리 SOT 비활성화 (Redis-only 로 fallback). context=%s",
                context,
            )

    def _safe_rollback(self) -> None:
        try:
            self.db.rollback()
        except Exception:  # noqa: BLE001
            pass

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
    ) -> Optional[AgentConversation]:
        """Conversation row 확보. SOT 비활성 시 None 반환 (Redis-only 모드)."""
        if SessionMemoryRepo._sot_disabled:
            return None
        try:
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
        except (ProgrammingError, OperationalError) as exc:
            if _is_missing_table_error(exc):
                self._safe_rollback()
                SessionMemoryRepo._disable_sot("_ensure_conversation")
                return None
            raise

    # ── internal helpers ─────────────────────────────────────────

    def _resolve_conv(
        self, session_id: str, group_id: str | None
    ) -> tuple["AgentConversation | None", str | None]:
        """Fetch conversation row and compute effective group_id.

        Returns (conv, effective_group_id).
        SOT 비활성 또는 row 없음 / group_id mismatch 시 (None, fallback_gid).
        Redis-only 모드에서도 caller 가 key prefix 를 만들 수 있도록 group_id 전달.
        """
        if SessionMemoryRepo._sot_disabled:
            return None, group_id
        try:
            conv = (
                self.db.query(AgentConversation)
                .filter(AgentConversation.session_id == session_id)
                .one_or_none()
            )
            if conv is None:
                return None, group_id
            if group_id is not None and conv.group_id != group_id:
                return None, None
            return conv, group_id or conv.group_id
        except (ProgrammingError, OperationalError) as exc:
            if _is_missing_table_error(exc):
                self._safe_rollback()
                SessionMemoryRepo._disable_sot("_resolve_conv")
                return None, group_id
            raise

    # ── messages ─────────────────────────────────────────────────

    def save_messages(
        self,
        session_id: str,
        user_id: str,
        group_id: str,
        messages: list[dict],
    ) -> None:
        """Write-through: MSSQL UPSERT 먼저 → 성공 시 Redis 갱신 + EXPIRE.

        SOT 비활성 시 MSSQL 부분을 skip 하고 Redis 만 갱신.
        """
        conv = self._ensure_conversation(session_id, user_id, group_id)

        if conv is not None:
            try:
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
            except (ProgrammingError, OperationalError) as exc:
                if _is_missing_table_error(exc):
                    self._safe_rollback()
                    SessionMemoryRepo._disable_sot("save_messages")
                else:
                    raise

        # MSSQL 성공/skip 후 Redis 갱신 (SOT 비활성 시에도 hot cache 는 유지)
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
        """Redis HIT → 즉시 / MISS → MSSQL fetch → Redis 채움 + EXPIRE.

        SOT 비활성 시 Redis-only — cache miss 면 빈 리스트.
        """
        conv, effective_group = self._resolve_conv(session_id, group_id)
        if effective_group is None:
            # group_id mismatch (격리 위반) — 빈 리스트
            return []

        key = self._msgs_key(effective_group, session_id)

        cached = self.redis.lrange(key, 0, -1)
        if cached:
            self.redis.expire(key, self.TTL_SECONDS)
            return [json.loads(item) for item in cached]

        if conv is None:
            # SOT 비활성 또는 row 없음 — MSSQL fallback 스킵
            return []

        # MSSQL fallback
        try:
            rows = (
                self.db.query(AgentConversationMessage)
                .filter(AgentConversationMessage.session_id == session_id)
                .order_by(AgentConversationMessage.turn_idx.asc())
                .all()
            )
        except (ProgrammingError, OperationalError) as exc:
            if _is_missing_table_error(exc):
                self._safe_rollback()
                SessionMemoryRepo._disable_sot("load_messages")
                return []
            raise
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
        """VM dict 전체를 MSSQL Conversation.vm_json + Redis Hash 에 저장.

        SOT 비활성 시 Redis Hash 만 갱신.
        """
        conv = self._ensure_conversation(session_id, user_id, group_id)
        if conv is not None:
            try:
                conv.vm_json = json.dumps(vm_dict, ensure_ascii=False)
                self.db.flush()
                self.db.commit()
            except (ProgrammingError, OperationalError) as exc:
                if _is_missing_table_error(exc):
                    self._safe_rollback()
                    SessionMemoryRepo._disable_sot("save_variable_memory")
                else:
                    raise

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
        """Redis HIT → 즉시 / MISS → MSSQL → Redis.

        SOT 비활성 시 Redis-only — cache miss 면 빈 dict.
        """
        conv, effective_group = self._resolve_conv(session_id, group_id)
        if effective_group is None:
            return {}

        key = self._vm_key(effective_group, session_id)

        cached = self.redis.hgetall(key)
        if cached:
            self.redis.expire(key, self.TTL_SECONDS)
            return {k: json.loads(v) for k, v in cached.items()}

        if conv is None or not conv.vm_json:
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
        """활성 시점 갱신 — Redis EXPIRE + MSSQL last_active_at, ttl_until.

        SOT 비활성 시 Redis EXPIRE 만 갱신.
        """
        effective_group: Optional[str] = group_id

        if not SessionMemoryRepo._sot_disabled:
            try:
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
            except (ProgrammingError, OperationalError) as exc:
                if _is_missing_table_error(exc):
                    self._safe_rollback()
                    SessionMemoryRepo._disable_sot("touch_ttl")
                else:
                    raise

        if effective_group is None:
            return
        self.redis.expire(
            self._msgs_key(effective_group, session_id), self.TTL_SECONDS
        )
        self.redis.expire(
            self._vm_key(effective_group, session_id), self.TTL_SECONDS
        )
