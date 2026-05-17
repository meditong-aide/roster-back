"""모호한 query 코퍼스 — clarification 흐름 검증용.

각 entry:
    query: 1차 모호 query.
    clarification_expected: 어떤 차원의 모호성인가 (field|nurse|month|scope|value).
    expected_clarification_keyword: agent 응답에 포함되어야 할 키워드 (예: "어느", "어떤").
    follow_up_response: 사용자 후속 답변.
    expected_skill_after: follow_up 후 호출되어야 하는 skill.
    expected_params_after: follow_up 후 tool_call args 의 subset.
"""

from __future__ import annotations

CLARIFY_QUERIES: list[dict] = [
    # ── (1) 필드 모호성 — "야간 늘려줘" → max_nig_per_month vs nig_req ──
    {
        "query": "야간 늘려줘",
        "clarification_expected": "field",
        "expected_clarification_keyword": "어느",
        "follow_up_response": "월 최대 야간 횟수요",
        "expected_skill_after": "update_constraint",
        "expected_params_after": {"field": "max_nig_per_month"},
    },
    # ── (2) 간호사 모호성 — 동명이인 ──
    {
        "query": "민지 야간전담으로 바꿔줘",
        "clarification_expected": "nurse",
        "expected_clarification_keyword": "어느",
        "follow_up_response": "김민지요",
        "expected_skill_after": "update_person_attr",
        "expected_params_after": {},
    },
    # ── (3) 월 모호성 — 월 미지정 ──
    {
        "query": "원티드 미제출자 누구야",
        "clarification_expected": "month",
        "expected_clarification_keyword": "어느 달",
        "follow_up_response": "5월",
        "expected_skill_after": "query_schedule",
        "expected_params_after": {"scope": "wanted_submissions", "month": 5},
    },
    # ── (4) scope 모호성 — "원티드 보여줘" 가 제출 내역인지 정책인지 ──
    {
        "query": "원티드 어떻게 돼?",
        "clarification_expected": "scope",
        "expected_clarification_keyword": "어떤",
        "follow_up_response": "제출 내역",
        "expected_skill_after": "query_schedule",
        "expected_params_after": {"scope": "wanted_submissions"},
    },
    # ── (5) 값 모호성 — "최대" 가 정확히 몇인지 ──
    {
        "query": "야간 최대치 좀 줄여줘",
        "clarification_expected": "value",
        "expected_clarification_keyword": "몇",
        "follow_up_response": "5회로",
        "expected_skill_after": "update_constraint",
        "expected_params_after": {"field": "max_nig_per_month", "value": 5},
    },
]
