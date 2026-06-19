"""Wave-1 corpus — 월간 사이클 마지막 단계 스킬 발화 모음.

대상 스킬:
  - query_generation_job  (생성 job 상태 조회)
  - manage_wanted_deadline (원티드 마감일 변경 / 즉시 마감)

엔트리 형식: (phrase, expected_category, expected_tool_in_scope)
"""

from __future__ import annotations

# ── query_generation_job ─────────────────────────────────────
QUERY_GENERATION_JOB_PHRASES: list[tuple[str, str, str]] = [
    ("근무표 생성 어디까지 갔어?", "generate", "query_generation_job"),
    ("생성 끝났어?", "generate", "query_generation_job"),
    ("마지막 생성 job 상태", "generate", "query_generation_job"),
    ("근무표 자동 생성 진행 상황", "generate", "query_generation_job"),
    ("생성 됐나?", "generate", "query_generation_job"),
]


# ── manage_wanted_deadline ──────────────────────────────────
MANAGE_WANTED_DEADLINE_PHRASES: list[tuple[str, str, str]] = [
    ("7월 원티드 마감일 7월 10일로 바꿔줘", "mutate", "manage_wanted_deadline"),
    ("이번 달 원티드 마감일 없애줘", "mutate", "manage_wanted_deadline"),
    ("7월 원티드 마감해줘", "mutate", "manage_wanted_deadline"),
    ("원티드 즉시 마감", "mutate", "manage_wanted_deadline"),
    ("7월 원티드 마감일 현재 어떻게 돼있어?", "mutate", "manage_wanted_deadline"),
    ("이번 달 원티드 상태 보여줘", "mutate", "manage_wanted_deadline"),
]


ALL_WAVE1_PHRASES: list[tuple[str, str, str]] = (
    QUERY_GENERATION_JOB_PHRASES + MANAGE_WANTED_DEADLINE_PHRASES
)
