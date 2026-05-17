"""QA-5: 모호 query → clarification → 후속 답변 후 expected_skill dispatch 흐름."""

from __future__ import annotations

import pytest

from tests.agent_qa.corpus.queries_clarify import CLARIFY_QUERIES
from tests.agent_qa.harness import (
    AgentTestSession,
    ScriptedClient,
    ScriptedResponse,
)


def _build_clarify_script(entry: dict) -> ScriptedClient:
    """모호 query → clarification 텍스트, follow_up → tool_call 의 2-step 시나리오."""
    clarification_text = (
        f"{entry['expected_clarification_keyword']} 부분을 명확히 해주시겠어요?"
    )
    tool_call_args = dict(entry["expected_params_after"])
    return ScriptedClient([
        ScriptedResponse(text=clarification_text),  # 1차: clarification
        ScriptedResponse(
            tool_calls=[{
                "name": entry["expected_skill_after"].replace("-", "_"),
                "args": tool_call_args,
            }]
        ),  # 2차: 후속 tool_call
    ])


def test_clarify_corpus_size_at_least_5():
    assert len(CLARIFY_QUERIES) >= 5


def test_clarify_dimension_coverage():
    """5 차원 모호성 (field/nurse/month/scope/value) 모두 커버."""
    dimensions = {entry["clarification_expected"] for entry in CLARIFY_QUERIES}
    required = {"field", "nurse", "month", "scope", "value"}
    missing = required - dimensions
    assert not missing, f"missing dimensions: {missing}"


@pytest.mark.parametrize("entry", CLARIFY_QUERIES, ids=lambda e: e["query"][:30])
def test_clarify_flow_two_turn(db, entry):
    """1차 → clarification, 2차 → expected_skill dispatch."""
    cli = _build_clarify_script(entry)
    sess = AgentTestSession(db, client=cli)

    # Turn 1: 모호 query → clarification 응답 기대
    sess.send(entry["query"])
    sess.assert_clarification_asked()
    assert not sess.get_tool_calls(), (
        f"1차 응답에서 tool call 발생 — clarification 만 와야 함. "
        f"calls={sess.get_tool_calls()}"
    )

    # Turn 2: follow_up → expected_skill dispatch 기대
    sess.send(entry["follow_up_response"])
    sess.assert_tool_called(entry["expected_skill_after"])


@pytest.mark.parametrize("entry", CLARIFY_QUERIES, ids=lambda e: e["query"][:30])
def test_clarification_keyword_in_response(db, entry):
    """1차 clarification 응답에 명시적 키워드가 포함되는지 (scripted 보장)."""
    cli = _build_clarify_script(entry)
    sess = AgentTestSession(db, client=cli)
    sess.send(entry["query"])
    answer = sess.last_result.answer
    keyword = entry["expected_clarification_keyword"]
    assert keyword in answer, (
        f"clarification 응답에 '{keyword}' 가 없음: answer={answer!r}"
    )
