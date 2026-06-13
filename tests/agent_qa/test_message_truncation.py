"""B13: _truncate_for_llm / _group_message_units 단위테스트.

이전엔 LLM 토큰 예산 관리 로직(turn 누적 → context 폭주 방지)이 단위 커버리지
없이 운영 중. tool-call group 이 잘려 LLM 입력이 깨지면 turn 자체가 실패.

검증 항목:
  * _group_message_units
    - assistant(tool_calls) + 뒤따르는 tool → 1 unit (분리 금지)
    - 일반 메시지는 단독 unit
    - orphan tool 단독 unit
    - 빈 입력 → 빈 출력
  * _truncate_for_llm
    - system 보존 + 최신 unit 보존(예산 초과해도)
    - budget 안에선 가능한 많이 포함 (newest-first)
    - tool-call group 은 unit 단위로 통째 포함/제외
    - 절단 후 앞단 orphan tool 제거
    - 빈 입력 / system 없는 입력 경계
"""

from __future__ import annotations

from agents_v2.agent_v3 import _group_message_units, _truncate_for_llm


# ── _group_message_units ────────────────────────────────────


def test_group_units_empty():
    assert _group_message_units([]) == []


def test_group_units_independent_messages():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},  # no tool_calls
        {"role": "user", "content": "c"},
    ]
    units = _group_message_units(msgs)
    assert [len(u) for u in units] == [1, 1, 1]


def test_group_units_tool_call_group_unified():
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "r1"},
        {"role": "tool", "tool_call_id": "tc1", "content": "r2"},
        {"role": "user", "content": "next"},
    ]
    units = _group_message_units(msgs)
    assert [len(u) for u in units] == [1, 3, 1]
    # assistant(tool_calls) + 두 tool 이 하나의 unit
    assert units[1][0]["role"] == "assistant"
    assert all(m["role"] == "tool" for m in units[1][1:])


def test_group_units_orphan_tool_is_solo_unit():
    """assistant tool_calls 없이 등장한 tool 은 단독 unit."""
    msgs = [{"role": "tool", "tool_call_id": "x", "content": "z"}]
    units = _group_message_units(msgs)
    assert len(units) == 1
    assert units[0][0]["role"] == "tool"


def test_group_units_assistant_without_tool_calls_solo():
    msgs = [
        {"role": "assistant", "content": "just text"},
        {"role": "user", "content": "ok"},
    ]
    units = _group_message_units(msgs)
    assert [len(u) for u in units] == [1, 1]


# ── _truncate_for_llm ───────────────────────────────────────


def test_truncate_empty_returns_empty():
    assert _truncate_for_llm([]) == []


def test_truncate_system_only_kept():
    msgs = [{"role": "system", "content": "S"}]
    assert _truncate_for_llm(msgs) == msgs


def test_truncate_no_system_no_head():
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    out = _truncate_for_llm(msgs, max_chars=1000)
    assert out == msgs  # 모두 들어감


def test_truncate_keeps_latest_unit_even_if_over_budget():
    """최신 unit 은 budget 초과해도 무조건 보존 (LLM 호출이 끊기지 않도록)."""
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "x" * 500},
        {"role": "user", "content": "y" * 500},
    ]
    # max_chars 너무 작아도 latest unit 은 보존
    out = _truncate_for_llm(msgs, max_chars=50)
    assert out[0]["role"] == "system"
    # 최신 user 메시지("y...") 가 최소 하나는 살아있음
    assert any(m.get("content", "").startswith("y") for m in out)


def test_truncate_includes_within_budget():
    """budget 충분하면 모두 포함."""
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    out = _truncate_for_llm(msgs, max_chars=10_000)
    assert out == msgs


def test_truncate_drops_orphan_tool_at_front():
    """절단 후 앞단에 tool 만 남으면 chain 무효 — 제거."""
    msgs = [
        {"role": "system", "content": "S"},
        # 잘려 나갈 오래된 메시지들 (budget 작게 줘서 강제 절단)
        {"role": "user", "content": "x" * 800},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc"}]},
        # 절단 경계에 따라 이 tool 이 orphan 으로 남을 수 있음
        {"role": "tool", "tool_call_id": "tc", "content": "y" * 800},
        {"role": "user", "content": "최신 질의"},
    ]
    out = _truncate_for_llm(msgs, max_chars=200)
    # head 다음 첫 non-system 메시지가 tool 이면 안 됨.
    non_system = [m for m in out if m.get("role") != "system"]
    assert not non_system or non_system[0].get("role") != "tool"


def test_truncate_keeps_tool_call_group_intact():
    """tool-call group(assistant+tool) 은 unit 단위 — 부분 포함 금지."""
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        {"role": "user", "content": "다음"},
    ]
    out = _truncate_for_llm(msgs, max_chars=10_000)
    # assistant 가 있으면 같은 unit 의 tool 도 같이 있어야 함.
    role_seq = [m["role"] for m in out]
    if "assistant" in role_seq:
        a_idx = role_seq.index("assistant")
        # 다음 위치에 tool 이 따라와야 (이 케이스에선 같은 unit)
        assert role_seq[a_idx + 1] == "tool"
