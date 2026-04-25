"""LLM client — provider-agnostic interface with tool calling support.

Replaces llm_adapter.py (text-only) with tool-calling capable clients.
Supports OpenAI, Anthropic, and Deterministic (test) providers.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


# ── Response ────────────────────────────────────────────────


@dataclass
class ToolCall:
    """Single tool call within an LLM response."""

    name: str
    args: dict
    call_id: str


@dataclass
class LLMResponse:
    """LLM response — either text or tool_call(s).

    Supports parallel tool calls: when LLM returns multiple tool_calls
    in one response, all are captured in `tool_calls` list.
    """

    type: Literal["text", "tool_call"]
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str | None = None
    _raw: Any = field(default=None, repr=False)

    # ── Convenience accessors (backward compat for single-call) ──

    @property
    def tool_name(self) -> str | None:
        return self.tool_calls[0].name if self.tool_calls else None

    @property
    def tool_args(self) -> dict | None:
        return self.tool_calls[0].args if self.tool_calls else None

    @property
    def tool_call_id(self) -> str | None:
        return self.tool_calls[0].call_id if self.tool_calls else None

    @property
    def is_text(self) -> bool:
        return self.type == "text"

    @property
    def is_tool_call(self) -> bool:
        return self.type == "tool_call"

    @property
    def is_parallel(self) -> bool:
        return len(self.tool_calls) > 1

    def as_assistant_message(self) -> dict:
        """Convert to OpenAI-style assistant message for conversation history."""
        if self.is_tool_call:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tc.call_id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(
                                tc.args, ensure_ascii=False
                            ),
                        },
                    }
                    for tc in self.tool_calls
                ],
            }
        return {"role": "assistant", "content": self.text or ""}


# ── Protocol ────────────────────────────────────────────────


class LLMClient(Protocol):
    """Provider-agnostic LLM interface with tool calling."""

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
    ) -> LLMResponse: ...


# ── OpenAI ──────────────────────────────────────────────────


class OpenAIClient:
    """OpenAI client with function calling."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4.1-mini",
    ):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.model = model

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        openai_tools = [{"type": "function", "function": t} for t in tools]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=openai_tools if tools else None,
            tool_choice=tool_choice if tools else None,
            temperature=0.1,
            max_tokens=2048,
        )
        choice = response.choices[0]

        if choice.message.tool_calls:
            calls = [
                ToolCall(
                    name=tc.function.name,
                    args=json.loads(tc.function.arguments),
                    call_id=tc.id,
                )
                for tc in choice.message.tool_calls
            ]
            return LLMResponse(
                type="tool_call",
                tool_calls=calls,
                _raw=choice.message,
            )
        return LLMResponse(
            type="text",
            text=choice.message.content or "",
            _raw=choice.message,
        )


# ── Anthropic ───────────────────────────────────────────────


