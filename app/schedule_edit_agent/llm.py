"""LLM 호출 — `agents_v2.llm_client.LLMClient` 와 같은 모양(chat(messages, tools) → LLMResponse)에 사용량을 더한다.

AIDE 클라이언트는 토큰 사용량을 버려서, 턴마다 입력·출력·캐시 토큰을 기록하려고 얇게 따로 둔다
(사용자 지시 11-02: 토큰 사용량 최적화를 위해 컨텍스트 관리를 보고 잴 수 있게).
모델은 `SCHEDULE_EDIT_LLM_MODEL`, 추론 수준은 `SCHEDULE_EDIT_LLM_REASONING`(비우면 보내지 않음).
★ 시험은 가짜 클라이언트로만 돈다 — 서비스 키로 실제 호출하지 않는다.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Protocol

from agents_v2.llm_client import LLMResponse, ToolCall

DEFAULT_MODEL = "gpt-5.5"
MAX_OUTPUT_TOKENS = 800
#: 호출 하나의 대기 상한(초) — SDK 기본은 600초·재시도 2회라 공급자가 느려지면 대화 lock·풀이 슬롯을 쥔 채
#: CloudFront 30초를 넘긴다(검토 HIGH-1). 재시도는 하지 않는다 — 실패하면 그 턴만 '다시 말씀해 주세요'.
CALL_TIMEOUT = 8.0


class EditLLM(Protocol):
    #: 마지막 호출의 사용량 {"model", "input", "output", "cached"} — 기록용
    last_usage: Dict[str, Any]

    def chat(self, messages: List[dict], tools: List[dict], *, tool_choice: str = "auto",
             timeout: Optional[float] = None) -> LLMResponse: ...


class OpenAIEditLLM:
    def __init__(self, model: Optional[str] = None, reasoning: Optional[str] = None):
        from openai import OpenAI

        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=CALL_TIMEOUT, max_retries=0)
        self.model = model or os.getenv("SCHEDULE_EDIT_LLM_MODEL") or DEFAULT_MODEL
        self.reasoning = reasoning if reasoning is not None else os.getenv("SCHEDULE_EDIT_LLM_REASONING")
        self.last_usage: Dict[str, Any] = {}

    def chat(self, messages: List[dict], tools: List[dict], *, tool_choice: str = "auto",
             timeout: Optional[float] = None) -> LLMResponse:
        kwargs: Dict[str, Any] = {"timeout": min(CALL_TIMEOUT, timeout) if timeout else CALL_TIMEOUT}
        if self.reasoning:
            kwargs["reasoning_effort"] = self.reasoning
        response = self.client.chat.completions.create(
            model=self.model, messages=messages,
            tools=[{"type": "function", "function": t} for t in tools] or None,
            tool_choice=tool_choice if tools else None,
            max_completion_tokens=MAX_OUTPUT_TOKENS, **kwargs)
        self.last_usage = _usage(self.model, getattr(response, "usage", None))
        message = response.choices[0].message
        if message.tool_calls:
            return LLMResponse(type="tool_call", _raw=message, tool_calls=[
                ToolCall(name=tc.function.name, args=json.loads(tc.function.arguments or "{}"), call_id=tc.id)
                for tc in message.tool_calls])
        return LLMResponse(type="text", text=message.content or "", _raw=message)


def _usage(model: str, usage) -> Dict[str, Any]:
    if usage is None:
        return {"model": model}
    details = getattr(usage, "prompt_tokens_details", None)
    return {"model": model, "input": getattr(usage, "prompt_tokens", None),
            "output": getattr(usage, "completion_tokens", None),
            "cached": getattr(details, "cached_tokens", None) if details is not None else None}
