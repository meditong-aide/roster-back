"""UserMemoryRepo — Tier 2 user fact 저장소 (cross-session 학습된 사실).

Zep 식 temporal validity:
  - valid_to IS NULL  → 현재 유효
  - valid_to non-NULL → 만료(이력 보존)

group_id 필터 강제 — D-1/D-2 audit 정책 (cross-group read/write 차단).

US-A4 (Extractor) 와의 연결고리:
  apply_fact() 가 LLM 분류기(Extractor)에서 내려온 action/fact dict를
  받는 entry point. Extractor 가 내보내는 형식:
    {"action": "ADD" | "UPDATE" | "DELETE" | "NOOP", "fact": {...}}
  를 그대로 unpacking 해서 apply_fact(action, fact) 로 전달하면 된다.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from db.models import AgentUserMemory
from services.memory.audit_logger import write_audit

logger = logging.getLogger(__name__)


class UserMemoryRepo:
    """Tier 2 user fact — cross-session 학습된 사실. Zep 식 temporal validity.

    모든 read/write 에 group_id 격리 강제.
    """

    VALID_ACTIONS = {"ADD", "UPDATE", "DELETE", "NOOP"}
    VALID_SOURCES = {"USER_STATED", "AGENT_INFERRED", "SYSTEM_DERIVED"}

    def __init__(self, db: Session):
        self.db = db

    # ── internal helpers ──────────────────────────────────────────

    def _now(self) -> datetime:
        return datetime.utcnow()

    def _validate_action(self, action: str) -> None:
        if action not in self.VALID_ACTIONS:
            raise ValueError(
                f"action={action!r} 은 유효하지 않음. "
                f"허용값: {self.VALID_ACTIONS}"
            )

    def _validate_source(self, source: str) -> None:
        if source not in self.VALID_SOURCES:
            raise ValueError(
                f"source={source!r} 은 유효하지 않음. "
                f"허용값: {self.VALID_SOURCES}"
            )

    def _current_row(
        self,
        user_id: str,
        group_id: str,
        fact_type: str,
    ) -> AgentUserMemory | None:
        """fact_type + user_id + group_id 에서 currently-valid row 반환 (없으면 None)."""
        return (
            self.db.query(AgentUserMemory)
            .filter(
                AgentUserMemory.user_id == user_id,
                AgentUserMemory.group_id == group_id,
                AgentUserMemory.fact_type == fact_type,
                AgentUserMemory.valid_to.is_(None),
            )
            .one_or_none()
        )

    def _row_to_dict(self, row: AgentUserMemory) -> dict:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "group_id": row.group_id,
            "fact_type": row.fact_type,
            "fact_text": row.fact_text,
            "source": row.source,
            "valid_from": row.valid_from,
            "valid_to": row.valid_to,
            "confidence": row.confidence,
            "evidence_session_id": row.evidence_session_id,
            "created_at": row.created_at,
        }

    # ── public API ───────────────────────────────────────────────

    def apply_fact(self, action: str, fact: dict) -> dict:
        """action ∈ {ADD, UPDATE, DELETE, NOOP} 에 따라 fact를 저장/갱신/삭제.

        - ADD: fact_text 신규 row insert. valid_from=now, valid_to=None.
        - UPDATE: 기존 fact_type+user_id+group_id의 currently-valid row를
                  valid_to=now로 expire 후 새 row insert (이력 보존).
        - DELETE: 해당 currently-valid row의 valid_to=now (soft delete).
        - NOOP: 변경 없음. 통계/추적 전용.

        fact dict 필수 키: user_id, group_id, fact_type, fact_text, source.
        선택 키: confidence (기본 1.0), evidence_session_id (기본 None).

        반환값: {"action": action, "row": <저장된 row dict> or None}
        """
        self._validate_action(action)

        user_id: str = fact["user_id"]
        group_id: str = fact["group_id"]
        fact_type: str = fact["fact_type"]
        fact_text: str = fact["fact_text"]
        source: str = fact["source"]
        self._validate_source(source)

        confidence: float = float(fact.get("confidence", 1.0))
        evidence_session_id: str | None = fact.get("evidence_session_id")

        now = self._now()

        if action == "NOOP":
            logger.debug(
                "[user_repo] NOOP user=%s group=%s fact_type=%s",
                user_id, group_id, fact_type,
            )
            return {"action": action, "row": None}

        if action == "ADD":
            new_row = AgentUserMemory(
                user_id=user_id,
                group_id=group_id,
                fact_type=fact_type,
                fact_text=fact_text,
                source=source,
                valid_from=now,
                valid_to=None,
                confidence=confidence,
                evidence_session_id=evidence_session_id,
                created_at=now,
            )
            self.db.add(new_row)
            self.db.flush()
            logger.debug(
                "[user_repo] ADD id=%s user=%s group=%s fact_type=%s",
                new_row.id, user_id, group_id, fact_type,
            )
            write_audit(
                self.db,
                action="WRITE",
                tier="USER",
                row_id=new_row.id,
                who=user_id,
                why=f"action=ADD fact_type={fact_type}",
                agent_run_id=evidence_session_id,
            )
            return {"action": action, "row": self._row_to_dict(new_row)}

        if action == "UPDATE":
            existing = self._current_row(user_id, group_id, fact_type)
            if existing is not None:
                old_id = existing.id
                existing.valid_to = now
                self.db.flush()
                # audit: 이전 row EXPIRE
                write_audit(
                    self.db,
                    action="EXPIRE",
                    tier="USER",
                    row_id=old_id,
                    who=user_id,
                    why=f"action=UPDATE superseded fact_type={fact_type}",
                    agent_run_id=evidence_session_id,
                )
            new_row = AgentUserMemory(
                user_id=user_id,
                group_id=group_id,
                fact_type=fact_type,
                fact_text=fact_text,
                source=source,
                valid_from=now,
                valid_to=None,
                confidence=confidence,
                evidence_session_id=evidence_session_id,
                created_at=now,
            )
            self.db.add(new_row)
            self.db.flush()
            logger.debug(
                "[user_repo] UPDATE new_id=%s user=%s group=%s fact_type=%s",
                new_row.id, user_id, group_id, fact_type,
            )
            # audit: 새 row WRITE
            write_audit(
                self.db,
                action="WRITE",
                tier="USER",
                row_id=new_row.id,
                who=user_id,
                why=f"action=UPDATE fact_type={fact_type}",
                agent_run_id=evidence_session_id,
            )
            return {"action": action, "row": self._row_to_dict(new_row)}

        # action == "DELETE"
        existing = self._current_row(user_id, group_id, fact_type)
        if existing is None:
            logger.warning(
                "[user_repo] DELETE — currently-valid row 없음 user=%s group=%s fact_type=%s",
                user_id, group_id, fact_type,
            )
            return {"action": action, "row": None}
        deleted_id = existing.id
        existing.valid_to = now
        self.db.flush()
        logger.debug(
            "[user_repo] DELETE id=%s user=%s group=%s fact_type=%s",
            existing.id, user_id, group_id, fact_type,
        )
        write_audit(
            self.db,
            action="DELETE",
            tier="USER",
            row_id=deleted_id,
            who=user_id,
            why=f"action=DELETE fact_type={fact_type}",
            agent_run_id=evidence_session_id,
        )
        return {"action": action, "row": self._row_to_dict(existing)}

    def query_valid_facts(
        self,
        user_id: str,
        group_id: str,
        fact_type: str | None = None,
    ) -> list[dict]:
        """현재 유효한 facts 반환 (valid_to IS NULL OR valid_to > now).

        group_id 필터 강제 — RBAC 격리.
        fact_type 을 지정하면 해당 타입만 필터링.
        """
        now = self._now()
        q = self.db.query(AgentUserMemory).filter(
            AgentUserMemory.user_id == user_id,
            AgentUserMemory.group_id == group_id,
            # valid_to IS NULL OR valid_to > now
            (AgentUserMemory.valid_to.is_(None)) | (AgentUserMemory.valid_to > now),
        )
        if fact_type is not None:
            q = q.filter(AgentUserMemory.fact_type == fact_type)
        rows = q.order_by(AgentUserMemory.valid_from.asc()).all()
        return [self._row_to_dict(r) for r in rows]

    def expire_fact(self, fact_id: int, group_id: str, reason: str = "") -> dict:
        """단일 fact를 valid_to=now로 만료 (soft delete).

        group_id 격리 강제 — 다른 group의 fact 만료 차단.
        reason 은 로깅 전용 (컬럼 없음, 향후 audit_log 연동 용도).
        """
        row = (
            self.db.query(AgentUserMemory)
            .filter(
                AgentUserMemory.id == fact_id,
                AgentUserMemory.group_id == group_id,
            )
            .one_or_none()
        )
        if row is None:
            raise ValueError(
                f"fact_id={fact_id} 가 group_id={group_id!r} 에 존재하지 않음"
            )
        now = self._now()
        row.valid_to = now
        self.db.flush()
        logger.debug(
            "[user_repo] expire_fact id=%s group=%s reason=%r",
            fact_id, group_id, reason,
        )
        write_audit(
            self.db,
            action="EXPIRE",
            tier="USER",
            row_id=fact_id,
            who=row.user_id,
            why=reason or "expire_fact",
            agent_run_id=None,
        )
        return self._row_to_dict(row)
