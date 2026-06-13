"""Layer A — agent 입력 분류기.

분류:
  SAFE        — 통과
  SUSPICIOUS  — 통과 + 로그 (UX 영향 X). 의심스럽지만 정상 발화 가능.
  MALICIOUS   — 차단. 일반 거절 메시지 반환.

탐지 카테고리:
  1) 시스템 지시 오버라이드 (ignore/disregard/이전 지시 무시)
  2) 역할 조작 (you are now / 당신은 이제)
  3) 시스템 프롬프트 추출 (print/show + system|prompt|instructions)
  4) 도구 호출 직접 흉내 (JSON tool_call 패턴)
  5) 크로스-테넌트 접근 (모든 그룹/병원, all groups)
  6) 토큰 스머글링 (<|im_start|> 등 모델 특수 토큰)
  7) 길이 폭주 (8000자+)

Note: 한국어 변형 다양성 때문에 false-negative 가 늘 있을 수 있음 →
  Layer B(output check) 가 2차 방어. 또한 위반 시 reason_code 로 어떤 패턴이
  걸렸는지 명시 → 디버깅·튜닝 가능.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

# ── 상수 ────────────────────────────────────────────────────────

# 단일 메시지 길이 상한. 정상 사용자 발화는 거의 1000자 미만.
MAX_INPUT_LENGTH = 8000

# 분류 결과
class InputVerdict(str, Enum):
    SAFE = "safe"
    SUSPICIOUS = "suspicious"
    MALICIOUS = "malicious"


@dataclass(frozen=True)
class InputCheckResult:
    """입력 분류 결과.

    verdict: SAFE/SUSPICIOUS/MALICIOUS
    reason_codes: 걸린 카테고리 코드 (디버깅용)
    block_message: MALICIOUS 시 사용자에게 보여줄 메시지
    """

    verdict: InputVerdict
    reason_codes: tuple[str, ...]
    block_message: str | None = None


# ── 패턴 정의 ──────────────────────────────────────────────────
# 각 카테고리는 (reason_code, regex, malicious_or_suspicious) 튜플.
# regex 는 IGNORECASE + MULTILINE. 한국어/영어 변형을 OR 로 묶음.

_SYSTEM_OVERRIDE_PATTERNS: tuple[tuple[str, re.Pattern[str], InputVerdict], ...] = (
    (
        "OVERRIDE_IGNORE_PREVIOUS",
        re.compile(
            r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above|earlier)\s+"
            r"(instructions|messages|prompts|rules|directives)",
            re.IGNORECASE,
        ),
        InputVerdict.MALICIOUS,
    ),
    (
        "OVERRIDE_IGNORE_PREVIOUS_KO",
        re.compile(
            r"(이전|위|앞|기존|모든)\s*(지시|명령|규칙|규정|지침|프롬프트)\s*"
            r"(을|를|는|은|들을)?\s*\S{0,10}?\s*(무시|잊어|버려|취소|지워)",
        ),
        InputVerdict.MALICIOUS,
    ),
    (
        "OVERRIDE_NEW_INSTRUCTIONS",
        re.compile(
            r"(new|updated|true)\s+(instructions|system\s+prompt|directives):",
            re.IGNORECASE,
        ),
        InputVerdict.MALICIOUS,
    ),
)

_ROLE_MANIPULATION_PATTERNS: tuple[tuple[str, re.Pattern[str], InputVerdict], ...] = (
    (
        "ROLE_YOU_ARE_NOW",
        re.compile(r"you\s+are\s+(now|actually|really)\s+a?\s*\w+", re.IGNORECASE),
        InputVerdict.SUSPICIOUS,
    ),
    (
        "ROLE_ACT_AS",
        re.compile(r"(act|pretend|behave|respond)\s+as\s+(a|an|the)\s+\w+", re.IGNORECASE),
        InputVerdict.SUSPICIOUS,
    ),
    (
        "ROLE_KO",
        re.compile(r"(너는|당신은|넌)\s*(이제|지금부터)\s*\S+\s*(이야|입니다|이다|야)"),
        InputVerdict.SUSPICIOUS,
    ),
    (
        "JAILBREAK_DAN",
        re.compile(r"\b(DAN|jailbreak|developer\s+mode|dev\s+mode)\b", re.IGNORECASE),
        InputVerdict.MALICIOUS,
    ),
)

_PROMPT_EXTRACTION_PATTERNS: tuple[tuple[str, re.Pattern[str], InputVerdict], ...] = (
    (
        "EXTRACT_SYSTEM_PROMPT",
        re.compile(
            r"(print|show|reveal|display|output|tell|repeat|give)\s+"
            r"(me\s+)?(your|the|its)?\s*"
            r"(system|initial|original|hidden|secret)\s+"
            r"(prompt|instruction|directive|rule|message)s?",
            re.IGNORECASE,
        ),
        InputVerdict.MALICIOUS,
    ),
    (
        "EXTRACT_SYSTEM_PROMPT_KO",
        re.compile(
            r"(시스템|초기|숨겨진|원본|원래의)\s*(프롬프트|지시|명령|지침)\s*"
            r"(을|를|는|은)?\s*(보여|알려|출력|공개|말해)",
        ),
        InputVerdict.MALICIOUS,
    ),
    (
        "EXTRACT_SECRETS",
        re.compile(
            r"(api[\s_-]?key|secret|password|token|credential)s?", re.IGNORECASE
        ),
        InputVerdict.SUSPICIOUS,
    ),
)

_TOOL_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str], InputVerdict], ...] = (
    (
        "TOOL_CALL_JSON",
        re.compile(
            r'\{[^{}]*"(name|tool|function|action)"\s*:\s*"[\w\-]+"',
        ),
        InputVerdict.MALICIOUS,
    ),
    (
        "TOOL_CALL_TAG",
        re.compile(r"<\s*tool[_\s]?call\s*>|<\s*function[_\s]?call\s*>", re.IGNORECASE),
        InputVerdict.MALICIOUS,
    ),
)

_CROSS_TENANT_PATTERNS: tuple[tuple[str, re.Pattern[str], InputVerdict], ...] = (
    (
        "CROSS_TENANT_ALL_GROUPS",
        re.compile(r"\b(all|every)\s+(group|tenant|hospital|ward)s?\b", re.IGNORECASE),
        InputVerdict.MALICIOUS,
    ),
    (
        "CROSS_TENANT_OTHER_KO",
        re.compile(
            r"(모든|전체|다른|타)\s*(병원|병동|그룹|테넌트|소속|기관)",
        ),
        InputVerdict.MALICIOUS,
    ),
    (
        "CROSS_TENANT_GROUP_ID",
        re.compile(r"group_id\s*[:=]?\s*['\"]?\w+['\"]?", re.IGNORECASE),
        InputVerdict.SUSPICIOUS,
    ),
)

_TOKEN_SMUGGLING_PATTERNS: tuple[tuple[str, re.Pattern[str], InputVerdict], ...] = (
    (
        "SPECIAL_TOKEN",
        re.compile(
            r"<\|(im_start|im_end|system|user|assistant|endoftext|start|end)\|>",
            re.IGNORECASE,
        ),
        InputVerdict.MALICIOUS,
    ),
    (
        "MARKER_BLOCK",
        re.compile(r"#{3,}\s*(system|assistant|user|prompt)\s*#{0,}", re.IGNORECASE),
        InputVerdict.SUSPICIOUS,
    ),
)


_ALL_PATTERN_GROUPS: tuple[
    tuple[tuple[str, re.Pattern[str], InputVerdict], ...], ...
] = (
    _SYSTEM_OVERRIDE_PATTERNS,
    _ROLE_MANIPULATION_PATTERNS,
    _PROMPT_EXTRACTION_PATTERNS,
    _TOOL_INJECTION_PATTERNS,
    _CROSS_TENANT_PATTERNS,
    _TOKEN_SMUGGLING_PATTERNS,
)


# ── Public API ─────────────────────────────────────────────────


_BLOCK_MESSAGE = (
    "죄송합니다. 해당 요청은 처리할 수 없습니다. 다시 정확한 질문을 부탁드립니다."
)


def classify_input(text: str) -> InputCheckResult:
    """사용자 발화를 분류한다. db/network 의존성 없음, pure function.

    - 빈 문자열 / 공백뿐 → SAFE (chat_router 가 별도 차단)
    - 길이 초과 → MALICIOUS (LENGTH_EXCEEDED)
    - 패턴 매칭 → 가장 강한 verdict 채택. 한 입력에 여러 reason_code 가능.
    """
    if not text:
        return InputCheckResult(verdict=InputVerdict.SAFE, reason_codes=())

    if len(text) > MAX_INPUT_LENGTH:
        return InputCheckResult(
            verdict=InputVerdict.MALICIOUS,
            reason_codes=("LENGTH_EXCEEDED",),
            block_message=_BLOCK_MESSAGE,
        )

    reason_codes: list[str] = []
    worst: InputVerdict = InputVerdict.SAFE
    for group in _ALL_PATTERN_GROUPS:
        for code, pattern, verdict in group:
            if pattern.search(text):
                reason_codes.append(code)
                if _verdict_rank(verdict) > _verdict_rank(worst):
                    worst = verdict

    block_message = _BLOCK_MESSAGE if worst is InputVerdict.MALICIOUS else None
    return InputCheckResult(
        verdict=worst,
        reason_codes=tuple(reason_codes),
        block_message=block_message,
    )


def _verdict_rank(v: InputVerdict) -> int:
    return {
        InputVerdict.SAFE: 0,
        InputVerdict.SUSPICIOUS: 1,
        InputVerdict.MALICIOUS: 2,
    }[v]
