"""AgentTestSession — deterministic LLM double + tool-call capture for QA suite.

Usage:
    sess = AgentTestSession(db, group_id="GRP001", office_id="OFF001",
                            year=2026, month=5, user_role="HN")
    result = sess.send("5월 원티드 미제출자 누구야?")
    sess.assert_tool_called("query_schedule", {"scope": "wanted_submissions"})

Two modes:
    (a) DeterministicClient passthrough — uses keyword routing in llm_client.py
        (default). 라우팅 검증에 적합.
    (b) Scripted client — pre-defined response sequence (tool_call / text).
        Clarification flow / multi-turn 검증에 적합.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.agent_v3 import AgentResult, SchedulingAgent
from agents_v2.llm_client import (
    DeterministicClient,
    LLMResponse,
    ToolCall,
)
from agents_v2.schemas.session_context import SessionContext


# ──────────────────────────────────────────────────────────────
# Scripted client — explicit response sequence per turn
# ──────────────────────────────────────────────────────────────


@dataclass
class ScriptedResponse:
    """단일 LLM 응답을 명시. is_text or is_tool_call 중 하나."""

    text: str | None = None
    tool_calls: list[dict] = field(default_factory=list)  # [{name, args}, ...]

    def to_llm_response(self) -> LLMResponse:
        if self.text is not None:
            return LLMResponse(type="text", text=self.text)
        calls = [
            ToolCall(name=tc["name"], args=tc.get("args") or {}, call_id=f"call_{i}")
            for i, tc in enumerate(self.tool_calls)
        ]
        return LLMResponse(type="tool_call", tool_calls=calls)


class ScriptedClient:
    """LLM 응답을 turn 단위로 미리 정의. send() 호출마다 다음 응답 반환.

    tool_call 응답 후에는 자동으로 결과를 답변으로 변환하는 fallback 도 옵션.
    """

    def __init__(self, responses: list[ScriptedResponse], *, auto_answer: bool = True):
        self.responses = list(responses)
        self.auto_answer = auto_answer
        self.idx = 0

    def chat(self, messages: list[dict], tools: list[dict], *, tool_choice: str = "auto") -> LLMResponse:
        # tool result 가 message 마지막에 있으면 → text answer 자동 생성 (auto_answer=True 한정)
        if messages and messages[-1].get("role") == "tool" and self.auto_answer:
            content = messages[-1].get("content", "")
            return LLMResponse(type="text", text=_render_answer(content))

        if self.idx >= len(self.responses):
            return LLMResponse(type="text", text="(scripted client exhausted)")

        resp = self.responses[self.idx]
        self.idx += 1
        return resp.to_llm_response()


def _render_answer(tool_content: str) -> str:
    """Tool result content (JSON string) → 짧은 한국어 응답 stub."""
    return f"결과를 확인했습니다: {tool_content[:120]}"


# ──────────────────────────────────────────────────────────────
# AgentTestSession
# ──────────────────────────────────────────────────────────────


class AgentTestSession:
    """Wraps SchedulingAgent + LLM double + SessionContext for QA tests."""

    def __init__(
        self,
        db: Session,
        *,
        group_id: str = "GRP001",
        office_id: str = "OFF001",
        year: int = 2026,
        month: int = 5,
        nurse_id: str = "N001",
        nurse_name: str = "김민지",
        user_role: str = "HN",
        client: Any = None,
    ):
        self.db = db
        self.ctx = SessionContext(
            office_id=office_id,
            group_id=group_id,
            year=year,
            month=month,
            nurse_id=nurse_id,
            nurse_name=nurse_name,
            user_role=user_role,
        )
        self.client = client or DeterministicClient()
        self.agent = SchedulingAgent(self.client)
        self.last_result: AgentResult | None = None
        self.all_results: list[AgentResult] = []

    # ── interaction ──────────────────────────────────────────

    def send(self, query: str) -> AgentResult:
        """1턴 진행. ctx.messages 누적 → 다음 send() 는 multi-turn 으로 이어짐."""
        result = self.agent.run(self.db, query, self.ctx)
        # multi-turn 을 위해 context 의 messages 를 갱신
        self.ctx.messages = result.messages
        if result.awaiting_approval:
            self.ctx.pending_approval = result.preview
        self.last_result = result
        self.all_results.append(result)
        return result

    # ── inspection ───────────────────────────────────────────

    def planning_stages(self) -> list[dict]:
        """trace 에서 planning stage (skill 라우팅 시점) 만 반환."""
        if not self.last_result:
            return []
        return [s.data for s in self.last_result.trace if s.name == "planning"]

    def execution_stages(self) -> list[dict]:
        if not self.last_result:
            return []
        return [s.data for s in self.last_result.trace if s.name == "execution"]

    def get_tool_calls(self) -> list[tuple[str, dict]]:
        """(skill_name, args) tuples — 라우팅된 모든 tool call 순서대로."""
        return [(p["skill"], p.get("args") or {}) for p in self.planning_stages()]

    def get_first_tool_call(self) -> tuple[str, dict] | None:
        calls = self.get_tool_calls()
        return calls[0] if calls else None

    # ── assertions ───────────────────────────────────────────

    def assert_tool_called(
        self, skill_name: str, params_subset: dict | None = None
    ) -> None:
        """trace 에 (skill_name, args ⊇ params_subset) 인 planning stage 있는지 검증."""
        normalized = skill_name.replace("-", "_")
        matches = []
        for actual_name, actual_args in self.get_tool_calls():
            actual_normalized = (actual_name or "").replace("-", "_")
            if actual_normalized != normalized:
                continue
            if params_subset:
                if not _is_subset(params_subset, actual_args):
                    continue
            matches.append((actual_name, actual_args))

        if not matches:
            all_calls = self.get_tool_calls()
            raise AssertionError(
                f"Expected tool call '{skill_name}' "
                f"with params subset {params_subset!r}, "
                f"but got: {all_calls!r}"
            )

    def assert_no_tool_called(self) -> None:
        calls = self.get_tool_calls()
        if calls:
            raise AssertionError(f"Expected no tool call, but got: {calls!r}")

    def assert_clarification_asked(self) -> None:
        """마지막 응답이 사용자에게 명확화를 요청했는지 — needs_clarification 또는 ? 종결."""
        if not self.last_result:
            raise AssertionError("No result yet — call send() first")
        if self.last_result.needs_clarification:
            return
        text = (self.last_result.answer or "").rstrip()
        if text.endswith("?") or text.endswith("？") or "어느" in text or "어떤" in text:
            return
        raise AssertionError(
            f"Expected clarification question, got answer: {self.last_result.answer!r}"
        )

    def assert_awaiting_approval(self) -> None:
        if not self.last_result:
            raise AssertionError("No result yet")
        if not self.last_result.awaiting_approval:
            raise AssertionError(
                f"Expected awaiting_approval=True, got: {self.last_result.to_dict()!r}"
            )

    def assert_preview_only(self) -> None:
        """가장 최근 tool call 이 preview_only=True 인지 검증 (민감 처리 1차 단계)."""
        calls = self.get_tool_calls()
        if not calls:
            raise AssertionError("No tool call to inspect preview_only")
        _, args = calls[-1]
        if not args.get("preview_only"):
            raise AssertionError(
                f"Expected preview_only=True in last tool call args, got: {args!r}"
            )


# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────


def _is_subset(subset: dict, full: dict) -> bool:
    """subset 의 모든 (key, value) 가 full 에서 동일하게 발견되는지."""
    if not isinstance(full, dict):
        return False
    for k, v in subset.items():
        if k not in full:
            return False
        if full[k] != v:
            return False
    return True
