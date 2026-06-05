"""US-A4: Memory middleware integration — inject + consolidate hooks 검증.

시나리오:
  1. session 1: 사용자 발화 → consolidate_after_turn → AgentUserMemory fact 저장
  2. session 2 (다른 session_id, 동일 user_id+group_id):
     inject_user_memory_context → system prompt 에 fact 포함 확인
  3. session 3 (다른 user_id, 같은 group_id):
     inject 시 다른 사용자의 fact 미주입 (격리)
  4. session 4 (다른 group_id):
     inject 시 미주입 (RBAC 격리)
"""

from __future__ import annotations

import uuid

import pytest

from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.llm_client import LLMResponse, ToolCall
from agents_v2.schemas.session_context import SessionContext
from db.models import AgentUserMemory
from services.memory.extractor import MemoryExtractor
from services.memory.user_repo import UserMemoryRepo


# ── LLM doubles ───────────────────────────────────────────────


class _TextOnlyLLM:
    """SchedulingAgent 메인 turn LLM — 항상 짧은 text 응답.

    Tier-1 turn 의 'system prompt' 를 캡쳐할 수 있도록 마지막 system content 보관.
    """

    def __init__(self):
        self.last_system_prompt: str = ""
        self.last_messages: list[dict] = []
        self.responses: list[str] = []

    def chat(self, messages, tools, *, tool_choice: str = "auto"):
        self.last_messages = list(messages)
        # 첫 메시지가 system 이면 캡쳐
        for m in messages:
            if m.get("role") == "system":
                self.last_system_prompt = m.get("content") or ""
                break
        return LLMResponse(type="text", text="ok")


class _ExtractorLLM:
    """Extractor 가 호출하는 LLM — scripted facts 반환.

    SchedulingAgent 의 turn LLM 과는 분리되어야 — 둘이 합쳐지면
    system prompt 첫 메시지가 다르게 보이기 때문.
    """

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
    nurse_id: str = "N001",
    group_id: str = "GRP001",
    conv_id: str | None = None,
) -> SessionContext:
    return SessionContext(
        office_id="OFF001",
        group_id=group_id,
        year=2026,
        month=4,
        nurse_id=nurse_id,
        nurse_name="김민지",
        user_role="HN",
        conversation_id=conv_id or str(uuid.uuid4()),
    )


def _make_agent(
    main_llm,
    extractor_facts: list[dict],
    enable_user_memory: bool = True,
) -> tuple[SchedulingAgent, _ExtractorLLM]:
    ext_llm = _ExtractorLLM(extractor_facts)
    extractor = MemoryExtractor(ext_llm)
    agent = SchedulingAgent(
        llm_client=main_llm,
        memory_extractor=extractor,
        enable_user_memory=enable_user_memory,
    )
    return agent, ext_llm


# ── Scenarios ─────────────────────────────────────────────────


