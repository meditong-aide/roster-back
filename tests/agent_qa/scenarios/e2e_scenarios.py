"""E2E 시나리오 코퍼스 — 수간호사 자연어 질의 → agent 예상 동작.

각 entry schema:
    id: str — 시나리오 식별자 (A1, B2, I3 등).
    category: str — 도메인 분류.
    complexity: 'simple' | 'compound'.
    query: str — 사용자 1차 입력.
    expected_action: str — **사람(수간호사/PO)이 읽고 검수**할 한국어 설명.
                          agent 가 어떤 skill 을 어떤 순서로, 어떤 답변을 내야 하는지 1-3 문장.
    turns: list[dict] — 자동화 가능한 turn 단위 검증 데이터.
    mode: 'read_only' | 'preview' | 'mutation'.
    permission: 'any' | 'hn_only'.
    notes: str | None.
"""

from __future__ import annotations

SCENARIOS: list[dict] = [
    # ════════════════════════════════════════════════════════════════
    # A. 설정 조회 (settings_read) — 5
    # ════════════════════════════════════════════════════════════════
    {
        "id": "A1",
        "category": "settings_read",
        "complexity": "simple",
        "query": "5월 야간 최대 횟수 설정 보여줘",
        "expected_action": (
            "query_schedule(scope=constraint_config) 호출. RosterConfig 의 max_nig_per_month "
            "값을 한국어로 답변. 예: '현재 야간 최대 7회로 설정되어 있습니다.'"
        ),
        "turns": [{
            "user": "5월 야간 최대 횟수 설정 보여줘",
            "expected_skill_calls": [
                {"name": "query_schedule", "args_subset": {"scope": "constraint_config"}}
            ],
            "expected_answer_contains": ["야간", "max_nig"],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "A2",
        "category": "settings_read",
        "complexity": "simple",
        "query": "팀 밸런스 켜져 있어?",
        "expected_action": (
            "query_schedule(scope=constraint_config) 호출. team_balance_enable 값 (bool) 을 "
            "'네/아니오' 자연어로 답변."
        ),
        "turns": [{
            "user": "팀 밸런스 켜져 있어?",
            "expected_skill_calls": [{"name": "query_schedule", "args_subset": {"scope": "constraint_config"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "A3",
        "category": "settings_read",
        "complexity": "simple",
        "query": "데이 필요인원 지금 몇 명이지?",
        "expected_action": (
            "query_schedule(scope=constraint_config) 호출. day_req 값 자연어 답변. "
            "예: '데이 시프트 필요인원은 4명입니다.'"
        ),
        "turns": [{
            "user": "데이 필요인원 지금 몇 명이지?",
            "expected_skill_calls": [{"name": "query_schedule", "args_subset": {"scope": "constraint_config"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "A4",
        "category": "settings_read",
        "complexity": "simple",
        "query": "주2회 오프 보장 설정 어떻게 되어있어?",
        "expected_action": (
            "query_schedule(scope=constraint_config) 호출. two_offs_per_week (bool) 값 응답."
        ),
        "turns": [{
            "user": "주2회 오프 보장 설정 어떻게 되어있어?",
            "expected_skill_calls": [{"name": "query_schedule", "args_subset": {"scope": "constraint_config"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "A5",
        "category": "settings_read",
        "complexity": "simple",
        "query": "스케줄 정책 전체 보여줘",
        "expected_action": (
            "query_schedule(scope=constraint_config) 호출. RosterConfig 의 30+ 필드 전체를 "
            "그룹별 (시프트별 필요인원 / 연속·휴무 / 구조정책 / 표시옵션) 자연어로 요약."
        ),
        "turns": [{
            "user": "스케줄 정책 전체 보여줘",
            "expected_skill_calls": [{"name": "query_schedule", "args_subset": {"scope": "constraint_config"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },

    # ════════════════════════════════════════════════════════════════
    # B. 설정 변경 (settings_update) — 6
    # ════════════════════════════════════════════════════════════════
    {
        "id": "B1",
        "category": "settings_update",
        "complexity": "simple",
        "query": "야간 최대 7회로 바꿔줘",
        "expected_action": (
            "1) update_constraint(field=max_nig_per_month, value=7, preview_only=True) 호출. "
            "2) preview 결과 (old/new) 안내 후 사용자 confirm 대기. "
            "3) 사용자 '네' → preview_only=False 로 재호출, 변경 완료 안내."
        ),
        "turns": [
            {
                "user": "야간 최대 7회로 바꿔줘",
                "expected_skill_calls": [{"name": "update_constraint",
                    "args_subset": {"field": "max_nig_per_month", "value": 7, "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
            {
                "user": "네",
                "expected_skill_calls": [{"name": "update_constraint",
                    "args_subset": {"field": "max_nig_per_month", "value": 7, "preview_only": False}}],
                "expected_answer_contains": ["변경", "완료"],
            },
        ],
        "mode": "mutation", "permission": "hn_only", "notes": "표준 preview→confirm 흐름.",
    },
    {
        "id": "B2",
        "category": "settings_update",
        "complexity": "simple",
        "query": "팀 밸런스 켜줘",
        "expected_action": (
            "update_constraint(field=team_balance_enable, value=true, preview_only=True). "
            "현재 false → true 변경 안내 후 confirm."
        ),
        "turns": [{
            "user": "팀 밸런스 켜줘",
            "expected_skill_calls": [{"name": "update_constraint",
                "args_subset": {"field": "team_balance_enable", "value": True, "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "B3",
        "category": "settings_update",
        "complexity": "simple",
        "query": "연속 근무 최대 5일로 제한",
        "expected_action": (
            "update_constraint(field=max_conseq_work, value=5, preview_only=True). "
            "사용자 confirm 후 적용."
        ),
        "turns": [{
            "user": "연속 근무 최대 5일로 제한",
            "expected_skill_calls": [{"name": "update_constraint",
                "args_subset": {"field": "max_conseq_work", "value": 5, "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "B4",
        "category": "settings_update",
        "complexity": "simple",
        "query": "야간 늘려줘",
        "expected_action": (
            "모호한 query — agent 가 clarify 발화. "
            "'야간 최대 횟수(max_nig_per_month)와 야간 필요인원(nig_req) 중 어느 쪽을 의미하시나요?' "
            "사용자 답변에 따라 update_constraint 적절한 field 로 호출."
        ),
        "turns": [
            {
                "user": "야간 늘려줘",
                "expected_clarification": True,
                "expected_skill_calls": [],
            },
            {
                "user": "월 최대 7회로",
                "expected_skill_calls": [{"name": "update_constraint",
                    "args_subset": {"field": "max_nig_per_month", "value": 7, "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
        ],
        "mode": "preview", "permission": "hn_only",
        "notes": "필드 모호성 clarify 흐름.",
    },
    {
        "id": "B5",
        "category": "settings_update",
        "complexity": "simple",
        "query": "이브닝 다음날 데이 금지 해제해줘",
        "expected_action": (
            "update_constraint(field=banned_day_after_eve, value=false, preview_only=True). "
            "기존이 true 라면 false 로 전환 안내."
        ),
        "turns": [{
            "user": "이브닝 다음날 데이 금지 해제해줘",
            "expected_skill_calls": [{"name": "update_constraint",
                "args_subset": {"field": "banned_day_after_eve", "value": False, "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "B6",
        "category": "settings_update",
        "complexity": "simple",
        "query": "프리셉터 게이지 7로 조정",
        "expected_action": (
            "update_constraint(field=preceptor_gauge, value=7, preview_only=True). "
            "게이지(0~10) 범위 검증 + confirm."
        ),
        "turns": [{
            "user": "프리셉터 게이지 7로 조정",
            "expected_skill_calls": [{"name": "update_constraint",
                "args_subset": {"field": "preceptor_gauge", "value": 7, "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },

    # ════════════════════════════════════════════════════════════════
    # C. 원티드 설정 (wanted_config) — 5
    # ════════════════════════════════════════════════════════════════
    {
        "id": "C1",
        "category": "wanted_config",
        "complexity": "simple",
        "query": "5월 원티드 마감일 5월 25일로 변경",
        "expected_action": (
            "bulk_mutation(scope=wanted_submissions, action=update_deadline, new_deadline='2026-05-25', preview_only=True). "
            "기존 마감일 vs 새 마감일 비교 안내 후 confirm."
        ),
        "turns": [{
            "user": "5월 원티드 마감일 5월 25일로 변경",
            "expected_skill_calls": [{"name": "bulk_mutation",
                "args_subset": {"scope": "wanted_submissions", "action": "update_deadline", "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "C2",
        "category": "wanted_config",
        "complexity": "simple",
        "query": "5월 원티드 마감일 언제야?",
        "expected_action": (
            "query_schedule(scope=wanted_config) 가 이상적이지만 **현재 미구현**. "
            "fallback 으로 wanted_submissions scope 로 라우팅되어 마감일 정보 응답이 가능할 수도. "
            "향후 wanted_config scope 추가 필요."
        ),
        "turns": [{
            "user": "5월 원티드 마감일 언제야?",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "wanted_config"}}],
        }],
        "mode": "read_only", "permission": "any",
        "notes": "wanted_config scope 미구현 — known gap (별도 PR).",
    },
    {
        "id": "C3",
        "category": "wanted_config",
        "complexity": "simple",
        "query": "원티드 AIDE 기능 켜져 있나?",
        "expected_action": (
            "query_schedule(scope=wanted_config) 호출 후 enable_aide 값 응답. "
            "현재 wanted_config scope 미구현 → known gap."
        ),
        "turns": [{
            "user": "원티드 AIDE 기능 켜져 있나?",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "wanted_config"}}],
        }],
        "mode": "read_only", "permission": "any",
        "notes": "wanted_config scope 미구현.",
    },
    {
        "id": "C4",
        "category": "wanted_config",
        "complexity": "simple",
        "query": "원티드 한도 설정 어떻게 되어 있어?",
        "expected_action": (
            "모호한 query — '병동 전체 한도'인지 '개인별 한도'인지 clarify 권장. "
            "사용자 답변에 따라:\n"
            "  - 병동 전체 → wanted_config (현재 미구현)\n"
            "  - 특정 nurse → query_schedule(scope=nurse_info) + wanted_max_requests 값"
        ),
        "turns": [{
            "user": "원티드 한도 설정 어떻게 되어 있어?",
            "expected_clarification": True,
            "expected_skill_calls": [],
        }],
        "mode": "read_only", "permission": "any",
        "notes": "scope 모호성 — clarify 기대.",
    },
    {
        "id": "C5",
        "category": "wanted_config",
        "complexity": "simple",
        "query": "원티드 작성 가능 기간 알려줘",
        "expected_action": (
            "query_schedule(scope=wanted_config) 호출 → 마감일 + 작성 시작일 응답. "
            "현재 wanted_config scope 미구현 → known gap."
        ),
        "turns": [{
            "user": "원티드 작성 가능 기간 알려줘",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "wanted_config"}}],
        }],
        "mode": "read_only", "permission": "any",
        "notes": "wanted_config scope 미구현.",
    },

    # ════════════════════════════════════════════════════════════════
    # D. 원티드 확정반영 (wanted_adjustment) — 5
    # ════════════════════════════════════════════════════════════════
    {
        "id": "D1",
        "category": "wanted_adjustment",
        "complexity": "simple",
        "query": "김민지 5월 15일 원티드 승인해줘",
        "expected_action": (
            "1) bulk_mutation(scope=wanted_adjustment, preview_only=True) — 김민지(nurse_id grounding) 5/15 항목 표시. "
            "2) 사용자 confirm → FixedWantedEntry approve 반영 (preview_only=False)."
        ),
        "turns": [
            {
                "user": "김민지 5월 15일 원티드 승인해줘",
                "expected_skill_calls": [{"name": "bulk_mutation",
                    "args_subset": {"scope": "wanted_adjustment", "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
            {
                "user": "네",
                "expected_skill_calls": [{"name": "bulk_mutation",
                    "args_subset": {"scope": "wanted_adjustment", "preview_only": False}}],
            },
        ],
        "mode": "mutation", "permission": "hn_only", "notes": None,
    },
    {
        "id": "D2",
        "category": "wanted_adjustment",
        "complexity": "simple",
        "query": "박혜미 5월 10일 원티드 거부",
        "expected_action": (
            "bulk_mutation(scope=wanted_adjustment, preview_only=True). "
            "박혜미 5/10 항목 거부 preview → confirm 후 거부 처리."
        ),
        "turns": [{
            "user": "박혜미 5월 10일 원티드 거부",
            "expected_skill_calls": [{"name": "bulk_mutation",
                "args_subset": {"scope": "wanted_adjustment", "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "D3",
        "category": "wanted_adjustment",
        "complexity": "simple",
        "query": "김민지 5월 15일 D를 N으로 변경 후 승인",
        "expected_action": (
            "bulk_mutation(scope=wanted_submissions, action=change_shift, preview_only=True). "
            "원본 D 항목을 N 으로 modify + 승인 flag 함께 적용 preview."
        ),
        "turns": [{
            "user": "김민지 5월 15일 D를 N으로 변경 후 승인",
            "expected_skill_calls": [{"name": "bulk_mutation",
                "args_subset": {"scope": "wanted_submissions", "action": "change_shift", "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "D4",
        "category": "wanted_adjustment",
        "complexity": "simple",
        "query": "이번 달 미승인된 원티드 전부 승인 처리",
        "expected_action": (
            "1) 안전 확인 — bulk approve 는 영향 큼. agent 가 영향 nurse 수와 항목 수 명시. "
            "2) bulk_mutation(scope=wanted_adjustment, preview_only=True) — 모든 미승인 항목 표시. "
            "3) 사용자 confirm 후 일괄 승인."
        ),
        "turns": [{
            "user": "이번 달 미승인된 원티드 전부 승인 처리",
            "expected_skill_calls": [{"name": "bulk_mutation",
                "args_subset": {"scope": "wanted_adjustment", "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only",
        "notes": "대규모 영향 — preview 시 영향 범위 명시 필수.",
    },
    {
        "id": "D5",
        "category": "wanted_adjustment",
        "complexity": "simple",
        "query": "박혜미 5월 5일 원티드 삭제",
        "expected_action": (
            "bulk_mutation(scope=wanted_submissions, action=cancel, preview_only=True). "
            "박혜미 5/5 단일 날짜 항목 삭제 preview."
        ),
        "turns": [{
            "user": "박혜미 5월 5일 원티드 삭제",
            "expected_skill_calls": [{"name": "bulk_mutation",
                "args_subset": {"scope": "wanted_submissions", "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },

    # ════════════════════════════════════════════════════════════════
    # E. 원티드 조회 (wanted_read) — 5
    # ════════════════════════════════════════════════════════════════
    {
        "id": "E1",
        "category": "wanted_read",
        "complexity": "simple",
        "query": "5월 원티드 미제출자 누구야?",
        "expected_action": (
            "query_schedule(scope=wanted_submissions, operation=count, year=2026, month=5). "
            "응답: 제출/미제출 카운트 + 미제출 nurse 이름 리스트."
        ),
        "turns": [{
            "user": "5월 원티드 미제출자 누구야?",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "wanted_submissions", "operation": "count", "year": 2026, "month": 5}}],
            "expected_answer_contains": ["미제출"],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "E2",
        "category": "wanted_read",
        "complexity": "simple",
        "query": "김민지 5월 원티드 신청 내용",
        "expected_action": (
            "query_schedule(scope=wanted_submissions, nurse_name='김민지'). "
            "응답: 김민지의 5월 wanted 항목 (날짜, 시프트, 사유)."
        ),
        "turns": [{
            "user": "김민지 5월 원티드 신청 내용",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "wanted_submissions"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "E3",
        "category": "wanted_read",
        "complexity": "simple",
        "query": "5월에 N 원티드 신청한 사람",
        "expected_action": (
            "query_schedule(scope=wanted_submissions, shift_name='나이트', month=5). "
            "응답: N 신청자 nurse 리스트 + 신청 날짜."
        ),
        "turns": [{
            "user": "5월에 N 원티드 신청한 사람",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "wanted_submissions"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "E4",
        "category": "wanted_read",
        "complexity": "simple",
        "query": "이번 달 원티드 변경 내역 보여줘",
        "expected_action": (
            "query_schedule(scope=wanted_adjustment, year=2026, month=5). "
            "응답: FixedWantedEntry 의 source_type 별 변경 (original/modified/added) 내역."
        ),
        "turns": [{
            "user": "이번 달 원티드 변경 내역 보여줘",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "wanted_adjustment"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "E5",
        "category": "wanted_read",
        "complexity": "simple",
        "query": "5월 15일 원티드 신청자 누구야?",
        "expected_action": (
            "query_schedule(scope=wanted_submissions, date='2026-05-15'). "
            "응답: 5/15 에 어떤 nurse 가 어떤 시프트로 신청했는지."
        ),
        "turns": [{
            "user": "5월 15일 원티드 신청자 누구야?",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "wanted_submissions"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },

    # ════════════════════════════════════════════════════════════════
    # F. 팀 조정 (team) — 4
    # ════════════════════════════════════════════════════════════════
    {
        "id": "F1",
        "category": "team",
        "complexity": "simple",
        "query": "박혜미 1팀으로 이동",
        "expected_action": (
            "1) update_person_attr(mutations=[{field=team_id, value=1}], preview_only=True). "
            "2) **preceptor 관계 충돌 검증**: 박혜미의 preceptor 가 다른 팀이면 추가 confirmation 요청. "
            "3) confirm 후 적용."
        ),
        "turns": [{
            "user": "박혜미 1팀으로 이동",
            "expected_skill_calls": [{"name": "update_person_attr",
                "args_subset": {"preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only",
        "notes": "team_id 변경은 preceptor consistency check 추가 점검 대상.",
    },
    {
        "id": "F2",
        "category": "team",
        "complexity": "simple",
        "query": "1팀 간호사 목록 알려줘",
        "expected_action": (
            "query_schedule(scope=nurse_info, team_id=1). "
            "응답: 1팀 소속 nurse 이름·grade·역할 목록."
        ),
        "turns": [{
            "user": "1팀 간호사 목록 알려줘",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "nurse_info"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "F3",
        "category": "team",
        "complexity": "simple",
        "query": "팀별 야간 배분 분석",
        "expected_action": (
            "analyze_report(scope=schedule, group_by='team', shift='나이트'). "
            "응답: 1팀/2팀별 N 시프트 분포 + variance 지표."
        ),
        "turns": [{
            "user": "팀별 야간 배분 분석",
            "expected_skill_calls": [{"name": "analyze_report", "args_subset": {}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "F4",
        "category": "team",
        "complexity": "simple",
        "query": "3팀 새로 만들어줘",
        "expected_action": (
            "**현재 Team CRUD skill 없음 — known gap**. agent 가 명확히 '미지원' 안내하고 "
            "수동 등록 방법 가이드 (관리자 화면 등). 또는 향후 manage_team skill 추가."
        ),
        "turns": [{
            "user": "3팀 새로 만들어줘",
            "expected_skill_calls": [],
        }],
        "mode": "read_only", "permission": "any",
        "notes": "Team CRUD 미지원 — known gap. 응답만 가능하므로 permission=any (실 mutation 시 hn_only 게이트 추가 예정).",
    },

    # ════════════════════════════════════════════════════════════════
    # G. 그레이드 조정 (grade) — 3
    # ════════════════════════════════════════════════════════════════
    {
        "id": "G1",
        "category": "grade",
        "complexity": "simple",
        "query": "김지은 그레이드 3으로",
        "expected_action": (
            "update_person_attr(mutations=[{field=grade, value=3}], preview_only=True). "
            "기존 grade vs 3 비교 후 confirm."
        ),
        "turns": [{
            "user": "김지은 그레이드 3으로",
            "expected_skill_calls": [{"name": "update_person_attr",
                "args_subset": {"preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "G2",
        "category": "grade",
        "complexity": "simple",
        "query": "그레이드별 분포 보여줘",
        "expected_action": (
            "analyze_report(scope=nurse_info, group_by='grade') 또는 "
            "query_schedule(scope=nurse_info) + 클라이언트 측 집계. "
            "응답: grade 1/2/3/... 별 인원 카운트."
        ),
        "turns": [{
            "user": "그레이드별 분포 보여줘",
            "expected_skill_calls": [{"name": "analyze_report", "args_subset": {}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "G3",
        "category": "grade",
        "complexity": "simple",
        "query": "최소 경력자 시프트당 2명 이상으로",
        "expected_action": (
            "update_constraint(field=min_exp_per_shift, value=2, preview_only=True). "
            "병동 정책 — 모든 시프트에 grade ≥2 인 nurse 최소 2명 필수. confirm 후 적용."
        ),
        "turns": [{
            "user": "최소 경력자 시프트당 2명 이상으로",
            "expected_skill_calls": [{"name": "update_constraint",
                "args_subset": {"field": "min_exp_per_shift", "value": 2, "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },

    # ════════════════════════════════════════════════════════════════
    # H. 개인별 N 개수 (monthly_limit) — 5
    # ════════════════════════════════════════════════════════════════
    {
        "id": "H1",
        "category": "monthly_limit",
        "complexity": "simple",
        "query": "김민지 5월 N 4번으로 맞춰줘",
        "expected_action": (
            "1) update_monthly_limit(nurse_ids=[김민지 nurse_id], year=2026, month=5, n_exact=4, preview_only=True). "
            "2) confirm 후 NurseMonthlyLimit upsert."
        ),
        "turns": [
            {
                "user": "김민지 5월 N 4번으로 맞춰줘",
                "expected_skill_calls": [{"name": "update_monthly_limit",
                    "args_subset": {"year": 2026, "month": 5, "n_exact": 4, "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
            {
                "user": "네",
                "expected_skill_calls": [{"name": "update_monthly_limit",
                    "args_subset": {"preview_only": False}}],
            },
        ],
        "mode": "mutation", "permission": "hn_only", "notes": None,
    },
    {
        "id": "H2",
        "category": "monthly_limit",
        "complexity": "simple",
        "query": "박혜미 5월 D 최소 8회",
        "expected_action": (
            "update_monthly_limit(d_min=8, preview_only=True). confirm 후 적용."
        ),
        "turns": [{
            "user": "박혜미 5월 D 최소 8회",
            "expected_skill_calls": [{"name": "update_monthly_limit",
                "args_subset": {"d_min": 8, "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "H3",
        "category": "monthly_limit",
        "complexity": "simple",
        "query": "이영희 5월 N 최대 5회로 제한",
        "expected_action": (
            "update_monthly_limit(n_max=5, preview_only=True). confirm 후 적용."
        ),
        "turns": [{
            "user": "이영희 5월 N 최대 5회로 제한",
            "expected_skill_calls": [{"name": "update_monthly_limit",
                "args_subset": {"n_max": 5, "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "H4",
        "category": "monthly_limit",
        "complexity": "simple",
        "query": "김민지 5월 N 몇 번 설정?",
        "expected_action": (
            "query_schedule(scope=monthly_limit, nurse_name='김민지', year=2026, month=5). "
            "응답: n_exact / n_min / n_max 중 설정된 값 (또는 미설정 안내)."
        ),
        "turns": [{
            "user": "김민지 5월 N 몇 번 설정?",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "monthly_limit"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "H5",
        "category": "monthly_limit",
        "complexity": "simple",
        "query": "5월 한도 설정된 간호사 전체 보여줘",
        "expected_action": (
            "query_schedule(scope=monthly_limit, year=2026, month=5, nurse_ids=None). "
            "응답: 한도가 설정된 모든 nurse 와 그 12 필드 (d/e/n/o × min/max/exact) 표시."
        ),
        "turns": [{
            "user": "5월 한도 설정된 간호사 전체 보여줘",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "monthly_limit"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },

    # ════════════════════════════════════════════════════════════════
    # I. 복합 질의 (compound) — 8
    # ════════════════════════════════════════════════════════════════
    {
        "id": "I1",
        "category": "compound",
        "complexity": "compound",
        "query": "박혜미 5월 10일 원티드 취소하고 김민지를 대신 배치",
        "expected_action": (
            "Turn1: bulk_mutation(cancel, preview_only=True) — 박혜미 5/10 취소 preview. "
            "Turn2 (confirm): cancel 실행 + recommend_candidates(date=2026-05-10) 호출 — 가능 대체자 목록. "
            "Turn3 (사용자가 김민지 지정): bulk_mutation(add_shift, nurse=김민지, preview_only=True). "
            "Turn4 (confirm): add_shift 실행."
        ),
        "turns": [
            {
                "user": "박혜미 5월 10일 원티드 취소하고 김민지를 대신 배치",
                "expected_skill_calls": [{"name": "bulk_mutation",
                    "args_subset": {"scope": "wanted_submissions", "action": "cancel", "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
            {
                "user": "네",
                "expected_skill_calls": [
                    {"name": "bulk_mutation", "args_subset": {"preview_only": False}},
                    {"name": "recommend_candidates", "args_subset": {}},
                ],
            },
        ],
        "mode": "mutation", "permission": "hn_only", "notes": "4-turn compound.",
    },
    {
        "id": "I2",
        "category": "compound",
        "complexity": "compound",
        "query": "5월 야간 분포 보고 균형 안 맞으면 max_nig 조정해줘",
        "expected_action": (
            "Turn1: analyze_report(shift=N) — 분포 수치 응답 + variance 평가. "
            "Turn2 (사용자 판단): update_constraint(max_nig_per_month, preview_only=True). "
            "Turn3 (confirm): 적용."
        ),
        "turns": [
            {
                "user": "5월 야간 분포 보고 균형 안 맞으면 max_nig 조정해줘",
                "expected_skill_calls": [{"name": "analyze_report", "args_subset": {}}],
            },
            {
                "user": "맞아 조정해줘 6회로",
                "expected_skill_calls": [{"name": "update_constraint",
                    "args_subset": {"field": "max_nig_per_month", "value": 6, "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
        ],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "I3",
        "category": "compound",
        "complexity": "compound",
        "query": "김민지 5월 N 4번으로 맞추고 근무표 다시 돌려",
        "expected_action": (
            "Turn1: update_monthly_limit(n_exact=4, preview_only=True). "
            "Turn2 (confirm): update_monthly_limit(preview_only=False) + generate_schedule(preview_only=True). "
            "Turn3 (confirm): generate_schedule(preview_only=False) → 잡 등록 안내."
        ),
        "turns": [
            {
                "user": "김민지 5월 N 4번으로 맞추고 근무표 다시 돌려",
                "expected_skill_calls": [{"name": "update_monthly_limit",
                    "args_subset": {"n_exact": 4, "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
            {
                "user": "네",
                "expected_skill_calls": [
                    {"name": "update_monthly_limit", "args_subset": {"preview_only": False}},
                    {"name": "generate_schedule", "args_subset": {"preview_only": True}},
                ],
                "expected_awaiting_approval": True,
            },
        ],
        "mode": "mutation", "permission": "hn_only", "notes": "3-turn compound.",
    },
    {
        "id": "I4",
        "category": "compound",
        "complexity": "compound",
        "query": "5월 미제출자 확인하고 그 사람들 마감일 5월 25일까지 연장",
        "expected_action": (
            "Turn1: query_schedule(wanted_submissions, count). 미제출자 명단 응답. "
            "Turn2 (사용자 확인): bulk_mutation(update_deadline, new_deadline=2026-05-25, preview_only=True). "
            "Turn3 (confirm): 적용."
        ),
        "turns": [
            {
                "user": "5월 미제출자 확인하고 그 사람들 마감일 5월 25일까지 연장",
                "expected_skill_calls": [{"name": "query_schedule",
                    "args_subset": {"scope": "wanted_submissions", "operation": "count"}}],
            },
            {
                "user": "네 연장해",
                "expected_skill_calls": [{"name": "bulk_mutation",
                    "args_subset": {"scope": "wanted_submissions", "action": "update_deadline", "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
        ],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "I5",
        "category": "compound",
        "complexity": "compound",
        "query": "팀 밸런스 켜고 5월 근무표 다시 만들어",
        "expected_action": (
            "Turn1: update_constraint(team_balance_enable=True, preview_only=True). "
            "Turn2 (confirm): update_constraint(False→True) + generate_schedule(preview_only=True). "
            "Turn3 (confirm): generate_schedule 실행."
        ),
        "turns": [
            {
                "user": "팀 밸런스 켜고 5월 근무표 다시 만들어",
                "expected_skill_calls": [{"name": "update_constraint",
                    "args_subset": {"field": "team_balance_enable", "value": True, "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
            {
                "user": "네",
                "expected_skill_calls": [
                    {"name": "update_constraint", "args_subset": {"preview_only": False}},
                    {"name": "generate_schedule", "args_subset": {"preview_only": True}},
                ],
                "expected_awaiting_approval": True,
            },
        ],
        "mode": "mutation", "permission": "hn_only",
        "notes": "가장 흔한 운영 시나리오: 정책 변경 + 재생성.",
    },
    {
        "id": "I6",
        "category": "compound",
        "complexity": "compound",
        "query": "박혜미 1팀으로 옮기고 김민지를 2팀으로",
        "expected_action": (
            "Turn1: update_person_attr(박혜미, team_id=1, preview_only=True). "
            "Turn2 (confirm): 박혜미 적용 + update_person_attr(김민지, team_id=2, preview_only=True). "
            "Turn3 (confirm): 김민지 적용. "
            "각 단계에서 preceptor consistency check 통과 확인."
        ),
        "turns": [
            {
                "user": "박혜미 1팀으로 옮기고 김민지를 2팀으로",
                "expected_skill_calls": [{"name": "update_person_attr",
                    "args_subset": {"preview_only": True}}],
                "expected_awaiting_approval": True,
            },
            {
                "user": "네",
                "expected_skill_calls": [
                    {"name": "update_person_attr", "args_subset": {"preview_only": False}},
                    {"name": "update_person_attr", "args_subset": {"preview_only": True}},
                ],
                "expected_awaiting_approval": True,
            },
        ],
        "mode": "mutation", "permission": "hn_only", "notes": None,
    },
    {
        "id": "I7",
        "category": "compound",
        "complexity": "compound",
        "query": "5월 근무표 위반사항 보여주고 자동으로 고쳐줘",
        "expected_action": (
            "Turn1: validate_schedule — 4종 violation (consecutive_night/max_conseq_work/grade_coverage/night_variance) 진단. "
            "Turn2 (사용자 동의): repair_schedule — 자동 수정 제안 (swap pair, 추가 인원 등). "
            "Turn3 (각 제안 confirm): 개별 swap 적용."
        ),
        "turns": [
            {
                "user": "5월 근무표 위반사항 보여주고 자동으로 고쳐줘",
                "expected_skill_calls": [{"name": "validate_schedule", "args_subset": {}}],
            },
            {
                "user": "고쳐줘",
                "expected_skill_calls": [{"name": "repair_schedule", "args_subset": {}}],
            },
        ],
        "mode": "preview", "permission": "any",
        "notes": "repair 제안의 실 적용은 추가 turn 의 mutation confirm 필요.",
    },
    {
        "id": "I8",
        "category": "compound",
        "complexity": "compound",
        "query": "5월 야간 분포 분석하고 인원 부족하면 박혜미 5월 N 한도 늘려",
        "expected_action": (
            "Turn1: analyze_report — 야간 분포 응답. "
            "Turn2 (사용자 결정): update_monthly_limit(박혜미, n_max=X, preview_only=True). "
            "Turn3 (confirm): 적용."
        ),
        "turns": [
            {
                "user": "5월 야간 분포 분석하고 인원 부족하면 박혜미 5월 N 한도 늘려",
                "expected_skill_calls": [{"name": "analyze_report", "args_subset": {}}],
            },
            {
                "user": "맞아 6회로",
                "expected_skill_calls": [{"name": "update_monthly_limit",
                    "args_subset": {"n_max": 6, "preview_only": True}}],
                "expected_awaiting_approval": True,
            },
        ],
        "mode": "preview", "permission": "hn_only",
        "notes": "analyze → 개인 한도 조정 흐름.",
    },

    # ════════════════════════════════════════════════════════════════
    # J. ShiftManage (slot manpower) — 3
    # ════════════════════════════════════════════════════════════════
    {
        "id": "J1",
        "category": "shift_manage",
        "complexity": "simple",
        "query": "RN 데이 1슬롯 인원 5명으로",
        "expected_action": (
            "update_constraint(field=manpower, nurse_class='RN', shift_slot=1, value=5, preview_only=True). "
            "ShiftManage 단일 슬롯 인원 변경 preview."
        ),
        "turns": [{
            "user": "RN 데이 1슬롯 인원 5명으로",
            "expected_skill_calls": [{"name": "update_constraint",
                "args_subset": {"field": "manpower", "nurse_class": "RN", "shift_slot": 1, "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "J2",
        "category": "shift_manage",
        "complexity": "simple",
        "query": "AN 야간 슬롯 인원 어떻게 설정되어있어?",
        "expected_action": (
            "query_schedule(scope=constraint_config) 가 fallback 또는 별도 shift_manage scope 가 이상적. "
            "현재 query_schedule 에 shift_manage 전용 scope 없음 — known minor gap. "
            "응답: ShiftManage 의 nurse_class='AN' & shift_slot 별 manpower 값."
        ),
        "turns": [{
            "user": "AN 야간 슬롯 인원 어떻게 설정되어있어?",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "constraint_config"}}],
        }],
        "mode": "read_only", "permission": "any",
        "notes": "shift_manage 전용 scope 없음 — fallback 사용 가능성.",
    },
    {
        "id": "J3",
        "category": "shift_manage",
        "complexity": "simple",
        "query": "RN 이브닝 슬롯1 인원 3명",
        "expected_action": (
            "update_constraint(field=manpower, nurse_class='RN', shift_slot=1, value=3, preview_only=True). "
            "shift_slot=1 이 이브닝인지 nurse_class 별 슬롯 매핑 확인 필요 (agent 가 grounding)."
        ),
        "turns": [{
            "user": "RN 이브닝 슬롯1 인원 3명",
            "expected_skill_calls": [{"name": "update_constraint",
                "args_subset": {"field": "manpower", "nurse_class": "RN", "preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },

    # ════════════════════════════════════════════════════════════════
    # K. 권한 거절 (permission_denial) — 4 — user_role='nurse'
    # ════════════════════════════════════════════════════════════════
    {
        "id": "K1",
        "category": "permission_denial",
        "complexity": "simple",
        "query": "야간 최대 7회로 바꿔줘",  # 일반 nurse 가 시도
        "expected_action": (
            "사용자 role='nurse' 인 경우 update_constraint 는 병동 전체 정책 변경 → permission_denied. "
            "agent 응답: '병동 전체 설정 변경은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다.'"
        ),
        "turns": [{
            "user": "야간 최대 7회로 바꿔줘",
            "expected_skill_calls": [{"name": "update_constraint", "args_subset": {}}],
            "expected_answer_contains": ["수간호사", "권한"],
        }],
        "mode": "preview", "permission": "hn_only",
        "notes": "session.user_role='nurse' 일 때만 거절. middleware._check_permission 가 차단.",
    },
    {
        "id": "K2",
        "category": "permission_denial",
        "complexity": "simple",
        "query": "박혜미 그레이드 3으로",  # 일반 nurse 가 타인 변경 시도
        "expected_action": (
            "role='nurse' 가 본인이 아닌 nurse_name 변경 → permission_denied. "
            "agent 응답: '다른 간호사(박혜미)의 데이터를 수정할 권한이 없습니다.'"
        ),
        "turns": [{
            "user": "박혜미 그레이드 3으로",
            "expected_skill_calls": [{"name": "update_person_attr", "args_subset": {}}],
            "expected_answer_contains": ["권한"],
        }],
        "mode": "preview", "permission": "hn_only",
        "notes": "nurse 본인 변경은 허용 (테스트 시 nurse_name 매핑 확인).",
    },
    {
        "id": "K3",
        "category": "permission_denial",
        "complexity": "simple",
        "query": "나 원티드 최대 5건으로",  # 일반 nurse 의 본인 변경 — 허용
        "expected_action": (
            "role='nurse' 가 본인('나') 의 wanted_max_requests 변경 → 허용. "
            "update_person_attr(preview_only=True) → confirm 후 적용."
        ),
        "turns": [{
            "user": "나 원티드 최대 5건으로",
            "expected_skill_calls": [{"name": "update_person_attr",
                "args_subset": {"preview_only": True}}],
            "expected_awaiting_approval": True,
        }],
        "mode": "preview", "permission": "any",
        "notes": "본인 변경은 일반 nurse 도 허용.",
    },
    {
        "id": "K4",
        "category": "permission_denial",
        "complexity": "simple",
        "query": "5월 원티드 마감일 5월 25일로",  # 일반 nurse 가 마감일 변경 시도
        "expected_action": (
            "role='nurse' 가 wanted_submissions update_deadline → ward-wide action → permission_denied. "
            "agent 응답: '원티드 마감일 변경은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다.'"
        ),
        "turns": [{
            "user": "5월 원티드 마감일 5월 25일로",
            "expected_skill_calls": [{"name": "bulk_mutation", "args_subset": {}}],
            "expected_answer_contains": ["수간호사", "권한"],
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },

    # ════════════════════════════════════════════════════════════════
    # L. Edge case / 오류 처리 (edge_case) — 4
    # ════════════════════════════════════════════════════════════════
    {
        "id": "L1",
        "category": "edge_case",
        "complexity": "simple",
        "query": "없는간호사 5월 원티드 보여줘",
        "expected_action": (
            "query_schedule(scope=wanted_submissions, nurse_name='없는간호사'). "
            "grounding 단계에서 nurse_name → nurse_id 해석 실패 → clarification 또는 error. "
            "agent 응답: '해당 이름의 간호사를 찾을 수 없습니다. 정확한 이름을 알려주세요.'"
        ),
        "turns": [{
            "user": "없는간호사 5월 원티드 보여줘",
            "expected_skill_calls": [{"name": "query_schedule",
                "args_subset": {"scope": "wanted_submissions"}}],
            "expected_answer_contains": ["찾을 수 없"],
        }],
        "mode": "read_only", "permission": "any",
        "notes": "grounding 실패 → 사용자에게 명확화 요구.",
    },
    {
        "id": "L2",
        "category": "edge_case",
        "complexity": "simple",
        "query": "13월 근무표 생성해줘",
        "expected_action": (
            "agent 가 사용자 입력의 month=13 을 invalid 로 판단. "
            "skill 호출 전 clarification 또는 에러 응답. "
            "응답 예: '13월은 유효하지 않은 월입니다. 1~12 중 선택해주세요.'"
        ),
        "turns": [{
            "user": "13월 근무표 생성해줘",
            "expected_skill_calls": [],
            "expected_answer_contains": ["유효"],
        }],
        "mode": "read_only", "permission": "any",
        "notes": "agent 가 input validation 으로 차단해야 — LLM 자체 책임.",
    },
    {
        "id": "L3",
        "category": "edge_case",
        "complexity": "simple",
        "query": "김민지 프리셉터를 김민지 본인으로 설정",
        "expected_action": (
            "update_person_attr(field=preceptor_id, value=self) preview 시 "
            "check_preceptor_team_consistency 가 자기참조 감지 → error 또는 clarification. "
            "agent 응답: '본인을 본인의 프리셉터로 지정할 수 없습니다.'"
        ),
        "turns": [{
            "user": "김민지 프리셉터를 김민지 본인으로 설정",
            "expected_skill_calls": [{"name": "update_person_attr",
                "args_subset": {"preview_only": True}}],
            "expected_answer_contains": ["없습니다", "자기"],
        }],
        "mode": "preview", "permission": "hn_only", "notes": None,
    },
    {
        "id": "L4",
        "category": "edge_case",
        "complexity": "simple",
        "query": "이번 달 위반사항 알려줘",
        "expected_action": (
            "validate_schedule 호출. 위반 없으면 'pass', 있으면 종류별 violation 리스트. "
            "근무표가 없는 월이면 'No schedule found' error 응답."
        ),
        "turns": [{
            "user": "이번 달 위반사항 알려줘",
            "expected_skill_calls": [{"name": "validate_schedule", "args_subset": {}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },

    # ════════════════════════════════════════════════════════════════
    # M. 분석 리포트 (analyze) — 3
    # ════════════════════════════════════════════════════════════════
    {
        "id": "M1",
        "category": "analyze",
        "complexity": "simple",
        "query": "5월 야간 균형도 분석해줘",
        "expected_action": (
            "analyze_report(scope=schedule, shift_codes=['N']). "
            "응답: nurse 별 N 카운트, variance, mean, min, max + 균형도 평가."
        ),
        "turns": [{
            "user": "5월 야간 균형도 분석해줘",
            "expected_skill_calls": [{"name": "analyze_report", "args_subset": {}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "M2",
        "category": "analyze",
        "complexity": "simple",
        "query": "5월 근무표 v1 과 v2 비교",
        "expected_action": (
            "analyze_report(operation='compare', year=2026, month=5). "
            "응답: 두 버전 간 nurse×date 셀 단위 변경 diff."
        ),
        "turns": [{
            "user": "5월 근무표 v1 과 v2 비교",
            "expected_skill_calls": [{"name": "analyze_report",
                "args_subset": {"operation": "compare"}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
    {
        "id": "M3",
        "category": "analyze",
        "complexity": "simple",
        "query": "공정성 리포트 보여줘",
        "expected_action": (
            "analyze_report(scope=schedule) — per_nurse_counts + variance 지표 응답. "
            "fairness 지표 (max-min, 표준편차) 강조."
        ),
        "turns": [{
            "user": "공정성 리포트 보여줘",
            "expected_skill_calls": [{"name": "analyze_report", "args_subset": {}}],
        }],
        "mode": "read_only", "permission": "any", "notes": None,
    },
]


def get_by_category(category: str) -> list[dict]:
    return [s for s in SCENARIOS if s["category"] == category]


def get_compound_scenarios() -> list[dict]:
    return [s for s in SCENARIOS if s["complexity"] == "compound"]


def get_simple_scenarios() -> list[dict]:
    return [s for s in SCENARIOS if s["complexity"] == "simple"]


def get_known_gaps() -> list[dict]:
    return [s for s in SCENARIOS if s.get("notes") and ("미구현" in s["notes"] or "gap" in s["notes"].lower())]


CATEGORIES = sorted({s["category"] for s in SCENARIOS})
TOTAL = len(SCENARIOS)
COMPOUND_COUNT = sum(1 for s in SCENARIOS if s["complexity"] == "compound")
