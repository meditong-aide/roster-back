"""Wave-3 corpus — 솔버 실패 후속 처리 발화 모음.

대상 스킬:
  - resolve_infeasibility (실패 해결 옵션 카탈로그)

query_generation_job 발화는 wave1 에 이미 있음 (실패 상태 조회와 옵션 카탈로그 carve).

엔트리 형식: (phrase, expected_category, expected_tool_in_scope)
"""

from __future__ import annotations

# ── resolve_infeasibility ────────────────────────────────────
RESOLVE_INFEASIBILITY_PHRASES: list[tuple[str, str, str]] = [
    ("이번 실패 어떻게 풀어", "validate_repair", "resolve_infeasibility"),
    ("7월 근무표 실패 해결 옵션 알려줘", "validate_repair", "resolve_infeasibility"),
    ("원인 알겠고 옵션 뭐 있어?", "validate_repair", "resolve_infeasibility"),
    ("infeasible 어떻게 풀까", "validate_repair", "resolve_infeasibility"),
    ("이번 실패 옵션 보여줘", "validate_repair", "resolve_infeasibility"),
    ("실패 해결 방법", "validate_repair", "resolve_infeasibility"),
]


ALL_WAVE3_PHRASES: list[tuple[str, str, str]] = list(RESOLVE_INFEASIBILITY_PHRASES)
