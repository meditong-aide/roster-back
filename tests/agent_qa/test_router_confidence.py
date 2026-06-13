"""B14: router 분류 신뢰도 휴리스틱.

categories shape → confidence 매핑이 일관되게 산출돼 RouterResult.to_dict() 와
로그에 반영되는지 검증. e2e LLM 정확도가 아니라 결과 후처리 휴리스틱이 대상.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agents_v2.router import _confidence_for, route


@dataclass
class _Resp:
    text: str
    input_tokens: int = 5
    output_tokens: int = 2
    model: str | None = "mock-router"
    is_tool_call: bool = False
    is_text: bool = True
    tool_calls: list | None = None


class _MockLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    def chat(self, messages, tools=None, *, tool_choice=None):  # type: ignore[no-untyped-def]
        return _Resp(text=self._text)


# ── 휴리스틱 자체 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "cats,expected",
    [
        ([], 0.0),
        (["read"], 1.0),
        (["read", "navigation"], 0.7),
        (["read", "navigation", "analyze"], 0.7),
        (["read", "navigation", "analyze", "mutate"], 0.4),
        (["a", "b", "c", "d", "e"], 0.4),
    ],
)
def test_confidence_heuristic(cats, expected) -> None:
    assert _confidence_for(cats) == expected


# ── route() 통합 — RouterResult.confidence + to_dict() ────────


def test_route_single_category_confidence_1():
    llm = _MockLLM('["read"]')
    result = route(llm, "근무표 보여줘")
    assert result.confidence == 1.0
    assert result.to_dict()["confidence"] == 1.0


def test_route_multi_category_confidence_0_7():
    llm = _MockLLM('["read","navigation"]')
    result = route(llm, "팀 어디서 바꿔")
    assert result.confidence == 0.7


def test_route_fallback_confidence_0():
    """파싱 실패(빈 응답) → fallback + confidence 0."""
    llm = _MockLLM("")  # text 없으면 _classify_raw 가 [] 반환
    result = route(llm, "??")
    assert result.fallback_used is True
    assert result.confidence == 0.0


def test_route_overshoot_confidence_0_4():
    llm = _MockLLM('["read","navigation","analyze","mutate"]')
    result = route(llm, "다 해줘")
    assert result.confidence == 0.4
