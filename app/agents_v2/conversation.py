"""Conversation state — multi-turn message history backed by SessionMemoryRepo.

US-A2: in-memory dict + threading.Lock + 1h TTL store 를 제거하고
SessionMemoryRepo (MSSQL SOT + Redis hot cache, 24h sliding TTL,
group_id 격리) 위임으로 전환했다.

Conversation dataclass 의 외부 인터페이스 (`.id`, `.messages`,
`.variable_memory`, `.pending_approval`) 는 그대로 유지된다 — 호출자 코드 변경 X.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from services.memory.session_repo import SessionMemoryRepo, _is_missing_table_error

# Variable-memory 안에 pending_approval 을 보관할 때 사용하는 reserved key.
# AgentConversation 스키마에 별도 컬럼 없이 vm_json 에 함께 직렬화한다.
_PENDING_APPROVAL_VM_KEY = "__pending_approval__"


@dataclass
class Conversation:
    """Single conversation state. (interface 호환 — 호출자 코드 변경 X)"""

    id: str
    messages: list[dict] = field(default_factory=list)
    pending_approval: dict | None = None
    variable_memory: dict[str, Any] = field(default_factory=dict)


def _split_pending(vm: dict[str, Any]) -> tuple[dict[str, Any], dict | None]:
    """vm dict 에서 pending_approval 을 분리해 반환."""
    if not vm:
        return {}, None
    pending = vm.get(_PENDING_APPROVAL_VM_KEY)
    if pending is None:
        return dict(vm), None
    clean = {k: v for k, v in vm.items() if k != _PENDING_APPROVAL_VM_KEY}
    return clean, pending


class ConversationStore:
    """Conversation state store — SessionMemoryRepo 위임.

    인터페이스:
      - create(db, user_id, group_id)                              → Conversation
      - get(db, conv_id, user_id, group_id)                        → Conversation | None
      - get_or_create(db, conv_id, user_id, group_id)              → Conversation
      - save_messages(db, conv_id, user_id, group_id, messages)
      - save_variable_memory(db, conv_id, user_id, group_id, vm)
      - set_pending_approval(db, conv_id, user_id, group_id, preview)

    모든 메서드가 db Session 을 받아야 한다 — 기존 in-memory 싱글톤 dict 폐기.
    """

    def __init__(self) -> None:
        # 상태 보관 없음 (Redis + MSSQL 이 SOT).
        # 싱글톤 인스턴스만 유지되며 메서드는 사실상 thin facade.
        pass

    # ── helpers ────────────────────────────────────────────────

    @staticmethod
    def _repo(db: Session) -> SessionMemoryRepo:
        return SessionMemoryRepo(db=db)

    @staticmethod
    def _resolve_user_id(user_id: str | None) -> str:
        # SessionMemoryRepo 는 user_id 가 NOT NULL — 미지정 시 "anonymous"
        return user_id or "anonymous"

    @staticmethod
    def _resolve_group_id(group_id: str | None) -> str:
        # 격리 키 누락 방지 — 명시되지 않으면 "default"
        return group_id or "default"

    # ── CRUD ───────────────────────────────────────────────────

    def create(
        self,
        db: Session,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> Conversation:
        """신규 세션 생성 — 빈 messages 로 conversation row 확보."""
        conv_id = str(uuid.uuid4())
        uid = self._resolve_user_id(user_id)
        gid = self._resolve_group_id(group_id)
        # _ensure_conversation 호출을 위해 빈 save 한 번 (write-through).
        self._repo(db).save_messages(conv_id, uid, gid, [])
        return Conversation(id=conv_id)

    def get(
        self,
        db: Session,
        conv_id: str,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> Conversation | None:
        """기존 세션 로드 — 없으면 None, group_id mismatch 면 None (격리).

        SOT 비활성 (마이그레이션 미적용) 환경에서는 Redis cache hit 만으로 판정한다.
        """
        gid_filter = group_id  # None 이면 격리 검사 skip
        repo = self._repo(db)
        messages = repo.load_messages(conv_id, group_id=gid_filter)
        vm_full = repo.load_variable_memory(conv_id, group_id=gid_filter)

        # AgentConversation row 자체가 없으면 messages == [] AND vm_full == {}
        # 이 경우 세션 존재 여부 확인 — MSSQL 직접 조회 (SOT 활성 시에만)
        if not messages and not vm_full:
            if SessionMemoryRepo._sot_disabled:
                # Redis cache 가 비었고 SOT 도 없으면 존재 안 함으로 간주.
                return None
            from db.models import AgentConversation
            try:
                conv_row = (
                    db.query(AgentConversation)
                    .filter(AgentConversation.session_id == conv_id)
                    .one_or_none()
                )
            except (ProgrammingError, OperationalError) as exc:
                if _is_missing_table_error(exc):
                    try:
                        db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    SessionMemoryRepo._disable_sot("ConversationStore.get")
                    return None
                raise
            if conv_row is None:
                return None
            if group_id is not None and conv_row.group_id != group_id:
                return None

        vm_clean, pending = _split_pending(vm_full)
        # TTL 갱신 — 활성 세션 표시
        repo.touch_ttl(conv_id, group_id=gid_filter)
        return Conversation(
            id=conv_id,
            messages=messages,
            pending_approval=pending,
            variable_memory=vm_clean,
        )

    def get_or_create(
        self,
        db: Session,
        conv_id: str | None,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> Conversation:
        if conv_id:
            existing = self.get(db, conv_id, user_id=user_id, group_id=group_id)
            if existing is not None:
                return existing
        return self.create(db, user_id=user_id, group_id=group_id)

    def save_messages(
        self,
        db: Session,
        conv_id: str,
        messages: list[dict],
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        uid = self._resolve_user_id(user_id)
        gid = self._resolve_group_id(group_id)
        self._repo(db).save_messages(conv_id, uid, gid, messages)

    def save_variable_memory(
        self,
        db: Session,
        conv_id: str,
        vm_data: dict[str, Any],
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        """variable_memory 저장. 기존 pending_approval 은 그대로 유지."""
        uid = self._resolve_user_id(user_id)
        gid = self._resolve_group_id(group_id)
        repo = self._repo(db)
        # 기존 vm 의 pending_approval 보존
        prev = repo.load_variable_memory(conv_id, group_id=gid)
        merged: dict[str, Any] = dict(vm_data)
        prev_pending = prev.get(_PENDING_APPROVAL_VM_KEY)
        if prev_pending is not None:
            merged[_PENDING_APPROVAL_VM_KEY] = prev_pending
        repo.save_variable_memory(conv_id, uid, gid, merged)

    def set_pending_approval(
        self,
        db: Session,
        conv_id: str,
        preview: dict | None,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        uid = self._resolve_user_id(user_id)
        gid = self._resolve_group_id(group_id)
        repo = self._repo(db)
        prev = repo.load_variable_memory(conv_id, group_id=gid)
        merged = {k: v for k, v in prev.items() if k != _PENDING_APPROVAL_VM_KEY}
        if preview is not None:
            merged[_PENDING_APPROVAL_VM_KEY] = preview
        repo.save_variable_memory(conv_id, uid, gid, merged)


# Singleton — 상태 없음, 인터페이스 라우터로만 동작.
conversation_store = ConversationStore()
