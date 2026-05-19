"""US-A4: MemoryExtractor — LLM-driven fact extraction unit tests.

Deterministic LLM double 로 ADD/UPDATE/DELETE/NOOP 4분류를 검증한다.
실제 OpenAI/Anthropic 호출 없이 결정적 응답을 주입한다.

CLAUDE.md 정책: regex/lookup table 금지 — LLM이 의미로 분류한다.
이 테스트는 Extractor 가 LLM tool_call 응답을 올바르게 파싱/검증하는지를 본다.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from agents_v2.llm_client import LLMResponse, ToolCall
from services.memory.extractor import MemoryExtractor


# ── Deterministic LLM double ─────────────────────────────────


class _ScriptedLLM:
    """주입된 facts 리스트를 emit_fact_decisions tool_call 로 반환.

    facts=None → text response (빈 응답) 로 fallback path 검증.
    facts=[] → tool_call 이지만 빈 facts.
    """

    def __init__(self, facts: list[dict] | None):
        self.facts = facts
        self.calls: list[dict] = []

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools})

        if self.facts is None:
            return LLMResponse(type="text", text="(no extraction)")

        tc = ToolCall(
            name="emit_fact_decisions",
            args={"facts": self.facts},
            call_id=f"call_{uuid.uuid4().hex[:8]}",
        )
        return LLMResponse(type="tool_call", tool_calls=[tc])


class _RaisingLLM:
    def chat(self, *args, **kwargs):  # noqa: D401
        raise RuntimeError("simulated LLM failure")


# ── Scenarios ────────────────────────────────────────────────


def _msgs_user(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def test_extract_add_scenario():
    """사용자가 새 선호 발화 → action=ADD."""
    scripted = [
        {
            "action": "ADD",
            "fact_type": "nurse_pref",
            "fact_text": "사용자는 김민지가 나이트 근무를 선호한다고 말함",
            "source": "USER_STATED",
            "confidence": 1.0,
        }
    ]
    extractor = MemoryExtractor(_ScriptedLLM(scripted))
    out = extractor.extract_facts(_msgs_user("김민지는 나이트를 좋아해"))

    assert len(out) == 1
    f = out[0]
    assert f["action"] == "ADD"
    assert f["fact_type"] == "nurse_pref"
    assert "나이트" in f["fact_text"]
    assert f["source"] == "USER_STATED"
    assert f["confidence"] == 1.0


def test_extract_update_scenario():
    """기존 fact 가 있고 사용자가 변경 발화 → action=UPDATE."""
    existing = [
        {
            "fact_type": "nurse_pref",
            "fact_text": "사용자는 김민지가 나이트를 선호한다고 말함",
        }
    ]
    scripted = [
        {
            "action": "UPDATE",
            "fact_type": "nurse_pref",
            "fact_text": "사용자는 김민지가 이브닝을 선호한다고 말함",
            "source": "USER_STATED",
            "confidence": 0.95,
        }
    ]
    extractor = MemoryExtractor(_ScriptedLLM(scripted))
    out = extractor.extract_facts(
        _msgs_user("아니 김민지는 이제 이브닝이 더 좋대"),
        existing_facts=existing,
    )

    assert len(out) == 1
    assert out[0]["action"] == "UPDATE"
    assert "이브닝" in out[0]["fact_text"]


def test_extract_delete_scenario():
    """사용자가 '이제 그거 안 해' 식 부정 발화 → action=DELETE."""
    existing = [
        {
            "fact_type": "nurse_pref",
            "fact_text": "사용자는 김민지가 나이트를 선호한다고 말함",
        }
    ]
    scripted = [
        {
            "action": "DELETE",
            "fact_type": "nurse_pref",
            "fact_text": "기존 선호 철회 (사용자가 더 이상 적용 안 함)",
            "source": "USER_STATED",
            "confidence": 1.0,
        }
    ]
    extractor = MemoryExtractor(_ScriptedLLM(scripted))
    out = extractor.extract_facts(
        _msgs_user("김민지 나이트 선호 이제 무시해줘"),
        existing_facts=existing,
    )

    assert len(out) == 1
    assert out[0]["action"] == "DELETE"


def test_extract_noop_scenario_returns_empty_or_noop():
    """일반 조회 발화 → action=NOOP 또는 빈 list."""
    # case A: LLM 이 NOOP 한 건 반환
    scripted = [
        {
            "action": "NOOP",
            "fact_type": "query_pattern",
            "fact_text": "사용자가 단순 근무표 조회를 요청",
            "source": "AGENT_INFERRED",
            "confidence": 0.5,
        }
    ]
    extractor = MemoryExtractor(_ScriptedLLM(scripted))
    out = extractor.extract_facts(_msgs_user("오늘 근무자 누구야?"))
    assert len(out) == 1
    assert out[0]["action"] == "NOOP"

    # case B: LLM 이 빈 list 반환
    extractor2 = MemoryExtractor(_ScriptedLLM([]))
    out2 = extractor2.extract_facts(_msgs_user("오늘 근무자 누구야?"))
    assert out2 == []


def test_extract_empty_messages_short_circuits():
    """빈 messages 면 LLM 호출 없이 즉시 []."""
    llm = _ScriptedLLM([])
    extractor = MemoryExtractor(llm)
    out = extractor.extract_facts([])
    assert out == []
    assert llm.calls == []  # LLM 호출 0회


def test_extract_drops_invalid_action():
    """LLM 이 잘못된 action 을 반환하면 그 fact 만 drop."""
    scripted = [
        {
            "action": "BOGUS",  # invalid
            "fact_type": "nurse_pref",
            "fact_text": "...",
            "source": "USER_STATED",
        },
        {
            "action": "ADD",
            "fact_type": "shift_alias",
            "fact_text": "사용자는 '나이트'를 'N'으로 부름",
            "source": "USER_STATED",
            "confidence": 0.9,
        },
    ]
    extractor = MemoryExtractor(_ScriptedLLM(scripted))
    out = extractor.extract_facts(_msgs_user("N 누구야?"))
    assert len(out) == 1
    assert out[0]["fact_type"] == "shift_alias"


def test_extract_drops_invalid_source():
    """LLM 이 허용 외 source 를 반환하면 drop."""
    scripted = [
        {
            "action": "ADD",
            "fact_type": "nurse_pref",
            "fact_text": "...",
            "source": "SYSTEM_OPS",  # not in {USER_STATED, AGENT_INFERRED}
        }
    ]
    extractor = MemoryExtractor(_ScriptedLLM(scripted))
    out = extractor.extract_facts(_msgs_user("..."))
    assert out == []


def test_extract_truncates_long_fact_text():
    """fact_text가 너무 길면 240자 + 말줄임표로 자른다."""
    long_text = "가" * 500
    scripted = [
        {
            "action": "ADD",
            "fact_type": "ward_pattern",
            "fact_text": long_text,
            "source": "AGENT_INFERRED",
            "confidence": 0.7,
        }
    ]
    extractor = MemoryExtractor(_ScriptedLLM(scripted))
    out = extractor.extract_facts(_msgs_user("길게 말함"))
    assert len(out) == 1
    assert len(out[0]["fact_text"]) <= 240
    assert out[0]["fact_text"].endswith("…")


def test_extract_handles_text_response_fallback():
    """LLM 이 tool_call 대신 text를 반환해도 JSON 파싱 시도."""

    class _TextLLM:
        def chat(self, *args, **kwargs):
            payload = {
                "facts": [
                    {
                        "action": "ADD",
                        "fact_type": "monthly_limit_pref",
                        "fact_text": "사용자는 max_nig=5 를 선호",
                        "source": "USER_STATED",
                        "confidence": 0.8,
                    }
                ]
            }
            return LLMResponse(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            )

    extractor = MemoryExtractor(_TextLLM())
    out = extractor.extract_facts(_msgs_user("월 나이트 5번이면 좋겠어"))
    assert len(out) == 1
    assert out[0]["fact_type"] == "monthly_limit_pref"


def test_extract_silent_on_llm_exception():
    """LLM 호출 자체가 실패해도 silent — 빈 list 반환."""
    extractor = MemoryExtractor(_RaisingLLM())
    out = extractor.extract_facts(_msgs_user("..."))
    assert out == []


def test_extract_passes_existing_facts_to_prompt():
    """existing_facts 가 user_prompt 에 포함되는지 검증."""
    existing = [
        {"fact_type": "nurse_pref", "fact_text": "기존 선호 1"},
        {"fact_type": "shift_alias", "fact_text": "사용자는 '이브닝'을 'E'로 부름"},
    ]
    llm = _ScriptedLLM([])
    extractor = MemoryExtractor(llm)
    extractor.extract_facts(_msgs_user("..."), existing_facts=existing)

    assert len(llm.calls) == 1
    user_msg = next(
        m for m in llm.calls[0]["messages"] if m["role"] == "user"
    )["content"]
    assert "기존 선호 1" in user_msg
    assert "shift_alias" in user_msg
