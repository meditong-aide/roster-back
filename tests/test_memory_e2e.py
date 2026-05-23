"""US-A5: 전체 메모리 시스템 E2E 검증 — session/fact/inject/audit 4단계 흐름.

시나리오:
  1. 세션 시작: ConversationStore.get_or_create (새 user_id+group_id)
  2. 사용자 발화 + consolidate: SchedulingAgent.run → consolidate_after_turn
     → AgentUserMemory fact 저장 확인
  3. 새 세션에서 inject: 다른 session_id + 동일 user_id+group_id → system prompt fact 포함
  4. audit 추적: AgentMemoryAudit 에 모든 변경이 기록됨 확인
"""

from __future__ import annotations

import uuid

import pytest

from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.llm_client import LLMResponse, ToolCall
from agents_v2.schemas.session_context import SessionContext
from db.models import AgentMemoryAudit, AgentUserMemory
from services.memory.extractor import MemoryExtractor
from services.memory.user_repo import UserMemoryRepo


# ── LLM doubles ───────────────────────────────────────────────


class _TextOnlyLLM:
    """메인 turn LLM — 항상 짧은 text 응답. 마지막 system prompt 캡쳐."""

    def __init__(self):
        self.last_system_prompt: str = ""

    def chat(self, messages, tools, *, tool_choice: str = "auto"):
        for m in messages:
            if m.get("role") == "system":
                self.last_system_prompt = m.get("content") or ""
                break
        return LLMResponse(type="text", text="ok")


class _ExtractorLLM:
    """Extractor LLM — scripted facts 반환."""

    def __init__(self, facts: list[dict]):
        self.facts = facts
        self.call_count = 0

    def chat(self, messages, tools, *, tool_choice: str = "auto"):
        self.call_count += 1
        tc = ToolCall(
            name="emit_fact_decisions",
            args={"facts": self.facts},
            call_id=f"call_{uuid.uuid4().hex[:8]}",
        )
        return LLMResponse(type="tool_call", tool_calls=[tc])


# ── helpers ───────────────────────────────────────────────────


def _ctx(
    *,
    nurse_id: str = "E2E_USER",
    group_id: str = "E2E_GRP",
    conv_id: str | None = None,
) -> SessionContext:
    return SessionContext(
        office_id="OFF001",
        group_id=group_id,
        year=2026,
        month=5,
        nurse_id=nurse_id,
        nurse_name="E2E 테스터",
        user_role="HN",
        conversation_id=conv_id or str(uuid.uuid4()),
    )


def _make_agent(
    main_llm,
    extractor_facts: list[dict],
) -> tuple[SchedulingAgent, _ExtractorLLM]:
    ext_llm = _ExtractorLLM(extractor_facts)
    extractor = MemoryExtractor(ext_llm)
    agent = SchedulingAgent(
        llm_client=main_llm,
        memory_extractor=extractor,
        enable_user_memory=True,
    )
    return agent, ext_llm


# ── E2E scenarios ─────────────────────────────────────────────


def test_e2e_step1_session_start(db):
    """단계 1: 새 user_id+group_id 로 agent.run 호출 — 오류 없이 응답 반환."""
    main_llm = _TextOnlyLLM()
    agent, _ = _make_agent(main_llm, extractor_facts=[])

    ctx = _ctx(nurse_id="E2E_U1", group_id="E2E_G1")
    result = agent.run(db, "안녕하세요", ctx)

    assert result is not None
    assert result.answer == "ok"


