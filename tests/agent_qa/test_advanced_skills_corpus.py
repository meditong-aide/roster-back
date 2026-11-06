"""B8: 4개 고도 스킬 corpus 회귀 보호.

검증:
  1) CATEGORY_TOOLS wiring 일관성 — 각 corpus entry 의 expected_tool 이
     해당 expected_category 의 tool 리스트에 살아있는지.
  2) router 통합 — MockLLM 이 expected_category 를 뱉으면 route() 가
     tool_names 에 expected_tool 을 포함시키는지.

이 두 단계로:
  - description / CATEGORY_TOOLS 가 동기화 안 된 경우 1에서 잡힘.
  - route() 의 fallback/스코핑 로직이 깨진 경우 2에서 잡힘.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agents_v2.router import CATEGORY_TOOLS, route
from tests.agent_qa.corpus.queries_advanced_skills import (
    ALL_ADVANCED_PHRASES,
    ANALYZE_REPORT_PHRASES,
    GENERATE_SCHEDULE_PHRASES,
    RECOMMEND_CANDIDATES_PHRASES,
    REPAIR_SCHEDULE_PHRASES,
)


# ── 1) wiring 일관성 ─────────────────────────────────────────


@pytest.mark.parametrize("phrase,category,expected_tool", ALL_ADVANCED_PHRASES)
def test_corpus_tool_present_in_category(
    phrase: str, category: str, expected_tool: str
) -> None:
    assert category in CATEGORY_TOOLS, (
        f"unknown category {category!r} in corpus entry for {phrase!r}"
    )
    assert expected_tool in CATEGORY_TOOLS[category], (
        f"{expected_tool!r} 가 CATEGORY_TOOLS[{category!r}] 에 없음 — "
        f"description/router wiring 불일치. 발화: {phrase!r}"
    )


# ── 2) Mock LLM 으로 route() 통합 ────────────────────────────


@dataclass
class _StaticCategoryResponse:
    text: str
    input_tokens: int = 10
    output_tokens: int = 5
    model: str | None = "mock-router"
    is_tool_call: bool = False
    is_text: bool = True
    tool_calls: list | None = None


class _MockRouterLLM:
    """발화에 대응되는 카테고리 JSON 을 반환하는 결정적 mock.

    실제 LLM 정확도는 별도 e2e 에서 확인 — 여기선 wiring + route() 동작만 검증.
    """

    def __init__(self, category: str) -> None:
        self._category = category

    def chat(self, messages, tools=None, *, tool_choice=None):  # type: ignore[no-untyped-def]
        return _StaticCategoryResponse(text=f'["{self._category}"]')


@pytest.mark.parametrize("phrase,category,expected_tool", ALL_ADVANCED_PHRASES)
def test_route_includes_expected_tool(
    phrase: str, category: str, expected_tool: str
) -> None:
    llm = _MockRouterLLM(category)
    result = route(llm, phrase)
    assert not result.fallback_used, f"fallback 발동 — wiring 깨짐: {phrase!r}"
    assert category in result.categories
    assert expected_tool in result.tool_names, (
        f"{expected_tool!r} not in scoped tools — wiring 검증 실패. "
        f"카테고리: {result.categories}, tools: {result.tool_names}"
    )


# ── 3) 각 스킬별 corpus 최소 5건 보유 ───────────────────────


def test_corpus_minimum_coverage_per_skill() -> None:
    """각 스킬 corpus 가 최소 5건 이상 — 표현 다양성 보장."""
    assert len(ANALYZE_REPORT_PHRASES) >= 5
    assert len(GENERATE_SCHEDULE_PHRASES) >= 5
    assert len(RECOMMEND_CANDIDATES_PHRASES) >= 5
    assert len(REPAIR_SCHEDULE_PHRASES) >= 5
