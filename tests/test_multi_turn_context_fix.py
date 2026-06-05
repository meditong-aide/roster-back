"""Multi-turn context regression — system prompt refresh + strict confirmation matching.

두 가지 버그 fix 검증:

A. 매 turn 마다 system prompt 가 재구성되어 ctx.messages[0] 에 반영됨
   (이전엔 첫 턴 system 이 그대로 고정되어 user_memory/SECURITY_BOUNDARY 가
    2턴 이후 안 들어가던 문제)

B. _is_confirmation/_is_denial 이 substring 대신 exact match + 짧은 메시지
   에서 토큰 매칭만 허용 — '예전에는' 같은 정상 발화의 오탐 방지
"""

from __future__ import annotations

from agents_v2.agent_v3 import (
    _is_confirmation,
    _is_denial,
    SchedulingAgent,
)
from agents_v2.llm_client import LLMResponse, ToolCall
from agents_v2.schemas.session_context import SessionContext


# ── A. System prompt refresh ──────────────────────────────────


class _RecordingLLM:
    """LLM 호출 시 inject 된 system prompt 를 기록만 하는 stub."""

    def __init__(self):
        self.system_prompts: list[str] = []

    def chat(self, messages, tools, *, tool_choice="auto"):
        sys_msg = next((m for m in messages if m.get("role") == "system"), None)
        self.system_prompts.append((sys_msg or {}).get("content", ""))
        # 단발 텍스트 답변으로 종료
        return LLMResponse(type="text", text="ok")


def _make_ctx(year: int, month: int) -> SessionContext:
    return SessionContext(
        office_id="OFF001",
        group_id="GRP001",
        year=year,
        month=month,
        nurse_id="N001",
        nurse_name="김민지",
        user_role="HN",
    )


def test_system_prompt_refreshed_on_second_turn(db, seed_data):
    llm = _RecordingLLM()
    agent = SchedulingAgent(llm, enable_user_memory=False)
    ctx = _make_ctx(2026, 5)

    r1 = agent.run(db, "안녕", ctx)
    ctx.messages = r1.messages

    # 두 번째 턴 — month 만 7 로 바꾼다. system prompt 가 새 month 로 갱신되어야 함.
    ctx.year, ctx.month = 2026, 7
    agent.run(db, "두 번째", ctx)

    assert len(llm.system_prompts) >= 2
    first_sys = llm.system_prompts[0]
    second_sys = llm.system_prompts[-1]
    # 첫 턴은 5월 정상 표시, 두 번째 턴은 7월 — role section 의 "기간:" 한 줄로 확인
    assert "기간: 2026년 5월" in first_sys
    assert "기간: 2026년 7월" in second_sys
    assert "기간: 2026년 5월" not in second_sys


def test_system_prompt_includes_security_boundary_each_turn(db, seed_data):
    """기존 세션이어도 매 턴 SECURITY_BOUNDARY 가 새로 들어가야 함."""
    llm = _RecordingLLM()
    agent = SchedulingAgent(llm, enable_user_memory=False)
    ctx = _make_ctx(2026, 5)

    agent.run(db, "first", ctx)
    ctx.messages = [
        # 기존 세션 simulation — system 자리에 옛 prompt 가 들어 있다고 가정
        {"role": "system", "content": "(legacy old prompt without boundary)"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
    ]
    agent.run(db, "second", ctx)

    second_sys = llm.system_prompts[-1]
    assert "untrusted_tool_output" in second_sys
    assert "user_memory" in second_sys


# ── B. Strict confirmation/denial ─────────────────────────────


def test_confirm_exact_match_passes():
    assert _is_confirmation("응") is True
    assert _is_confirmation("네") is True
    assert _is_confirmation("ok") is True
    assert _is_confirmation("실행해") is True


def test_confirm_short_token_match_passes():
    assert _is_confirmation("응 그래") is True
    assert _is_confirmation("ok 실행해") is True


def test_confirm_long_sentence_no_substring_match():
    """긴 자연 발화에서 'yes'/'예' 같은 음절이 우연히 들어가도 confirm 아님."""
    assert _is_confirmation("예전에는 이렇게 했었지") is False
    assert _is_confirmation("그건 yesterday 데이터야") is False
    # '확인' 이 들어가지만 새 요청
    assert _is_confirmation(
        "이전 데이터를 확인해서 다시 보고서를 작성해주세요"
    ) is False


def test_deny_exact_and_token_match():
    assert _is_denial("아니") is True
    assert _is_denial("취소") is True
    assert _is_denial("아니요, 그냥 취소해") is True


def test_deny_long_sentence_no_substring_match():
    assert _is_denial("아니요 라고 답한 적 없습니다만 그래도 확인 부탁드립니다 라고 길게 말함") is False
    # '아니' 가 들어가도 긴 일반 발화면 거부 아님
    assert _is_denial("이건 아니라고 생각해서 다시 한 번 검토를 부탁드리려고 합니다") is False


def test_empty_message_neither_confirm_nor_deny():
    assert _is_confirmation("") is False
    assert _is_denial("") is False
    assert _is_confirmation("   ") is False