class AnthropicClient:
    """Anthropic client with tool use."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-haiku-4-5-20251001",
    ):
        import anthropic

        self.client = anthropic.Anthropic(
            api_key=api_key or os.getenv("ANTHROPIC_API_KEY")
        )
        self.model = model

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        # Anthropic: system is separate from messages
        system_msg = ""
        conv_messages = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            elif m["role"] == "tool":
                # Anthropic uses tool_result blocks
                conv_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.get("tool_call_id", ""),
                                "content": m["content"],
                            }
                        ],
                    }
                )
            elif m["role"] == "assistant" and m.get("tool_calls"):
                # Convert OpenAI-style tool_calls to Anthropic tool_use blocks
                tc = m["tool_calls"][0]
                conv_messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tc["id"],
                                "name": tc["function"]["name"],
                                "input": json.loads(tc["function"]["arguments"]),
                            }
                        ],
                    }
                )
            else:
                conv_messages.append(m)

        anthropic_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 2048,
            "messages": conv_messages,
            "temperature": 0.1,
        }
        if system_msg:
            kwargs["system"] = system_msg
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        response = self.client.messages.create(**kwargs)

        calls = [
            ToolCall(name=b.name, args=b.input, call_id=b.id)
            for b in response.content
            if b.type == "tool_use"
        ]
        if calls:
            return LLMResponse(type="tool_call", tool_calls=calls, _raw=response)

        text = next(
            (b.text for b in response.content if b.type == "text"), ""
        )
        return LLMResponse(type="text", text=text, _raw=response)


# ── Deterministic (test) ────────────────────────────────────


class DeterministicClient:
    """API-free deterministic client for testing the tool-calling loop.

    Routes based on Korean keywords in the last user message.
    When a tool result is already in messages, generates a text answer.
    """

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        user_msg = _last_user_message(messages)
        has_result = _has_tool_result(messages)

        # After tool result → generate answer
        if has_result:
            result_data = _last_tool_result(messages)
            return LLMResponse(
                type="text",
                text=self._format_answer(result_data),
            )

        # Route to appropriate tool
        return self._route(user_msg)

    def _route(self, msg: str) -> LLMResponse:
        # 원티드 관련
        if "원티드" in msg or "희망근무" in msg:
            if any(w in msg for w in ["취소", "철회"]):
                name = _extract_name(msg)
                return _tool_call(
                    "bulk_mutation",
                    {
                        "scope": "wanted_submissions",
                        "action": "cancel",
                        "nurse_name": name,
                        "preview_only": True,
                    },
                )
            if any(w in msg for w in ["미제출", "현황", "몇"]):
                return _tool_call(
                    "query_schedule",
                    {"scope": "wanted_submissions", "operation": "count"},
                )
            if "조정" in msg:
                return _tool_call(
                    "query_schedule",
                    {"scope": "wanted_adjustment"},
                )
            return _tool_call(
                "query_schedule",
                {"scope": "wanted_submissions"},
            )

        # 근무표 관련
        if any(w in msg for w in ["근무표", "근무", "스케줄"]):
            if "생성" in msg:
                return _tool_call(
                    "generate_schedule", {"confirm": False}
                )
            if "검증" in msg or "위반" in msg:
                return _tool_call("validate_schedule", {})
            if any(w in msg for w in ["균형", "공정", "불균형", "분석"]):
                return _tool_call(
                    "analyze_report",
                    {"scope": "schedule", "analysis_type": "fairness"},
                )
            if "변경" in msg or "바꿔" in msg or "→" in msg:
                name = _extract_name(msg)
                return _tool_call(
                    "bulk_mutation",
                    {
                        "scope": "schedule",
                        "action": "change_shift",
                        "nurse_name": name,
                        "preview_only": True,
                    },
                )
            if "대체" in msg or "추천" in msg:
                return _tool_call(
                    "recommend_candidates",
                    {"date": "", "shift_name": ""},
                )
            name = _extract_name(msg)
            args: dict[str, Any] = {"scope": "schedule"}
            if name:
                args["nurse_name"] = name
            if "나이트" in msg or "야간" in msg:
                args["shift_name"] = "나이트"
            return _tool_call("query_schedule", args)

        # 간호사 정보
        if any(w in msg for w in ["간호사", "직원", "누구"]):
            return _tool_call(
                "query_schedule", {"scope": "nurse_info"}
            )

        # 설정
        if any(w in msg for w in ["설정", "제약", "규칙"]):
            return _tool_call(
                "query_schedule", {"scope": "constraint_config"}
            )

        return LLMResponse(
            type="text",
            text="요청을 이해하지 못했습니다. 좀 더 구체적으로 말씀해 주세요.",
        )

    def _format_answer(self, data: Any) -> str:
        """Format tool result into a readable answer."""
        if isinstance(data, dict):
            if "error" in data:
                return f"오류가 발생했습니다: {data['error']}"
            if "needs_clarification" in data:
                q = data.get("question", "추가 정보가 필요합니다.")
                opts = data.get("options", [])
                if opts:
                    return f"{q}\n" + "\n".join(
                        f"  {i+1}. {o}" for i, o in enumerate(opts)
                    )
                return q
            if "preview" in data or "preview_only" in data:
                return f"변경 미리보기:\n{json.dumps(data, ensure_ascii=False, indent=2)}\n\n진행할까요?"
        return f"조회 결과:\n{json.dumps(data, ensure_ascii=False, indent=2, default=str)}"


# ── Helpers ─────────────────────────────────────────────────

_KNOWN_NAMES = ["김민지", "박지은", "이수정", "정다은", "최유진", "한서연"]


def _last_user_message(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def _has_tool_result(messages: list[dict]) -> bool:
    return any(m["role"] == "tool" for m in messages)


def _last_tool_result(messages: list[dict]) -> Any:
    for m in reversed(messages):
        if m["role"] == "tool":
            try:
                return json.loads(m["content"])
            except (json.JSONDecodeError, TypeError):
                return m["content"]
    return None


def _extract_name(msg: str) -> str:
    for name in _KNOWN_NAMES:
        if name in msg:
            return name
    # Fallback: extract Korean name pattern (2-4 syllables)
    import re
    m = re.search(r"[가-힣]{2,4}(?=\s|님|의|한테|에게|$)", msg)
    if m:
        # Exclude common non-name words
        candidate = m.group()
        _NON_NAMES = {"원티드", "희망근무", "근무표", "근무", "스케줄", "간호사", "나이트", "이브닝", "데이", "야간", "설정", "병동"}
        if candidate not in _NON_NAMES:
            return candidate
    return ""


def _tool_call(name: str, args: dict) -> LLMResponse:
    return LLMResponse(
        type="tool_call",
        tool_calls=[ToolCall(name=name, args=args, call_id=f"call_{uuid.uuid4().hex[:8]}")],
    )


# ── Factory ─────────────────────────────────────────────────


def get_llm_client(provider: str = "openai") -> LLMClient:
    """Create an LLM client by provider name."""
    if provider == "deterministic":
        return DeterministicClient()
    if provider == "anthropic":
        return AnthropicClient()
    return OpenAIClient()
