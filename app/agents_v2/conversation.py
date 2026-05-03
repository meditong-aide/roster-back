"""Conversation state — multi-turn message history across HTTP requests.

In-memory store with TTL-based expiry. Thread-safe.
Production alternative: Redis or database-backed store.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass
class Conversation:
    """Single conversation state."""

    id: str
    messages: list[dict] = field(default_factory=list)
    pending_approval: dict | None = None
    variable_memory: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


class ConversationStore:
    """In-memory conversation store. Thread-safe with TTL eviction."""

    TTL = 3600  # 1 hour

    def __init__(self) -> None:
        self._store: dict[str, Conversation] = {}
        self._lock = Lock()

    def create(self) -> Conversation:
        conv = Conversation(id=str(uuid.uuid4()))
        with self._lock:
            self._store[conv.id] = conv
        return conv

    def get(self, conv_id: str) -> Conversation | None:
        with self._lock:
            conv = self._store.get(conv_id)
            if conv and (time.time() - conv.last_active) > self.TTL:
                del self._store[conv_id]
                return None
            if conv:
                conv.last_active = time.time()
            return conv

    def get_or_create(self, conv_id: str | None) -> Conversation:
        if conv_id:
            conv = self.get(conv_id)
            if conv:
                return conv
        return self.create()

    def save_messages(self, conv_id: str, messages: list[dict]) -> None:
        conv = self.get(conv_id)
        if conv:
            conv.messages = messages

    def set_pending_approval(
        self, conv_id: str, preview: dict | None
    ) -> None:
        conv = self.get(conv_id)
        if conv:
            conv.pending_approval = preview

    def save_variable_memory(
        self, conv_id: str, vm_data: dict[str, Any]
    ) -> None:
        conv = self.get(conv_id)
        if conv:
            conv.variable_memory = vm_data

    def cleanup_expired(self) -> int:
        """Remove expired conversations. Returns count removed."""
        now = time.time()
        with self._lock:
            expired = [
                k
                for k, v in self._store.items()
                if now - v.last_active > self.TTL
            ]
            for k in expired:
                del self._store[k]
            return len(expired)


# Singleton
conversation_store = ConversationStore()