def test_consolidate_persists_fact_after_turn(db, seed_data):
    """session 1: 사용자 발화 → consolidate → AgentUserMemory row 1개."""
    facts_to_emit = [
        {
            "action": "ADD",
            "fact_type": "nurse_pref",
            "fact_text": "사용자는 김민지가 나이트 근무를 선호함을 명시",
            "source": "USER_STATED",
            "confidence": 1.0,
        }
    ]
    main_llm = _TextOnlyLLM()
    agent, ext_llm = _make_agent(main_llm, facts_to_emit)

    ctx = _ctx(nurse_id="N001", group_id=seed_data["group_id"])
    result = agent.run(db, "김민지는 나이트가 좋아요", ctx)

    assert result.answer == "ok"
    assert ext_llm.call_count == 1  # consolidate 한 번 호출됨

    rows = (
        db.query(AgentUserMemory)
        .filter(
            AgentUserMemory.user_id == "N001",
            AgentUserMemory.group_id == seed_data["group_id"],
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].fact_type == "nurse_pref"
    assert "나이트" in rows[0].fact_text
    assert rows[0].valid_to is None


def test_inject_uses_facts_from_prior_session(db, seed_data):
    """session 2 (다른 conv_id, 동일 user+group): inject 시 prior fact 가 system prompt 에 포함."""
    # ── session 1: fact 등록 ──
    repo = UserMemoryRepo(db)
    repo.apply_fact(
        "ADD",
        {
            "user_id": "N001",
            "group_id": seed_data["group_id"],
            "fact_type": "nurse_pref",
            "fact_text": "사용자는 김민지가 나이트를 선호한다고 말함",
            "source": "USER_STATED",
            "confidence": 1.0,
        },
    )
    db.commit()

    # ── session 2: extractor 는 빈 facts 로 두고, inject 만 검증 ──
    main_llm = _TextOnlyLLM()
    agent, _ = _make_agent(main_llm, extractor_facts=[])

    ctx = _ctx(nurse_id="N001", group_id=seed_data["group_id"])
    agent.run(db, "오늘 근무자 누구야?", ctx)

    assert "사용자에 대해 알고 있는 사실" in main_llm.last_system_prompt
    assert "나이트" in main_llm.last_system_prompt
    assert "nurse_pref" in main_llm.last_system_prompt


def test_inject_isolates_by_user_id(db, seed_data):
    """다른 user_id 로 inject 시 — 타 사용자의 fact 미주입."""
    repo = UserMemoryRepo(db)
    repo.apply_fact(
        "ADD",
        {
            "user_id": "N001",
            "group_id": seed_data["group_id"],
            "fact_type": "nurse_pref",
            "fact_text": "N001 사용자만의 비밀 선호 fact",
            "source": "USER_STATED",
            "confidence": 1.0,
        },
    )
    db.commit()

    main_llm = _TextOnlyLLM()
    agent, _ = _make_agent(main_llm, extractor_facts=[])

    # 다른 nurse_id 로 turn 실행
    ctx = _ctx(nurse_id="N002", group_id=seed_data["group_id"])
    agent.run(db, "안녕", ctx)

    assert "비밀 선호 fact" not in main_llm.last_system_prompt
    # memory 블록 자체도 없을 것 (N002 fact 없음)
    assert "사용자에 대해 알고 있는 사실" not in main_llm.last_system_prompt


def test_inject_isolates_by_group_id(db, seed_data):
    """다른 group_id 로 inject 시 — 타 group 의 fact 미주입 (RBAC)."""
    repo = UserMemoryRepo(db)
    repo.apply_fact(
        "ADD",
        {
            "user_id": "N001",
            "group_id": "GRP_A",
            "fact_type": "nurse_pref",
            "fact_text": "GRP_A 그룹의 사실",
            "source": "USER_STATED",
            "confidence": 1.0,
        },
    )
    db.commit()

    main_llm = _TextOnlyLLM()
    agent, _ = _make_agent(main_llm, extractor_facts=[])

    # 동일 user_id, 다른 group_id 로 turn
    ctx = _ctx(nurse_id="N001", group_id="GRP_B")
    agent.run(db, "안녕", ctx)

    assert "GRP_A 그룹의 사실" not in main_llm.last_system_prompt


def test_inject_skipped_when_no_facts_exist(db, seed_data):
    """fact 가 없으면 memory 블록 자체 미주입 (토큰 절약)."""
    main_llm = _TextOnlyLLM()
    agent, _ = _make_agent(main_llm, extractor_facts=[])

    ctx = _ctx(nurse_id="N999_new_user", group_id=seed_data["group_id"])
    agent.run(db, "안녕", ctx)

    assert "사용자에 대해 알고 있는 사실" not in main_llm.last_system_prompt


def test_enable_user_memory_false_disables_all_hooks(db, seed_data):
    """enable_user_memory=False 면 inject/consolidate 모두 스킵."""
    # pre-existing fact 가 있어도 inject 안 됨
    repo = UserMemoryRepo(db)
    repo.apply_fact(
        "ADD",
        {
            "user_id": "N001",
            "group_id": seed_data["group_id"],
            "fact_type": "nurse_pref",
            "fact_text": "주입되면 안 되는 fact",
            "source": "USER_STATED",
        },
    )
    db.commit()

    main_llm = _TextOnlyLLM()
    # extractor LLM 으로 fact 발화 한 건 등록되어도 — consolidate 안 됨
    agent, ext_llm = _make_agent(
        main_llm,
        extractor_facts=[
            {
                "action": "ADD",
                "fact_type": "nurse_pref",
                "fact_text": "consolidate 안 되어야 함",
                "source": "USER_STATED",
            }
        ],
        enable_user_memory=False,
    )

    ctx = _ctx(nurse_id="N001", group_id=seed_data["group_id"])
    agent.run(db, "..." , ctx)

    assert "주입되면 안 되는 fact" not in main_llm.last_system_prompt
    assert ext_llm.call_count == 0

    # consolidate 안 되었으니 새 fact row 없음 (기존 1건만)
    rows = (
        db.query(AgentUserMemory)
        .filter(AgentUserMemory.user_id == "N001")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].fact_text == "주입되면 안 되는 fact"


def test_consolidate_failure_is_silent(db, seed_data):
    """consolidate 단계에서 예외가 나도 agent 응답 영향 X."""

    class _BoomExtractor:
        def extract_facts(self, messages, existing_facts=None):
            raise RuntimeError("simulated extractor crash")

    main_llm = _TextOnlyLLM()
    agent = SchedulingAgent(
        llm_client=main_llm,
        memory_extractor=_BoomExtractor(),
        enable_user_memory=True,
    )

    ctx = _ctx(nurse_id="N001", group_id=seed_data["group_id"])
    result = agent.run(db, "안녕", ctx)

    # turn 자체는 성공
    assert result.answer == "ok"


def test_consolidate_evidence_session_id_recorded(db, seed_data):
    """consolidate 시 conversation_id → evidence_session_id 저장."""
    facts_to_emit = [
        {
            "action": "ADD",
            "fact_type": "shift_alias",
            "fact_text": "사용자는 '나이트'를 'N'으로 부름",
            "source": "USER_STATED",
            "confidence": 0.9,
        }
    ]
    main_llm = _TextOnlyLLM()
    agent, _ = _make_agent(main_llm, facts_to_emit)

    conv_id = "conv-evidence-test"
    ctx = _ctx(
        nurse_id="N001",
        group_id=seed_data["group_id"],
        conv_id=conv_id,
    )
    agent.run(db, "N 근무자 누구야?", ctx)

    rows = (
        db.query(AgentUserMemory)
        .filter(
            AgentUserMemory.user_id == "N001",
            AgentUserMemory.fact_type == "shift_alias",
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].evidence_session_id == conv_id
