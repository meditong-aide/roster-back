"""QA-3 smoke test — AgentTestSession 자체 동작 검증."""

from __future__ import annotations

import pytest

from tests.agent_qa.harness import (
    AgentTestSession,
    ScriptedClient,
    ScriptedResponse,
    _is_subset,
)


# ── _is_subset unit ────────────────────────────────────────


def test_is_subset_true():
    assert _is_subset({"a": 1}, {"a": 1, "b": 2})
    assert _is_subset({}, {"a": 1})


def test_is_subset_false_missing_key():
    assert not _is_subset({"a": 1, "c": 3}, {"a": 1, "b": 2})


def test_is_subset_false_value_mismatch():
    assert not _is_subset({"a": 2}, {"a": 1})


def test_is_subset_full_not_dict():
    assert not _is_subset({"a": 1}, ["a", 1])


# ── ScriptedClient unit ────────────────────────────────────


def test_scripted_client_text():
    cli = ScriptedClient([ScriptedResponse(text="안녕하세요")])
    resp = cli.chat([{"role": "user", "content": "hi"}], tools=[])
    assert resp.is_text
    assert resp.text == "안녕하세요"


def test_scripted_client_tool_call():
    cli = ScriptedClient(
        [ScriptedResponse(tool_calls=[{"name": "query_schedule", "args": {"scope": "nurse_info"}}])]
    )
    resp = cli.chat([{"role": "user", "content": "hi"}], tools=[])
    assert resp.is_tool_call
    assert resp.tool_calls[0].name == "query_schedule"
    assert resp.tool_calls[0].args == {"scope": "nurse_info"}


def test_scripted_client_exhaustion_returns_text():
    cli = ScriptedClient([])
    resp = cli.chat([{"role": "user", "content": "hi"}], tools=[])
    assert resp.is_text


# ── AgentTestSession smoke ─────────────────────────────────


def test_session_send_returns_result(db):
    sess = AgentTestSession(db)
    result = sess.send("이번 달 근무표 보여줘")
    assert result is not None
    assert sess.last_result is result


def test_session_records_planning_stage(db):
    """deterministic client 가 query_schedule 로 라우팅하는지."""
    sess = AgentTestSession(db)
    sess.send("이번 달 근무표 보여줘")
    calls = sess.get_tool_calls()
    # 최소 1 tool call 이 있어야 함
    assert len(calls) >= 1, f"no tool calls recorded for query, trace: {sess.last_result.trace}"


def test_session_assert_tool_called_pass(db):
    sess = AgentTestSession(db)
    sess.send("이번 달 근무표 보여줘")
    # deterministic routes to query_schedule
    sess.assert_tool_called("query_schedule")


def test_session_assert_tool_called_fail_raises(db):
    sess = AgentTestSession(db)
    sess.send("이번 달 근무표 보여줘")
    with pytest.raises(AssertionError):
        sess.assert_tool_called("generate_schedule")


def test_session_scripted_clarification(db):
    """ScriptedClient 로 clarification 응답을 직접 주입."""
    sess = AgentTestSession(
        db, client=ScriptedClient([ScriptedResponse(text="어느 필드를 변경하실까요?")])
    )
    sess.send("야간 늘려줘")
    sess.assert_clarification_asked()


def test_session_multi_turn_accumulates_messages(db):
    sess = AgentTestSession(db)
    sess.send("이번 달 근무표 보여줘")
    msgs_after_1 = len(sess.ctx.messages)
    sess.send("4월은?")
    msgs_after_2 = len(sess.ctx.messages)
    assert msgs_after_2 > msgs_after_1, "multi-turn 시 messages 가 누적되어야 함"
