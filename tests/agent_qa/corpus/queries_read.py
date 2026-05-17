"""설정/상태 조회 query 코퍼스 — 32 entries × 7 domains.

각 entry schema:
    query: str — 수간호사가 자연어로 던지는 질문 (Korean).
    expected_skill: str — agent 가 라우팅해야 하는 skill 이름 (registry key).
    expected_scope: str | None — query_schedule 의 scope param (다른 skill 은 None).
    expected_params_subset: dict — agent 가 채워야 하는 최소 파라미터 (subset 매칭).
    category: str — domain 분류 (roster_config / wanted_config / monthly_limit / schedule /
                    wanted_submission / nurse_info / generation_job).
    note: str | None — 미구현 도메인 (현재 라우팅 실패 예상) 같은 메모.

라우팅 정합성은 tests/agent_qa/test_routing_read.py 에서 검증.
"""

from __future__ import annotations

READ_QUERIES: list[dict] = [
    # ─────── A. RosterConfig 조회 (5) ───────
    {
        "query": "이번 달 야간 최대 횟수 설정 몇 번이야?",
        "expected_skill": "query_schedule",
        "expected_scope": "constraint_config",
        "expected_params_subset": {"scope": "constraint_config"},
        "category": "roster_config",
        "note": None,
    },
    {
        "query": "연속 근무 며칠까지 가능하게 해놨지?",
        "expected_skill": "query_schedule",
        "expected_scope": "constraint_config",
        "expected_params_subset": {"scope": "constraint_config"},
        "category": "roster_config",
        "note": None,
    },
    {
        "query": "팀 밸런스 설정 켜져 있어?",
        "expected_skill": "query_schedule",
        "expected_scope": "constraint_config",
        "expected_params_subset": {"scope": "constraint_config"},
        "category": "roster_config",
        "note": None,
    },
    {
        "query": "데이 필요인원 지금 몇 명이지?",
        "expected_skill": "query_schedule",
        "expected_scope": "constraint_config",
        "expected_params_subset": {"scope": "constraint_config"},
        "category": "roster_config",
        "note": None,
    },
    {
        "query": "스케줄 정책 전체 보여줘",
        "expected_skill": "query_schedule",
        "expected_scope": "constraint_config",
        "expected_params_subset": {"scope": "constraint_config"},
        "category": "roster_config",
        "note": None,
    },
    # ─────── B. WantedConfig 조회 (5) — 현재 미구현 도메인 ───────
    {
        "query": "5월 원티드 마감일 언제야?",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_config",
        "expected_params_subset": {"scope": "wanted_config", "year": 2026, "month": 5},
        "category": "wanted_config",
        "note": "wanted_config scope 미구현 — known_failure",
    },
    {
        "query": "AIDE 기능 켜져 있나?",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_config",
        "expected_params_subset": {"scope": "wanted_config"},
        "category": "wanted_config",
        "note": "wanted_config scope 미구현 — known_failure",
    },
    {
        "query": "원티드 한도 설정 어떻게 되어 있어?",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_config",
        "expected_params_subset": {"scope": "wanted_config"},
        "category": "wanted_config",
        "note": "wanted_config scope 미구현 — known_failure",
    },
    {
        "query": "이번 달 원티드 정책 보여줘",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_config",
        "expected_params_subset": {"scope": "wanted_config"},
        "category": "wanted_config",
        "note": "wanted_config scope 미구현 — known_failure",
    },
    {
        "query": "원티드 수정 가능 기간 알려줘",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_config",
        "expected_params_subset": {"scope": "wanted_config"},
        "category": "wanted_config",
        "note": "wanted_config scope 미구현 — known_failure",
    },
    # ─────── C. NurseMonthlyLimit 조회 (5) — QA-8 에서 구현 ───────
    {
        "query": "김민지 5월 야간 몇 번으로 설정됐어?",
        "expected_skill": "query_schedule",
        "expected_scope": "monthly_limit",
        "expected_params_subset": {"scope": "monthly_limit", "year": 2026, "month": 5},
        "category": "monthly_limit",
        "note": "monthly_limit scope — QA-8 에서 구현 예정",
    },
    {
        "query": "박혜미 4월 데이 정확히 몇 번이야?",
        "expected_skill": "query_schedule",
        "expected_scope": "monthly_limit",
        "expected_params_subset": {"scope": "monthly_limit", "year": 2026, "month": 4},
        "category": "monthly_limit",
        "note": "monthly_limit scope — QA-8",
    },
    {
        "query": "5월 야간 최대치 개인별로 설정한 간호사 누구야?",
        "expected_skill": "query_schedule",
        "expected_scope": "monthly_limit",
        "expected_params_subset": {"scope": "monthly_limit", "year": 2026, "month": 5},
        "category": "monthly_limit",
        "note": "monthly_limit scope — QA-8",
    },
    {
        "query": "이번 달 N 제한 설정된 간호사 목록 보여줘",
        "expected_skill": "query_schedule",
        "expected_scope": "monthly_limit",
        "expected_params_subset": {"scope": "monthly_limit"},
        "category": "monthly_limit",
        "note": "monthly_limit scope — QA-8",
    },
    {
        "query": "박혜미 5월 N 제한 어떻게 되어 있지?",
        "expected_skill": "query_schedule",
        "expected_scope": "monthly_limit",
        "expected_params_subset": {"scope": "monthly_limit", "year": 2026, "month": 5},
        "category": "monthly_limit",
        "note": "monthly_limit scope — QA-8",
    },
    # ─────── D. 근무표 조회 (5) ───────
    {
        "query": "5월 근무표 보여줘",
        "expected_skill": "query_schedule",
        "expected_scope": "schedule",
        "expected_params_subset": {"year": 2026, "month": 5},
        "category": "schedule",
        "note": None,
    },
    {
        "query": "4월 김민지 일정 알려줘",
        "expected_skill": "query_schedule",
        "expected_scope": "schedule",
        "expected_params_subset": {"year": 2026, "month": 4},
        "category": "schedule",
        "note": None,
    },
    {
        "query": "이번 달 야간 누가 들어가?",
        "expected_skill": "query_schedule",
        "expected_scope": "schedule",
        "expected_params_subset": {},
        "category": "schedule",
        "note": None,
    },
    {
        "query": "5월 1일 데이 누구야?",
        "expected_skill": "query_schedule",
        "expected_scope": "schedule",
        "expected_params_subset": {"year": 2026, "month": 5},
        "category": "schedule",
        "note": None,
    },
    {
        "query": "근무표 버전 몇 개 있어?",
        "expected_skill": "query_schedule",
        "expected_scope": "schedule",
        "expected_params_subset": {},
        "category": "schedule",
        "note": None,
    },
    # ─────── E. 원티드 제출 내역 조회 (5) ───────
    {
        "query": "5월 원티드 미제출자 누구야?",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions", "year": 2026, "month": 5},
        "category": "wanted_submission",
        "note": None,
    },
    {
        "query": "김민지 5월 원티드 뭐 냈어?",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions"},
        "category": "wanted_submission",
        "note": None,
    },
    {
        "query": "이번 달 원티드 제출률 어떻게 돼?",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions", "operation": "count"},
        "category": "wanted_submission",
        "note": None,
    },
    {
        "query": "이번 달 원티드 변경 내역 보여줘",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_adjustment",
        "expected_params_subset": {"scope": "wanted_adjustment"},
        "category": "wanted_submission",
        "note": None,
    },
    {
        "query": "박혜미 원티드 어떤 거 신청했어?",
        "expected_skill": "query_schedule",
        "expected_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions"},
        "category": "wanted_submission",
        "note": None,
    },
    # ─────── F. 간호사 정보 조회 (5) ───────
    {
        "query": "김민지 정보 보여줘",
        "expected_skill": "query_schedule",
        "expected_scope": "nurse_info",
        "expected_params_subset": {"scope": "nurse_info"},
        "category": "nurse_info",
        "note": None,
    },
    {
        "query": "야간전담 간호사 누구야?",
        "expected_skill": "query_schedule",
        "expected_scope": "nurse_info",
        "expected_params_subset": {"scope": "nurse_info"},
        "category": "nurse_info",
        "note": None,
    },
    {
        "query": "1팀 간호사 목록 알려줘",
        "expected_skill": "query_schedule",
        "expected_scope": "nurse_info",
        "expected_params_subset": {"scope": "nurse_info"},
        "category": "nurse_info",
        "note": None,
    },
    {
        "query": "경력 3년 이상 간호사 누구누구 있어?",
        "expected_skill": "query_schedule",
        "expected_scope": "nurse_info",
        "expected_params_subset": {"scope": "nurse_info"},
        "category": "nurse_info",
        "note": None,
    },
    {
        "query": "프리셉터 매칭된 간호사 몇 명이야?",
        "expected_skill": "query_schedule",
        "expected_scope": "nurse_info",
        "expected_params_subset": {"scope": "nurse_info"},
        "category": "nurse_info",
        "note": None,
    },
    # ─────── G. 근무표 생성 잡 상태 조회 (2) ───────
    {
        "query": "5월 근무표 생성 진행 어디까지 됐어?",
        "expected_skill": "query_schedule",
        "expected_scope": "generation_job",
        "expected_params_subset": {"scope": "generation_job"},
        "category": "generation_job",
        "note": None,
    },
    {
        "query": "근무표 생성 결과 어떻게 됐어?",
        "expected_skill": "query_schedule",
        "expected_scope": "generation_job",
        "expected_params_subset": {"scope": "generation_job"},
        "category": "generation_job",
        "note": None,
    },
]


def get_queries_by_category(category: str) -> list[dict]:
    """카테고리별 query 필터."""
    return [q for q in READ_QUERIES if q["category"] == category]


def get_known_failures() -> list[dict]:
    """현재 미구현 도메인 (라우팅 실패 예상) entry."""
    return [q for q in READ_QUERIES if q.get("note") and "미구현" in q["note"]]
