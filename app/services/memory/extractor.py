"""MemoryExtractor — LLM-driven fact extraction from conversation turns.

US-A4. Mem0 식 패턴: LLM function call 한 번으로 최근 turn의 메시지에서
사용자 관련 fact를 ADD / UPDATE / DELETE / NOOP 4분류로 추출한다.

CLAUDE.md 정책:
  - regex / lookup table / pattern dict 사용 금지
  - LLM이 의미 기반으로 분류
  - 기존 LLMClient (`app/agents_v2/llm_client.py`) 재사용
    → gpt-4.1-mini primary, claude-haiku-4-5 fallback

LLM 응답은 function call (tool_call) 로 강제한다 — JSON schema validation 보장.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


# PII 검출 — 한국어 이름 + 호칭 패턴 (의료 도메인 PII 방어).
# 사용 정책: LLM 의미 분류는 regex 금지 (CLAUDE.md), 보안 검증은 별개로 허용.
# 예시 매칭: "김민지 간호사", "박혜미 선생님", "이수정 님"
_PII_NAME_HONORIFIC_PATTERN = re.compile(
    r"[가-힣]{2,4}\s*(?:간호사|선생님|선생|님|씨|쌤)"
)


# ── LLM tool schema ─────────────────────────────────────────
#
# 한 번의 호출에서 여러 fact를 한꺼번에 분류해 반환하도록 강제.
# tool name: emit_fact_decisions

_EMIT_TOOL_NAME = "emit_fact_decisions"

_EMIT_TOOL_SCHEMA = {
    "name": _EMIT_TOOL_NAME,
    "description": (
        "최근 대화에서 추출 가능한 사용자/도메인 사실(facts)을 "
        "ADD/UPDATE/DELETE/NOOP 4분류로 정리해 반환한다. "
        "추출할 사실이 없으면 빈 facts 배열을 반환한다. "
        "regex/lookup table을 쓰지 말고 의미를 보고 분류하라."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "description": "추출된 fact decisions. 없으면 빈 배열.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["ADD", "UPDATE", "DELETE", "NOOP"],
                            "description": (
                                "ADD: 새 사실. UPDATE: 기존 사실이 변경됨. "
                                "DELETE: 기존 사실을 더 이상 보유하지 않음. "
                                "NOOP: 추출은 했으나 저장 불필요."
                            ),
                        },
                        "fact_type": {
                            "type": "string",
                            "description": (
                                "사실 종류 — 예: 'nurse_pref', 'shift_alias', "
                                "'ward_pattern', 'monthly_limit_pref', "
                                "'query_pattern' 등 짧은 snake_case identifier."
                            ),
                        },
                        "fact_text": {
                            "type": "string",
                            "description": "사실을 한국어 자연어 한 문장으로 표현.",
                        },
                        "source": {
                            "type": "string",
                            "enum": ["USER_STATED", "AGENT_INFERRED"],
                            "description": (
                                "USER_STATED: 사용자가 명시적으로 발화. "
                                "AGENT_INFERRED: agent가 행동/맥락에서 추론."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "description": "0~1 신뢰도. 명시 발화는 1.0, 추론은 0.5~0.9 권장.",
                        },
                    },
                    "required": ["action", "fact_type", "fact_text", "source"],
                },
            }
        },
        "required": ["facts"],
    },
}


# ── Prompt 텍스트 ────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "당신은 간호사 근무 스케줄링 챗봇의 메모리 추출기입니다.\n"
    "최근 대화에서 사용자에 대해 **장기 기억할 가치가 있는** 사실을 추출하세요.\n\n"
    "추출 대상 예시:\n"
    "  - 사용자가 표명한 선호 (특정 간호사가 선호하는 시프트, 사용자가 자주 묻는 카테고리)\n"
    "  - 도메인 약어/별칭 사용 패턴 (사용자가 '나이트' 대신 'N'을 쓴다 등)\n"
    "  - 특정 nurse/ward의 반복 관측 패턴\n"
    "  - 월별 제약/한도에 대한 사용자 선호\n\n"
    "분류 규칙:\n"
    "  - ADD: 기존 facts에 없는 새 사실\n"
    "  - UPDATE: 기존 facts에 같은 fact_type이 있지만 내용이 달라짐\n"
    "  - DELETE: 사용자가 기존 사실을 명시적으로 부정 / 철회\n"
    "  - NOOP: 추출했지만 저장 불필요 (단순 호기심 질문, 사실 미포함 발화 등)\n\n"
    "제약:\n"
    "  - regex / 문자열 패턴 매칭 금지 — 의미로 판단\n"
    "  - 일반적인 조회/질문 발화는 추출하지 말 것 (빈 facts 반환)\n"
    "  - fact_text는 한국어 한 문장, 100자 이내 권장\n"
    "  - fact_type은 짧은 snake_case identifier (예: nurse_pref)\n"
    "  - **PII 금지**: fact_text에 사람 이름을 직접 쓰지 말 것. "
    "사용자가 '김민지 간호사', '박혜미 선생님' 처럼 발화했어도 "
    "fact_text 에는 nurse_id (예: 'nurse_id=N001') 또는 'the user', "
    "'특정 간호사' 같은 일반화 표현을 사용. 의료 도메인 개인정보 보호.\n"
    f"  - 반드시 `{_EMIT_TOOL_NAME}` tool 한 번만 호출하여 결과를 반환할 것"
)


# ── post-processing constraints ──────────────────────────────

_MAX_FACT_TEXT_LEN = 240  # safety truncation
_VALID_ACTIONS = {"ADD", "UPDATE", "DELETE", "NOOP"}
_VALID_SOURCES = {"USER_STATED", "AGENT_INFERRED"}


def _truncate(text: str, n: int = _MAX_FACT_TEXT_LEN) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _contains_pii(text: str) -> bool:
    """fact_text에 한국어 이름 + 호칭 패턴이 포함되어 있는지 확인.

    LLM이 prompt 지시를 무시하고 사람 이름을 raw로 fact_text 에 넣은 경우의
    last-mile 방어. 의료 도메인 PII 보호 (개인정보보호법 + 의료법).
    """
    return bool(_PII_NAME_HONORIFIC_PATTERN.search(text))


def _validate_fact(fact: dict) -> Optional[dict]:
    """LLM이 내려준 single fact decision을 검증/정규화. 부적합하면 None."""
    action = fact.get("action")
    fact_type = fact.get("fact_type")
    fact_text = fact.get("fact_text")
    source = fact.get("source")

    if action not in _VALID_ACTIONS:
        logger.debug("[extractor] drop fact — invalid action=%r", action)
        return None
    if not isinstance(fact_type, str) or not fact_type.strip():
        logger.debug("[extractor] drop fact — invalid fact_type")
        return None
    if not isinstance(fact_text, str) or not fact_text.strip():
        logger.debug("[extractor] drop fact — invalid fact_text")
        return None
    if source not in _VALID_SOURCES:
        logger.debug("[extractor] drop fact — invalid source=%r", source)
        return None

    # PII last-mile 방어 — LLM prompt 지시 위반 케이스 차단
    if _contains_pii(fact_text):
        logger.warning(
            "[extractor] drop fact — PII detected (Korean name + honorific) in fact_text. "
            "LLM은 nurse_id 또는 일반화 표현을 사용해야 합니다."
        )
        return None

    try:
        confidence = float(fact.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "action": action,
        "fact_type": fact_type.strip(),
        "fact_text": _truncate(fact_text.strip()),
        "source": source,
        "confidence": confidence,
    }


def _format_existing(existing_facts: list[dict] | None) -> str:
    if not existing_facts:
        return "(없음)"
    lines = []
    for f in existing_facts:
        fact_type = f.get("fact_type", "?")
        fact_text = f.get("fact_text", "?")
        lines.append(f"- [{fact_type}] {fact_text}")
    return "\n".join(lines)


def _format_messages(messages: list[dict]) -> str:
    """LLM에게 보여줄 최근 turn의 메시지 요약."""
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            lines.append(f"[{role}] {content.strip()}")
        elif m.get("tool_calls"):
            tc_names = [
                tc.get("function", {}).get("name", "?")
                for tc in m["tool_calls"]
            ]
            lines.append(f"[{role}] (tool_calls: {', '.join(tc_names)})")
    return "\n".join(lines) if lines else "(빈 대화)"


# ── MemoryExtractor ─────────────────────────────────────────


class MemoryExtractor:
    """LLM-driven fact extraction from conversation messages.

    Mem0 패턴: LLM function calling으로 ADD/UPDATE/DELETE/NOOP 4분류.
    regex/lookup table/pattern dict 절대 금지 (CLAUDE.md 정책).
    """

    def __init__(self, llm_client: Any):
        """llm_client: app/agents_v2/llm_client.py의 LLMClient.

        chat(messages, tools, *, tool_choice) -> LLMResponse 인터페이스 가정.
        """
        self.llm = llm_client

    def extract_facts(
        self,
        messages: list[dict],
        existing_facts: list[dict] | None = None,
    ) -> list[dict]:
        """최근 turn의 messages 에서 사용자 fact 추출.

        existing_facts가 주어지면 UPDATE/DELETE 분류 정확도가 올라간다.

        반환: list of {
            "action": "ADD" | "UPDATE" | "DELETE" | "NOOP",
            "fact_type": str,
            "fact_text": str,
            "source": "USER_STATED" | "AGENT_INFERRED",
            "confidence": float (0~1)
        }
        추출할 fact가 없으면 빈 list 반환.
        """
        if not messages:
            return []

        user_prompt = (
            "## 최근 대화\n"
            f"{_format_messages(messages)}\n\n"
            "## 현재 사용자에 대해 이미 알고 있는 사실 (existing facts)\n"
            f"{_format_existing(existing_facts)}\n\n"
            f"위 정보를 바탕으로 `{_EMIT_TOOL_NAME}` tool을 호출하여 "
            "fact decisions를 반환하세요. 추출할 사실이 없으면 facts=[] 로 반환."
        )

        llm_messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            response = self.llm.chat(
                llm_messages,
                tools=[_EMIT_TOOL_SCHEMA],
                tool_choice="auto",
            )
        except Exception as e:
            logger.warning("[extractor] LLM call failed: %s", e)
            return []

        # tool_call 우선
        raw_facts: list[dict] = []
        if getattr(response, "is_tool_call", False):
            for tc in getattr(response, "tool_calls", []) or []:
                if tc.name != _EMIT_TOOL_NAME:
                    continue
                args = tc.args or {}
                items = args.get("facts") or []
                if isinstance(items, list):
                    raw_facts.extend(
                        [x for x in items if isinstance(x, dict)]
                    )
        else:
            # text fallback — JSON 파싱 시도
            text = getattr(response, "text", None) or ""
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    items = parsed.get("facts") or []
                    if isinstance(items, list):
                        raw_facts.extend(
                            [x for x in items if isinstance(x, dict)]
                        )
            except (json.JSONDecodeError, TypeError):
                logger.debug("[extractor] non-JSON text response, ignoring")

        # validate + normalize
        decisions: list[dict] = []
        for f in raw_facts:
            validated = _validate_fact(f)
            if validated is not None:
                decisions.append(validated)

        logger.debug(
            "[extractor] extracted %d valid facts (from %d raw)",
            len(decisions),
            len(raw_facts),
        )
        return decisions
