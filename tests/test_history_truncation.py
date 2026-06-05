"""Token budget — _truncate_for_llm 단위 검증.

LLM injection 직전 messages slice 의 동작 확인. 영구 messages 변수 자체는
agent_v3.run() 흐름에서 그대로 보존되므로 본 테스트는 helper 함수에 집중.
"""

from __future__ import annotations

from agents_v2.agent_v3 import _truncate_for_llm, _message_size


def _sys(content: str = "system prompt") -> dict:
    return {"role": "system", "content": content}


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _assistant(content: str = "", tool_calls: list | None = None) -> dict:
    m: dict = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def _tool(content: str) -> dict:
    return {"role": "tool", "content": content}


def test_empty_messages_returns_empty():
    assert _truncate_for_llm([]) == []


def test_short_messages_unchanged():
    """예산 안에 모두 들어가면 list 그대로."""
    msgs = [_sys("S"), _user("안녕"), _assistant("네 어떻게 도와드릴까요?")]
    result = _truncate_for_llm(msgs, max_chars=10_000)
    assert result == msgs


def test_system_always_preserved_when_first():
    """첫 system message는 항상 head 에 유지."""
    long_user = _user("x" * 5000)
    msgs = [_sys("S"), long_user, _user("최신")]
    result = _truncate_for_llm(msgs, max_chars=10_000)
    assert result[0]["role"] == "system"
    # 최신 user 는 반드시 포함
    assert any(m.get("content") == "최신" for m in result)


def test_long_history_drops_oldest():
    """예산 초과 시 오래된 user/assistant 부터 drop."""
    msgs = [
        _sys("S"),
        _user("turn1 " + "a" * 2000),
        _assistant("응답1 " + "b" * 2000),
        _user("turn2 " + "c" * 2000),
        _assistant("응답2 " + "d" * 2000),
        _user("최신 turn"),
    ]
    # max_chars 작게 → 가장 오래된 것부터 빠짐
    result = _truncate_for_llm(msgs, max_chars=3000)
    # system 은 유지
    assert result[0]["role"] == "system"
    # 최신 turn 은 반드시 포함
    assert result[-1]["content"] == "최신 turn"
    # turn1 (가장 오래됨) 은 drop
    contents = [m.get("content", "") for m in result]
    assert not any(c.startswith("turn1 ") for c in contents)


def test_no_system_at_head_still_works():
    """system 없이 시작하는 경우도 동작."""
    msgs = [_user("a"), _assistant("b"), _user("c")]
    result = _truncate_for_llm(msgs, max_chars=10_000)
    assert len(result) == 3
    assert result[0]["role"] == "user"


def test_tool_calls_count_toward_size():
    """assistant.tool_calls 는 _message_size 에 포함."""
    huge_tc = [{"function": {"name": "x", "arguments": "y" * 5000}}]
    m = _assistant("", tool_calls=huge_tc)
    assert _message_size(m) > 1000


def test_system_exceeds_budget_still_includes_latest():
    """system 만으로 예산 초과해도 latest 는 반드시 함께 반환.

    LLM 호출이 의미를 가지려면 최소 system + latest user 보존 필수.
    build_system_prompt 가 31k chars (도메인 지식 + skills) 라 빈번한 시나리오.
    """
    huge_sys = _sys("S" * 10000)
    msgs = [huge_sys, _user("최신")]
    result = _truncate_for_llm(msgs, max_chars=1000)
    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert result[-1]["content"] == "최신"


def test_only_system_no_rest_returned_as_is():
    """rest 가 비어 있고 system 만 있으면 그대로 반환."""
    msgs = [_sys("S")]
    result = _truncate_for_llm(msgs, max_chars=100)
    assert result == msgs


def test_latest_message_always_kept_even_if_alone_exceeds():
    """가장 최근 메시지는 예산 초과해도 유지 (LLM 호출 의미 보장)."""
    msgs = [_sys("S"), _user("X" * 10000)]
    result = _truncate_for_llm(msgs, max_chars=500)
    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert result[-1]["role"] == "user"
    assert result[-1]["content"] == "X" * 10000


def test_original_messages_list_not_mutated():
    """원본 list 와 dict 는 mutate 되지 않음."""
    msgs = [_sys("S"), _user("a"), _assistant("b"), _user("c")]
    snapshot = [dict(m) for m in msgs]
    _truncate_for_llm(msgs, max_chars=10)
    for original, after in zip(snapshot, msgs):
        assert original == after
