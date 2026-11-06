"""Wave-1 corpus routing — query_generation_job / manage_wanted_deadline.

검증:
  1) CATEGORY_TOOLS wiring — corpus 의 expected_tool 이 expected_category 에 있음
  2) route() + Mock LLM — 카테고리만 정확히 뱉으면 tool_names 에 expected_tool 포함
  3) 각 스킬 corpus 최소 5건 보유
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agents_v2.router import CATEGORY_TOOLS, route
from tests.agent_qa.corpus.queries_wave1 import (
    ALL_WAVE1_PHRASES,
    MANAGE_WANTED_DEADLINE_PHRASES,
    QUERY_GENERATION_JOB_PHRASES,
)


# ── 1) wiring ───────────────────────────────────────────


@pytest.mark.parametrize("phrase,category,expected_tool", ALL_WAVE1_PHRASES)
def test_corpus_tool_present_in_category(phrase, category, expected_tool):
    assert category in CATEGORY_TOOLS, f"unknown category {category!r}"
    assert expected_tool in CATEGORY_TOOLS[category], (
        f"{expected_tool!r} 가 CATEGORY_TOOLS[{category!r}] 에 없음 — 발화: {phrase!r}"
    )


# ── 2) route() 통합 ─────────────────────────────────────


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
    def __init__(self, category: str) -> None:
        self._category = category

    def chat(self, messages, tools=None, *, tool_choice=None):  # type: ignore[no-untyped-def]
        return _StaticCategoryResponse(text=f'["{self._category}"]')


@pytest.mark.parametrize("phrase,category,expected_tool", ALL_WAVE1_PHRASES)
def test_route_includes_expected_tool(phrase, category, expected_tool):
    llm = _MockRouterLLM(category)
    result = route(llm, phrase)
    assert not result.fallback_used, f"fallback 발동 — wiring 깨짐: {phrase!r}"
    assert category in result.categories
    assert expected_tool in result.tool_names, (
        f"{expected_tool!r} not in scoped tools — {result.tool_names}"
    )


# ── 3) coverage ────────────────────────────────────────


def test_corpus_minimum_coverage_per_skill():
    assert len(QUERY_GENERATION_JOB_PHRASES) >= 5
    assert len(MANAGE_WANTED_DEADLINE_PHRASES) >= 5
