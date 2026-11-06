"""B6: memory consolidate LLM 호출의 토큰/비용 회계 검증.

이전엔 MemoryExtractor.extract_facts 가 LLM 을 호출하면서도 토큰을 노출하지 않아
record_llm_usage 가 호출되지 못함 → memory consolidate 비용 영구 누락.

테스트:
  - extract_facts 호출 후 extractor.last_usage 에 (input, output, model) 노출
  - 호출 실패 시 (0, 0, None) 으로 리셋 (이전 호출값 잔존 차단)
  - 빈 messages 입력은 LLM 호출 자체 안 함 → last_usage 변경 없음
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from services.memory.extractor import MemoryExtractor


# ── 가짜 LLM 응답/클라이언트 ───────────────────────────────────


@dataclass
class _FakeResponse:
    is_tool_call: bool = False
    tool_calls: list[Any] | None = None
    text: str | None = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None


class _FakeLLMOK:
    def __init__(self, in_tok: int, out_tok: int, model: str) -> None:
        self._in = in_tok
        self._out = out_tok
        self._model = model

    def chat(self, messages, tools=None, *, tool_choice=None):  # type: ignore[no-untyped-def]
        return _FakeResponse(
            is_tool_call=False,
            text="[]",  # JSON fallback path — 빈 facts
            input_tokens=self._in,
            output_tokens=self._out,
            model=self._model,
        )


class _FakeLLMFail:
    def chat(self, *a, **kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")


# ── 케이스 ─────────────────────────────────────────────────


def test_last_usage_captures_response_tokens():
    ext = MemoryExtractor(_FakeLLMOK(123, 45, "gpt-5.4-nano"))
    ext.extract_facts(messages=[{"role": "user", "content": "안녕"}])
    assert ext.last_usage == (123, 45, "gpt-5.4-nano")


def test_last_usage_reset_on_failure():
    ext = MemoryExtractor(_FakeLLMOK(50, 20, "gpt-5.4-nano"))
    # 첫 호출은 정상 — last_usage 채워짐
    ext.extract_facts(messages=[{"role": "user", "content": "x"}])
    assert ext.last_usage == (50, 20, "gpt-5.4-nano")
    # 두 번째 호출은 LLM 실패 — last_usage 가 (0, 0, None) 으로 리셋되어야 함.
    ext.llm = _FakeLLMFail()
    ext.extract_facts(messages=[{"role": "user", "content": "y"}])
    assert ext.last_usage == (0, 0, None)


def test_empty_messages_skips_llm():
    ext = MemoryExtractor(_FakeLLMOK(999, 999, "gpt-5.4-nano"))
    # 초기값
    assert ext.last_usage == (0, 0, None)
    ext.extract_facts(messages=[])  # 빈 → LLM 호출 안 함
    # last_usage 변동 없음 — 초기값 유지
    assert ext.last_usage == (0, 0, None)


def test_zero_token_response_recorded_as_is():
    """LLM 이 0 토큰 응답을 주면 (0, 0, model) 그대로 기록.

    agent_v3 가 in_tok or out_tok 조건으로 record skip 하므로 0/0 은 자연스럽게 skip.
    """
    ext = MemoryExtractor(_FakeLLMOK(0, 0, "gpt-5.4-nano"))
    ext.extract_facts(messages=[{"role": "user", "content": "x"}])
    assert ext.last_usage == (0, 0, "gpt-5.4-nano")
