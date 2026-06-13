"""approval/denial corpus — pending_approval 분기 회귀 보호.

agent_v3 의 `_is_confirmation` / `_is_denial` 가 잡아야 하는 자연스러운 한국어
발화 모음. dev_query_log 에서 관찰된 실제 사용자 발화(특히 "그래") 와
일상 변형을 포함.

회귀 사례 (2026-06-01): "그래" 가 _CONFIRM_WORDS 누락 → LLM 추론에 의존,
일관성 낮은 처리. 이 corpus 가 deterministic 처리를 보장.
"""

from __future__ import annotations

# 확정 컨펌 — _is_confirmation True 여야 함
CONFIRM_PHRASES: list[str] = [
    "응",
    "네",
    "예",
    "ㅇㅇ",
    "그래",
    "그렇게 해",
    "그렇게해",
    "맞아",
    "맞아요",
    "맞습니다",
    "확인",
    "진행",
    "진행해",
    "실행",
    "실행해",
    "좋아",
    "오케이",
    "ok",
    "okay",
    "yes",
    "y",
    "yep",
    "응응",
    "네 진행해줘",
    "그래 진행해",
    "응 확인",
]

# 확정 거부 — _is_denial True 여야 함
DENY_PHRASES: list[str] = [
    "아니",
    "아니요",
    "아니오",
    "no",
    "취소",
    "취소해",
    "취소해줘",
    "하지마",
    "그만",
    "됐어",
    "괜찮아",
    "skip",
    "건너뛰기",
    "아니 취소",
    "아니 그만",
]

# 모호한 발화 — 둘 다 False, agent 가 재질의해야 함
AMBIGUOUS_PHRASES: list[str] = [
    "음...",
    "잠깐",
    "잠시만",
    "글쎄",
    "다시 보여줘",
    "한 번 더 설명해줘",
    "어떻게 되는지 봐봐",
    "",  # 빈 문자열
    "   ",  # 공백만
]

# 회귀 함정 — 컨펌처럼 보이지만 confirm 아님(과거 substring 매칭 회귀 차단)
FALSE_POSITIVE_TRAPS: list[str] = [
    # "예전에는" 안에 '예' 포함 — 토큰 분리되면 'is_confirmation' False 가 정답
    "예전에 그랬어",
    "예전 같지 않네",
    # "확인했어" 자체는 confirm 이지만 "확인하지 못했어" 는 아님
    "확인하지 못했어",
    # "네 어쩌고" 가 너무 길면 ack 가 아닌 일반 답변 — _CONFIRM_MAX_LEN 초과
    "네 그런데 이번에는 다른 옵션도 같이 보여줄 수 있을까요? 좀 더 자세한 설명이 필요해요",
]


# ── B9: apply_hint 흐름 전용 corpus ─────────────────────────
# infeasibility resolver 가 "soft 모드로 바꾸면 풀려요. 적용할까요?" 같은
# 옵션을 제시했을 때 사용자가 자연스럽게 답하는 발화.
# CONFIRM/DENY 의 일반 corpus 와 별개로, apply_hint 컨텍스트에서 빈도 높은 표현.

APPLY_HINT_CONFIRM_PHRASES: list[str] = [
    "그렇게 해",
    "그렇게 해줘",
    "적용해",
    "적용해줘",
    "그렇게 진행해",
    "응 그 옵션",
    "그래 그렇게",
]

APPLY_HINT_DENY_PHRASES: list[str] = [
    "아니 다른 방법",
    "그건 싫어",
    "그 옵션 말고",
    "원래대로 해줘",
    "취소 다른 방법은?",
    "아니 그건 안 돼",
]

# 모호 — agent 가 question 을 다시 띄워야 함 (pending_approval 유지)
APPLY_HINT_AMBIGUOUS_PHRASES: list[str] = [
    "그게 뭐야?",
    "어떻게 바뀌어?",
    "soft 모드가 뭐야?",
    "어떤 영향이 있어?",
    "더 자세히 알려줘",
    "다른 옵션은 뭐가 있어?",
]