def test_e2e_step2_consolidate_persists_fact(db):
    """단계 2: 사용자 발화 → consolidate_after_turn → AgentUserMemory fact 저장."""
    facts_to_emit = [
        {
            "action": "ADD",
            "fact_type": "nurse_pref",
            "fact_text": "E2E 테스터는 데이 근무를 선호함",
            "source": "USER_STATED",
            "confidence": 1.0,
        }
    ]
    main_llm = _TextOnlyLLM()
    agent, ext_llm = _make_agent(main_llm, facts_to_emit)

    ctx = _ctx(nurse_id="E2E_U2", group_id="E2E_G2", conv_id="conv-e2e-s2")
    result = agent.run(db, "저는 데이 근무가 좋아요", ctx)

    assert result.answer == "ok"
    assert ext_llm.call_count == 1

    # AgentUserMemory 에 fact 저장됨
    rows = (
        db.query(AgentUserMemory)
        .filter(
            AgentUserMemory.user_id == "E2E_U2",
            AgentUserMemory.group_id == "E2E_G2",
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].fact_type == "nurse_pref"
    assert "데이" in rows[0].fact_text
    assert rows[0].valid_to is None  # currently valid


def test_e2e_step3_inject_in_new_session(db):
    """단계 3: 다른 session_id, 동일 user_id+group_id → system prompt 에 prior fact 포함."""
    # 직접 fact 등록 (repo 경유)
    repo = UserMemoryRepo(db)
    repo.apply_fact(
        "ADD",
        {
            "user_id": "E2E_U3",
            "group_id": "E2E_G3",
            "fact_type": "nurse_pref",
            "fact_text": "E2E 테스터는 나이트 근무를 기피함",
            "source": "USER_STATED",
            "confidence": 1.0,
        },
    )
    db.commit()

    # 새 session_id 로 agent.run → inject 검증
    main_llm = _TextOnlyLLM()
    agent, _ = _make_agent(main_llm, extractor_facts=[])

    ctx = _ctx(
        nurse_id="E2E_U3",
        group_id="E2E_G3",
        conv_id=str(uuid.uuid4()),  # 다른 conv_id
    )
    agent.run(db, "오늘 일정 알려줘", ctx)

    # system prompt 에 prior fact 포함 확인
    assert "사용자에 대해 알고 있는 사실" in main_llm.last_system_prompt
    assert "나이트" in main_llm.last_system_prompt


def test_e2e_step4_audit_trail_recorded(db):
    """단계 4: 모든 fact 변경이 AgentMemoryAudit 에 기록됨."""
    user_id = "E2E_U4"
    group_id = "E2E_G4"

    facts_to_emit = [
        {
            "action": "ADD",
            "fact_type": "shift_alias",
            "fact_text": "나이트=N",
            "source": "AGENT_INFERRED",
            "confidence": 0.9,
        }
    ]
    main_llm = _TextOnlyLLM()
    agent, ext_llm = _make_agent(main_llm, facts_to_emit)

    ctx = _ctx(nurse_id=user_id, group_id=group_id)
    agent.run(db, "나이트 쓸 때 N 이라고 해줘", ctx)

    # AgentMemoryAudit 에 WRITE 기록 확인
    audit_rows = (
        db.query(AgentMemoryAudit)
        .filter(AgentMemoryAudit.who == user_id)
        .all()
    )
    assert len(audit_rows) >= 1

    write_rows = [r for r in audit_rows if r.action == "WRITE"]
    assert len(write_rows) >= 1

    a = write_rows[0]
    assert a.tier == "USER"
    assert a.row_id is not None
    assert a.timestamp is not None


def test_e2e_full_flow_add_update_expire_audit(db):
    """E2E 전체 흐름: ADD → UPDATE → expire_fact → audit trail 완결성."""
    user_id = "E2E_FULL"
    group_id = "E2E_FULL_GRP"

    repo = UserMemoryRepo(db)

    # ADD
    add_result = repo.apply_fact(
        "ADD",
        {
            "user_id": user_id,
            "group_id": group_id,
            "fact_type": "nurse_pref",
            "fact_text": "원래 선호 사실",
            "source": "USER_STATED",
            "confidence": 1.0,
            "evidence_session_id": "e2e-full-sess",
        },
    )
    fact_id = add_result["row"]["id"]

    # UPDATE
    repo.apply_fact(
        "UPDATE",
        {
            "user_id": user_id,
            "group_id": group_id,
            "fact_type": "nurse_pref",
            "fact_text": "갱신된 선호 사실",
            "source": "AGENT_INFERRED",
            "confidence": 0.9,
            "evidence_session_id": "e2e-full-sess",
        },
    )

    # expire_fact for new row
    new_row = (
        db.query(AgentUserMemory)
        .filter(
            AgentUserMemory.user_id == user_id,
            AgentUserMemory.group_id == group_id,
            AgentUserMemory.valid_to.is_(None),
        )
        .one()
    )
    repo.expire_fact(new_row.id, group_id=group_id, reason="E2E 만료")

    # audit trail 전체 확인
    audit_rows = (
        db.query(AgentMemoryAudit)
        .filter(AgentMemoryAudit.who == user_id)
        .order_by(AgentMemoryAudit.id.asc())
        .all()
    )

    actions = [r.action for r in audit_rows]

    # ADD → WRITE, UPDATE → EXPIRE + WRITE, expire_fact → EXPIRE
    assert "WRITE" in actions
    assert "EXPIRE" in actions

    # 최소 4 audit rows: WRITE(ADD) + EXPIRE(old) + WRITE(new) + EXPIRE(expire_fact)
    assert len(audit_rows) >= 4

    # 모든 row: tier=USER, timestamp 존재
    for row in audit_rows:
        assert row.tier == "USER"
        assert row.timestamp is not None
