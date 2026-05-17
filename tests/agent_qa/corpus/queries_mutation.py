"""설정/상태 수정 query 코퍼스 — 32 entries × 6 domains.

각 entry schema:
    query: str — 수간호사 자연어 명령 (Korean).
    expected_skill: str — agent 가 라우팅해야 하는 skill (registry key).
    expected_action: str | None — bulk_mutation 의 action / generate_schedule 의 동작 등.
    expected_field_or_scope: str | None — update_constraint 의 field, update_person_attr 의
                                          최우선 mutation field, bulk_mutation 의 scope 등.
    expected_params_subset: dict — agent 가 채워야 하는 최소 파라미터 (subset 매칭).
    sensitivity: "low" | "high" — high 는 preview→confirm 2-step 강제 대상.
    category: str — 도메인 분류.
    note: str | None — 미구현 도메인 등 메모.

QA-4 (routing) / QA-7 (sensitive confirm) 에서 사용.
"""

from __future__ import annotations

MUTATION_QUERIES: list[dict] = [
    # ─────── A. RosterConfig 변경 (5) ───────
    {
        "query": "야간 최대 7회로 바꿔줘",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "max_nig_per_month",
        "expected_params_subset": {"field": "max_nig_per_month", "value": 7},
        "sensitivity": "high",
        "category": "roster_config",
        "note": None,
    },
    {
        "query": "팀 밸런스 켜줘",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "team_balance_enable",
        "expected_params_subset": {"field": "team_balance_enable", "value": True},
        "sensitivity": "low",
        "category": "roster_config",
        "note": None,
    },
    {
        "query": "데이 필요인원 5명으로 설정해줘",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "day_req",
        "expected_params_subset": {"field": "day_req", "value": 5},
        "sensitivity": "high",
        "category": "roster_config",
        "note": None,
    },
    {
        "query": "연속 근무 최대 5일로 제한",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "max_conseq_work",
        "expected_params_subset": {"field": "max_conseq_work", "value": 5},
        "sensitivity": "high",
        "category": "roster_config",
        "note": None,
    },
    {
        "query": "이브닝 다음날 데이 금지 해제해줘",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "banned_day_after_eve",
        "expected_params_subset": {"field": "banned_day_after_eve", "value": False},
        "sensitivity": "low",
        "category": "roster_config",
        "note": None,
    },
    # ─────── B. 개인 속성 변경 (5) ───────
    {
        "query": "김민지 야간 전담으로 바꿔줘",
        "expected_skill": "update_person_attr",
        "expected_action": None,
        "expected_field_or_scope": "is_night_nurse",
        "expected_params_subset": {},
        "sensitivity": "high",
        "category": "person_attr",
        "note": None,
    },
    {
        "query": "박혜미 1팀으로 이동",
        "expected_skill": "update_person_attr",
        "expected_action": None,
        "expected_field_or_scope": "team_id",
        "expected_params_subset": {},
        "sensitivity": "high",
        "category": "person_attr",
        "note": None,
    },
    {
        "query": "김지은 그레이드 3으로",
        "expected_skill": "update_person_attr",
        "expected_action": None,
        "expected_field_or_scope": "grade",
        "expected_params_subset": {},
        "sensitivity": "high",
        "category": "person_attr",
        "note": None,
    },
    {
        "query": "박춘일 주말 오프 해제",
        "expected_skill": "update_person_attr",
        "expected_action": None,
        "expected_field_or_scope": "is_weekend_off",
        "expected_params_subset": {},
        "sensitivity": "low",
        "category": "person_attr",
        "note": None,
    },
    {
        "query": "이영희 원티드 최대 5건으로",
        "expected_skill": "update_person_attr",
        "expected_action": None,
        "expected_field_or_scope": "wanted_max_requests",
        "expected_params_subset": {},
        "sensitivity": "low",
        "category": "person_attr",
        "note": None,
    },
    # ─────── C. 원티드 확정반영 (5) ───────
    {
        "query": "김민지 5월 15일 원티드 승인해줘",
        "expected_skill": "bulk_mutation",
        "expected_action": "approve",
        "expected_field_or_scope": "wanted_adjustment",
        "expected_params_subset": {"scope": "wanted_adjustment"},
        "sensitivity": "high",
        "category": "wanted_adjustment",
        "note": None,
    },
    {
        "query": "박혜미 5월 10일 원티드 거부",
        "expected_skill": "bulk_mutation",
        "expected_action": "reject",
        "expected_field_or_scope": "wanted_adjustment",
        "expected_params_subset": {"scope": "wanted_adjustment"},
        "sensitivity": "high",
        "category": "wanted_adjustment",
        "note": None,
    },
    {
        "query": "5월 원티드 마감일 5월 20일로 변경",
        "expected_skill": "bulk_mutation",
        "expected_action": "update_deadline",
        "expected_field_or_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions", "action": "update_deadline"},
        "sensitivity": "high",
        "category": "wanted_deadline",
        "note": None,
    },
    {
        "query": "김민지 5월 원티드 취소해줘",
        "expected_skill": "bulk_mutation",
        "expected_action": "cancel",
        "expected_field_or_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions", "action": "cancel"},
        "sensitivity": "high",
        "category": "wanted_cancel",
        "note": None,
    },
    {
        "query": "박혜미 5월 15일에 N 원티드 추가해줘",
        "expected_skill": "bulk_mutation",
        "expected_action": "add_shift",
        "expected_field_or_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions", "action": "add_shift"},
        "sensitivity": "high",
        "category": "wanted_add",
        "note": None,
    },
    # ─────── D. 원티드 일자 변경 (5) ───────
    {
        "query": "김민지 5월 10일 D를 N으로 변경",
        "expected_skill": "bulk_mutation",
        "expected_action": "change_shift",
        "expected_field_or_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions", "action": "change_shift"},
        "sensitivity": "high",
        "category": "wanted_modify",
        "note": None,
    },
    {
        "query": "박혜미 5월 5일 원티드 N으로 바꿔",
        "expected_skill": "bulk_mutation",
        "expected_action": "change_shift",
        "expected_field_or_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions", "action": "change_shift"},
        "sensitivity": "high",
        "category": "wanted_modify",
        "note": None,
    },
    {
        "query": "이영희 5월 20일 D 원티드 추가",
        "expected_skill": "bulk_mutation",
        "expected_action": "add_shift",
        "expected_field_or_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions", "action": "add_shift"},
        "sensitivity": "high",
        "category": "wanted_add",
        "note": None,
    },
    {
        "query": "5월 원티드 미승인된 거 모두 거부 처리",
        "expected_skill": "bulk_mutation",
        "expected_action": "reject",
        "expected_field_or_scope": "wanted_adjustment",
        "expected_params_subset": {"scope": "wanted_adjustment"},
        "sensitivity": "high",
        "category": "wanted_bulk",
        "note": None,
    },
    {
        "query": "김민지 5월 15일 D 원티드 취소",
        "expected_skill": "bulk_mutation",
        "expected_action": "cancel",
        "expected_field_or_scope": "wanted_submissions",
        "expected_params_subset": {"scope": "wanted_submissions"},
        "sensitivity": "high",
        "category": "wanted_cancel",
        "note": None,
    },
    # ─────── E. NurseMonthlyLimit — QA-8 에서 구현 (5) ───────
    {
        "query": "김민지 5월 N 4번으로 맞춰줘",
        "expected_skill": "update_monthly_limit",
        "expected_action": None,
        "expected_field_or_scope": "n_exact",
        "expected_params_subset": {"year": 2026, "month": 5},
        "sensitivity": "high",
        "category": "monthly_limit",
        "note": "update_monthly_limit skill — QA-8 에서 구현",
    },
    {
        "query": "박혜미 5월 D 최소 8회",
        "expected_skill": "update_monthly_limit",
        "expected_action": None,
        "expected_field_or_scope": "d_min",
        "expected_params_subset": {"year": 2026, "month": 5},
        "sensitivity": "high",
        "category": "monthly_limit",
        "note": "update_monthly_limit skill — QA-8",
    },
    {
        "query": "이영희 5월 N 최대 5회로 제한",
        "expected_skill": "update_monthly_limit",
        "expected_action": None,
        "expected_field_or_scope": "n_max",
        "expected_params_subset": {"year": 2026, "month": 5},
        "sensitivity": "high",
        "category": "monthly_limit",
        "note": "update_monthly_limit skill — QA-8",
    },
    {
        "query": "김지은 5월 N 한도 3",
        "expected_skill": "update_monthly_limit",
        "expected_action": None,
        "expected_field_or_scope": "n_exact",
        "expected_params_subset": {},
        "sensitivity": "high",
        "category": "monthly_limit",
        "note": "update_monthly_limit skill — QA-8",
    },
    {
        "query": "박춘일 5월 D 정확히 10번",
        "expected_skill": "update_monthly_limit",
        "expected_action": None,
        "expected_field_or_scope": "d_exact",
        "expected_params_subset": {"year": 2026, "month": 5},
        "sensitivity": "high",
        "category": "monthly_limit",
        "note": "update_monthly_limit skill — QA-8",
    },
    # ─────── F. ShiftManage 인원 조정 (3) ───────
    {
        "query": "RN 데이 1슬롯 인원 5명으로",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "manpower",
        "expected_params_subset": {"field": "manpower", "nurse_class": "RN", "shift_slot": 1},
        "sensitivity": "high",
        "category": "shift_manage",
        "note": None,
    },
    {
        "query": "AN 야간 2슬롯 4명으로",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "manpower",
        "expected_params_subset": {"field": "manpower", "nurse_class": "AN", "shift_slot": 2},
        "sensitivity": "high",
        "category": "shift_manage",
        "note": None,
    },
    {
        "query": "RN 이브닝 슬롯1 인원 3명",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "manpower",
        "expected_params_subset": {"field": "manpower", "nurse_class": "RN"},
        "sensitivity": "high",
        "category": "shift_manage",
        "note": None,
    },
    # ─────── G. 근무표 생성 (3) ───────
    {
        "query": "5월 근무표 생성해줘",
        "expected_skill": "generate_schedule",
        "expected_action": None,
        "expected_field_or_scope": None,
        "expected_params_subset": {"year": 2026, "month": 5},
        "sensitivity": "high",
        "category": "schedule_generate",
        "note": None,
    },
    {
        "query": "5월 근무표 다시 돌려줘",
        "expected_skill": "generate_schedule",
        "expected_action": None,
        "expected_field_or_scope": None,
        "expected_params_subset": {"year": 2026, "month": 5},
        "sensitivity": "high",
        "category": "schedule_generate",
        "note": None,
    },
    {
        "query": "6월 근무표 만들어줘",
        "expected_skill": "generate_schedule",
        "expected_action": None,
        "expected_field_or_scope": None,
        "expected_params_subset": {"year": 2026, "month": 6},
        "sensitivity": "high",
        "category": "schedule_generate",
        "note": None,
    },
    # ─────── H. 게이지 조정 (low sensitivity, 2) ───────
    {
        "query": "팀 밸런스 강도 7로",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "team_balance_gauge",
        "expected_params_subset": {"field": "team_balance_gauge", "value": 7},
        "sensitivity": "low",
        "category": "roster_config",
        "note": None,
    },
    {
        "query": "프리셉터 게이지 5로 조정",
        "expected_skill": "update_constraint",
        "expected_action": None,
        "expected_field_or_scope": "preceptor_gauge",
        "expected_params_subset": {"field": "preceptor_gauge", "value": 5},
        "sensitivity": "low",
        "category": "roster_config",
        "note": None,
    },
]


def get_high_sensitivity() -> list[dict]:
    return [q for q in MUTATION_QUERIES if q["sensitivity"] == "high"]


def get_low_sensitivity() -> list[dict]:
    return [q for q in MUTATION_QUERIES if q["sensitivity"] == "low"]


def get_queries_by_category(category: str) -> list[dict]:
    return [q for q in MUTATION_QUERIES if q["category"] == category]


def get_known_failures() -> list[dict]:
    return [q for q in MUTATION_QUERIES if q.get("note") and ("미구현" in q["note"] or "QA-8" in q["note"])]
