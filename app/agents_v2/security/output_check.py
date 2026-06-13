"""Layer B — agent 출력 누출 검사.

응답 반환 직전 검사:
  1) 시스템 프롬프트 sentinel 누출 (SECURITY_BOUNDARY 등 고정 문구)
  2) 크로스-테넌트 group_id 누출 (GRP\\d+ 형식, 사용자 group 외)
  3) 내부 도구/API 경로/파일 경로 노출
  4) 시크릿 형태(긴 base64/hex) 잡음

결과:
  - OK            : 그대로 반환
  - REDACTED      : 누출 부분 마스킹 후 반환
  - BLOCKED       : 답변 전체를 일반 메시지로 대체 (심각 누출)

CLAUDE.md: 보안 경계는 regex 적정. intent 해석 영역과 분리.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# ── 결과 타입 ──────────────────────────────────────────────────


class OutputVerdict(str, Enum):
    OK = "ok"
    REDACTED = "redacted"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class OutputCheckResult:
    """출력 검사 결과.

    verdict: OK/REDACTED/BLOCKED
    answer: 최종 사용자에게 보낼 답변 (마스킹/치환된 본문)
    reason_codes: 걸린 누출 카테고리
    """

    verdict: OutputVerdict
    answer: str
    reason_codes: tuple[str, ...]


# ── 패턴 ───────────────────────────────────────────────────────

# group_id 형식 — 운영 환경에서 GRP + 숫자(or 알파넘). 너무 일반적인 'GRP'
# 한 단어는 피하고 'GRP[알파넘]{3,}' 로 제한.
_GROUP_ID_RE = re.compile(r"\bGRP[A-Z0-9]{3,}\b")

# 시스템 프롬프트 sentinel — 응답에 절대 나오면 안 되는 구절.
# agent_v3.py 의 system prompt 헤더와 SECURITY_BOUNDARY 키워드.
_SYSTEM_SENTINELS: tuple[str, ...] = (
    "SECURITY_BOUNDARY",
    "<<SYSTEM>>",
    "<|im_start|>",
    "당신은 AIDE 스케줄링 에이전트",
    "domain knowledge:",
    "routines:",
)

# 내부 API/파일 경로 패턴
_INTERNAL_PATH_RE = re.compile(
    r"(?:^|[\s\(\[\"'`])"
    r"(?:/app/|/tests/|/api/internal/|app/agents_v2/|app/services/)"
    r"[\w./_\-]+",
)

# 도구 이름 (내부 식별자 — 사용자에게 노출되면 안 됨)
_TOOL_NAME_RE = re.compile(
    r"\b(manage_grade|manage_team_min|bulk_mutation|query_schedule|"
    r"update_constraint|update_person_attr|generate_schedule|"
    r"validate_schedule|recommend_candidates|repair_schedule|"
    r"analyze_report|navigate|prefill)\s*\([^)]*\)",
)

# 길이 임계: base64/hex 같은 50자+ 토큰류 (시크릿 흔적)
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{50,}\b")


# ── Public API ─────────────────────────────────────────────────


_REDACT_TAG = "[보호됨]"
_BLOCK_MESSAGE = "내부 처리 중 응답을 표시할 수 없습니다. 다시 시도해주세요."


def check_output(answer: str, *, user_group_id: str | None) -> OutputCheckResult:
    """answer 를 검사하고 (verdict, 최종 answer, 사유) 반환.

    user_group_id 가 None 이면 cross-tenant 검사 생략 (로그인 컨텍스트 부재).
    sentinel/내부경로/긴토큰은 발견 즉시 BLOCKED (답변 전체를 generic 으로 치환).
    cross-tenant group_id 는 REDACTED (해당 토큰만 마스킹).
    """
    if not answer:
        return OutputCheckResult(verdict=OutputVerdict.OK, answer=answer, reason_codes=())

    reasons: list[str] = []

    # 1) 시스템 sentinel — 발견 즉시 BLOCKED
    for sentinel in _SYSTEM_SENTINELS:
        if sentinel in answer:
            reasons.append("SYSTEM_PROMPT_LEAK")
            return OutputCheckResult(
                verdict=OutputVerdict.BLOCKED,
                answer=_BLOCK_MESSAGE,
                reason_codes=tuple(reasons),
            )

    # 2) 내부 경로/도구 호출 형식 누출 → BLOCKED
    if _INTERNAL_PATH_RE.search(answer):
        reasons.append("INTERNAL_PATH_LEAK")
        return OutputCheckResult(
            verdict=OutputVerdict.BLOCKED,
            answer=_BLOCK_MESSAGE,
            reason_codes=tuple(reasons),
        )
    if _TOOL_NAME_RE.search(answer):
        reasons.append("TOOL_NAME_LEAK")
        return OutputCheckResult(
            verdict=OutputVerdict.BLOCKED,
            answer=_BLOCK_MESSAGE,
            reason_codes=tuple(reasons),
        )

    # 3) 긴 토큰 (시크릿 흔적) → BLOCKED
    if _LONG_TOKEN_RE.search(answer):
        reasons.append("LONG_TOKEN_LEAK")
        return OutputCheckResult(
            verdict=OutputVerdict.BLOCKED,
            answer=_BLOCK_MESSAGE,
            reason_codes=tuple(reasons),
        )

    # 4) cross-tenant group_id — 사용자 외 group_id 가 답변에 등장 → REDACTED
    redacted = answer
    if user_group_id:
        leaked_ids: set[str] = set()
        for m in _GROUP_ID_RE.finditer(answer):
            gid = m.group(0)
            if gid != user_group_id:
                leaked_ids.add(gid)
        if leaked_ids:
            reasons.append("CROSS_TENANT_GROUP_ID")
            for gid in leaked_ids:
                redacted = redacted.replace(gid, _REDACT_TAG)

    if reasons:
        return OutputCheckResult(
            verdict=OutputVerdict.REDACTED,
            answer=redacted,
            reason_codes=tuple(reasons),
        )
    return OutputCheckResult(verdict=OutputVerdict.OK, answer=answer, reason_codes=())
