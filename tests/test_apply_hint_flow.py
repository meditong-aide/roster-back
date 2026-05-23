"""US-B2 — apply_hint 재시도 흐름 검증.

시나리오:
  1. yes 흐름: INFEASIBLE 응답(apply_hint 포함) → "재시도?" 발화 →
               "네" → generate_schedule 재호출 + 결과 반환.
  2. no 흐름: 동일 turn1 → "아니오" → pending_approval 클리어 + "취소" 응답.
  3. cancel 흐름: turn2 → "취소해줘" → no 흐름과 동일.
  4. timeout/모호 입력: turn2 → "음..." → pending_approval 유지 + 재질의.

deterministic LLMClient double + 가짜 generate_schedule fixture로 검증.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents_v2.agent_v3 import (
    AgentResult,
    SchedulingAgent,
    _extract_apply_hint_question,
    _is_denial,
    _DENY_WORDS,
    _CONFIRM_WORDS,
)
from agents_v2.llm_client import DeterministicClient, LLMResponse, ToolCall
from agents_v2.schemas.session_context import SessionContext
from agents_v2.middleware import SkillResult, MiddlewareStep


# ── 공통 픽스처 ────────────────────────────────────────────────


@pytest.fixture
def ctx_base():
    """기본 SessionContext — pending_approval 없음."""
    return SessionContext(
        office_id="OFF001",
        group_id="GRP001",
        year=2026,
        month=5,
        nurse_id="N001",
        nurse_name="김민지",
        user_role="HN",
    )


_APPLY_HINT = {
    "method": "POST",
    "url": "/roster_create/generate",
    "config_overrides": {"team_min_soft_fallback": True},
    "user_consent_required": True,
    "human_message_ko": "'팀 최소 인원' (team_min → soft 모드) 적용 후 재시도합니다. 동의하시겠습니까?",
}

_INFEASIBLE_JOB_DATA = {
    "job_id": "job-test-001",
    "status": "FAILED",
    "infeasibility": {
        "infeasible": True,
        "severity": "HIGH",
        "summary_ko": "팀 최소 인원 부족으로 근무표를 만들 수 없었습니다.",
        "apply_hint": _APPLY_HINT,
    },
    "infeasibility_response": "근무표를 만들 수 없었습니다. 원인: 팀 인원 부족.",
}

_PENDING_APPLY_HINT = {
    "type": "apply_hint",
    "apply_hint": _APPLY_HINT,
    "original_args": {"group_id": "GRP001", "year": 2026, "month": 5},
    "question": _APPLY_HINT["human_message_ko"],
}


def _make_generate_skill_result(data: dict) -> SkillResult:
    return SkillResult(
        data=data,
        duration_ms=50,
        skill_name="generate_schedule",
        grounded_params={},
        middleware_steps=[],
    )


# ── _extract_apply_hint_question 단위 테스트 ───────────────────


class TestExtractApplyHintQuestion:
    def test_returns_human_message_ko(self):
        q = _extract_apply_hint_question("generate_schedule", _INFEASIBLE_JOB_DATA)
        assert q == _APPLY_HINT["human_message_ko"]

    def test_returns_none_for_non_generate_skill(self):
        q = _extract_apply_hint_question("bulk_mutation", _INFEASIBLE_JOB_DATA)
        assert q is None

    def test_returns_none_when_no_infeasibility(self):
        q = _extract_apply_hint_question("generate_schedule", {"status": "QUEUED"})
        assert q is None

    def test_returns_none_when_no_apply_hint(self):
        data = {"infeasibility": {"infeasible": True, "summary_ko": "오류"}}
        q = _extract_apply_hint_question("generate_schedule", data)
        assert q is None

    def test_returns_none_when_user_consent_not_required(self):
        hint = {**_APPLY_HINT, "user_consent_required": False}
        data = {"infeasibility": {"apply_hint": hint}}
        q = _extract_apply_hint_question("generate_schedule", data)
        assert q is None

    def test_returns_default_when_no_human_message(self):
        hint = {**_APPLY_HINT, "human_message_ko": ""}
        data = {"infeasibility": {"apply_hint": hint}}
        q = _extract_apply_hint_question("generate_schedule", data)
        assert q is not None
        assert "재시도" in q

    def test_generate_hyphen_variant_also_matches(self):
        """'generate-schedule' (하이픈) 도 인식."""
        q = _extract_apply_hint_question("generate-schedule", _INFEASIBLE_JOB_DATA)
        assert q == _APPLY_HINT["human_message_ko"]


# ── _is_denial 단위 테스트 ─────────────────────────────────────


class TestIsDenial:
    def test_exact_match_아니오(self):
        assert _is_denial("아니오") is True

    def test_exact_match_취소(self):
        assert _is_denial("취소") is True

    def test_no_exact_match(self):
        assert _is_denial("no") is True

    def test_substring_match(self):
        assert _is_denial("아니요, 그냥 취소해") is True

    def test_confirm_word_not_denial(self):
        assert _is_denial("네") is False
        assert _is_denial("응") is False

    def test_ambiguous_not_denial(self):
        assert _is_denial("음...") is False
        assert _is_denial("잠깐만") is False


# ── LLM double — generate_schedule INFEASIBLE 응답 반환 ────────


class InfeasibleGenerateLLMClient:
    """1회 generate_schedule 호출 후 텍스트 응답으로 종료하는 결정론적 클라이언트."""

    def chat(self, messages: list[dict], tools: list[dict], *, tool_choice: str = "auto") -> LLMResponse:
        # tool result 가 이미 있으면 텍스트 응답
        has_tool = any(m.get("role") == "tool" for m in messages)
        if has_tool:
            return LLMResponse(type="text", text="근무표 생성에 실패했습니다.")
        # 첫 번째 호출: generate_schedule 도구 호출
        return LLMResponse(
            type="tool_call",
            tool_calls=[ToolCall(
                name="generate_schedule",
                args={"group_id": "GRP001", "year": 2026, "month": 5},
                call_id="call_gen_001",
            )],
        )


# ── 시나리오 1: yes 흐름 ───────────────────────────────────────


class TestApplyHintYesFlow:
    """turn1: INFEASIBLE → "재시도?" 발화. turn2: "네" → generate_schedule 재호출."""

    def _make_agent(self):
        return SchedulingAgent(InfeasibleGenerateLLMClient())

    def test_turn1_returns_awaiting_approval_with_apply_hint(self, ctx_base):
        agent = self._make_agent()
        with patch("agents_v2.agent_v3.execute_skill") as mock_exec:
            mock_exec.return_value = _make_generate_skill_result(_INFEASIBLE_JOB_DATA)
            result = agent.run(MagicMock(), "근무표 생성해줘", ctx_base)

        assert result.awaiting_approval is True
        assert result.preview is not None
        assert result.preview.get("type") == "apply_hint"
        assert result.preview["apply_hint"] == _APPLY_HINT
        assert "동의하시겠습니까?" in result.answer

    def test_turn2_yes_calls_generate_schedule_with_constraint_adjustments(self, ctx_base):
        agent = self._make_agent()
        ctx_base.pending_approval = _PENDING_APPLY_HINT

        called_with: dict = {}

        def fake_exec(db, skill_name, args, ctx):
            if skill_name == "generate_schedule":
                called_with.update({"skill": skill_name, "args": args})
                return _make_generate_skill_result({"job_id": "job-retry-001", "status": "QUEUED"})
            return _make_generate_skill_result({})

        with patch("agents_v2.agent_v3.execute_skill", side_effect=fake_exec):
            result = agent.run(MagicMock(), "네", ctx_base)

        assert called_with.get("skill") == "generate_schedule"
        passed_args = called_with.get("args", {})
        assert passed_args.get("constraint_adjustments") == {"team_min_soft_fallback": True}
        assert passed_args.get("preview_only") is False
        assert "재시도" in result.answer

    def test_turn2_yes_variants(self, ctx_base):
        """확인 단어 다양 (응/좋아/ok)."""
        agent = self._make_agent()

        for confirm_word in ("응", "좋아", "ok", "진행"):
            ctx = SessionContext(
                office_id="OFF001", group_id="GRP001", year=2026, month=5,
                pending_approval=dict(_PENDING_APPLY_HINT),
            )
            with patch("agents_v2.agent_v3.execute_skill") as mock_exec:
                mock_exec.return_value = _make_generate_skill_result({"job_id": "job-ok", "status": "QUEUED"})
                result = agent.run(MagicMock(), confirm_word, ctx)
            assert "재시도" in result.answer, f"confirm_word={confirm_word!r} failed"


# ── 시나리오 2: no 흐름 ───────────────────────────────────────


class TestApplyHintNoFlow:
    """turn2: "아니오" → pending_approval 클리어 + "취소" 응답."""

    def test_no_clears_pending_approval(self, ctx_base):
        agent = SchedulingAgent(DeterministicClient())
        ctx_base.pending_approval = _PENDING_APPLY_HINT

        result = agent.run(MagicMock(), "아니오", ctx_base)

        assert "취소" in result.answer
        assert result.awaiting_approval is False

    def test_no_does_not_call_generate_schedule(self, ctx_base):
        agent = SchedulingAgent(DeterministicClient())
        ctx_base.pending_approval = _PENDING_APPLY_HINT

        with patch("agents_v2.agent_v3.execute_skill") as mock_exec:
            result = agent.run(MagicMock(), "아니오", ctx_base)

        mock_exec.assert_not_called()
        assert "취소" in result.answer

    def test_no_variants(self, ctx_base):
        agent = SchedulingAgent(DeterministicClient())
        for deny_word in ("아니요", "no", "그만"):
            ctx = SessionContext(
                office_id="OFF001", group_id="GRP001", year=2026, month=5,
                pending_approval=dict(_PENDING_APPLY_HINT),
            )
            with patch("agents_v2.agent_v3.execute_skill") as mock_exec:
                result = agent.run(MagicMock(), deny_word, ctx)
            mock_exec.assert_not_called()
            assert "취소" in result.answer, f"deny_word={deny_word!r} should cancel"


# ── 시나리오 3: cancel 흐름 (취소해줘) ────────────────────────


class TestApplyHintCancelFlow:
    """turn2: "취소해줘" → no 흐름과 동일."""

    def test_cancel_returns_취소_answer(self, ctx_base):
        agent = SchedulingAgent(DeterministicClient())
        ctx_base.pending_approval = _PENDING_APPLY_HINT

        with patch("agents_v2.agent_v3.execute_skill") as mock_exec:
            result = agent.run(MagicMock(), "취소해줘", ctx_base)

        mock_exec.assert_not_called()
        assert "취소" in result.answer
        assert result.awaiting_approval is False


# ── 시나리오 4: 모호한 입력 → 재질의 ──────────────────────────


class TestApplyHintAmbiguousInput:
    """turn2: "음..." → pending_approval 유지 + 재질의."""

    def test_ambiguous_returns_awaiting_approval(self, ctx_base):
        agent = SchedulingAgent(DeterministicClient())
        ctx_base.pending_approval = _PENDING_APPLY_HINT

        with patch("agents_v2.agent_v3.execute_skill") as mock_exec:
            result = agent.run(MagicMock(), "음...", ctx_base)

        mock_exec.assert_not_called()
        assert result.awaiting_approval is True
        assert result.preview is not None
        assert result.preview.get("type") == "apply_hint"

    def test_ambiguous_preserves_question(self, ctx_base):
        agent = SchedulingAgent(DeterministicClient())
        ctx_base.pending_approval = _PENDING_APPLY_HINT

        with patch("agents_v2.agent_v3.execute_skill"):
            result = agent.run(MagicMock(), "잠깐만요", ctx_base)

        # question 필드가 그대로 응답에 담겨야 함
        assert "동의하시겠습니까?" in result.answer

    def test_ambiguous_variants(self, ctx_base):
        agent = SchedulingAgent(DeterministicClient())
        for ambiguous in ("음...", "잠깐", "글쎄", "모르겠어"):
            ctx = SessionContext(
                office_id="OFF001", group_id="GRP001", year=2026, month=5,
                pending_approval=dict(_PENDING_APPLY_HINT),
            )
            with patch("agents_v2.agent_v3.execute_skill"):
                result = agent.run(MagicMock(), ambiguous, ctx)
            assert result.awaiting_approval is True, f"ambiguous={ambiguous!r} should re-ask"
