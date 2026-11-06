"""세션 히스토리 복원 시 tool_call_id 재구성 — OpenAI 400 회귀 방지.

버그: AgentConversationMessage 에 tool_call_id 컬럼이 없어 영속화 시 소실 →
MSSQL/Redis 재로드된 tool 메시지에 tool_call_id 없음 → 다음 LLM 호출 400
("messages with role 'tool' must have a 'tool_call_id'"). navigate 등 tool 호출
후 대화가 persist→reload 되면 재현. _repair_tool_call_ids 가 assistant.tool_calls
id 를 순서대로 페어링해 복원.
"""

from __future__ import annotations

from services.memory.session_repo import SessionMemoryRepo

repair = SessionMemoryRepo._repair_tool_call_ids


def test_single_tool_call_id_restored():
    msgs = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "그레이드 설정으로 이동해줘"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_abc", "type": "function",
                         "function": {"name": "navigate", "arguments": "{}"}}]},
        {"role": "tool", "content": '{"dispatched": true}'},  # tool_call_id 소실
    ]
    repair(msgs)
    assert msgs[3]["tool_call_id"] == "call_abc"


def test_parallel_tool_calls_paired_in_order():
    msgs = [
        {"role": "assistant", "content": None,
         "tool_calls": [
             {"id": "call_1", "function": {"name": "a", "arguments": "{}"}},
             {"id": "call_2", "function": {"name": "b", "arguments": "{}"}},
         ]},
        {"role": "tool", "content": "r1"},
        {"role": "tool", "content": "r2"},
    ]
    repair(msgs)
    assert msgs[1]["tool_call_id"] == "call_1"
    assert msgs[2]["tool_call_id"] == "call_2"


def test_existing_tool_call_id_preserved():
    msgs = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_x", "function": {"name": "a", "arguments": "{}"}}]},
        {"role": "tool", "content": "r", "tool_call_id": "call_x"},
    ]
    repair(msgs)
    assert msgs[1]["tool_call_id"] == "call_x"  # 이미 있으면 유지


def test_no_preceding_assistant_left_untouched():
    # 앞단 orphan tool (assistant 없음) — 페어링 불가, 예외 없이 통과(_truncate 가 leading orphan 제거)
    msgs = [{"role": "tool", "content": "r"}]
    repair(msgs)
    assert "tool_call_id" not in msgs[0]
