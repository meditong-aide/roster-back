"""파라미터 근거 평가 코퍼스 — 실제 저장 필드(모델/서비스로직)에 매핑된 질의.

기존 corpus 스키마 계승 + expected_behavior(outcome 타입) 추가:
    query: 자연어 질의
    expected_skill: 정답 스킬(tool name). abstain 이면 None.
    expected_slots: args ⊇ 여기(subset 매칭). 없으면 {}.
    expected_behavior: tool | clarify | abstain | reject | deny
    category: 파라미터 그룹
    note: 트리키 포인트
"""

from __future__ import annotations

PARAM_QUERIES: list[dict] = [
    # ── 1. 간호사 속성 (update_person_attr) ──
    {"query": "김민지 등급 3으로 올려줘", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "grade"}, "expected_behavior": "tool", "category": "person_attr", "note": None},
    {"query": "최지현 수간호사로 지정해줘", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "is_head_nurse"}, "expected_behavior": "tool", "category": "person_attr", "note": None},
    {"query": "김민지 야간 전담으로 바꿔줘", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "allowed_shifts"}, "expected_behavior": "tool", "category": "person_attr", "note": None},
    {"query": "정수민 프리셉터를 김민지로 지정", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "preceptor_id"}, "expected_behavior": "tool", "category": "person_attr", "note": None},
    {"query": "한지우 평일 데이 고정근무로", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "fixed_shift"}, "expected_behavior": "tool", "category": "person_attr", "note": None},
    {"query": "이영희 메모에 '허리 부상 주의' 추가", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "nurse_memo"}, "expected_behavior": "tool", "category": "person_attr", "note": None},
    {"query": "박철수 원티드 최대 3건으로 설정", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "wanted_max_requests"}, "expected_behavior": "tool", "category": "person_attr", "note": None},
    # 병동 이동 vs 팀 이동 혼동 트리키
    {"query": "한지우 A팀으로 이동시켜줘", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "team_id"}, "expected_behavior": "tool", "category": "person_attr", "note": "team_id (group_id 아님)"},
    {"query": "김민지 중환자실2 병동으로 옮겨줘", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "group_id"}, "expected_behavior": "tool", "category": "person_attr", "note": "group_id (team_id 아님)"},

    # ── 2. 퇴사/삭제 ──
    {"query": "이영희 8월 31일자로 퇴사 처리해줘", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "resignation_date"}, "expected_behavior": "tool", "category": "resignation", "note": None},
    {"query": "김민지 퇴사 처리해줘", "expected_skill": "update_person_attr",
     "expected_slots": {"field": "resignation_date"}, "expected_behavior": "tool", "category": "resignation",
     "note": "날짜없음 → update_person_attr 라우팅 정답, skill이 날짜 되묻음(유닛테스트 담당)"},
    {"query": "퇴사자를 명단에서 삭제하려고 합니다", "expected_skill": "navigate",
     "expected_slots": {"target": "nurse_management"}, "expected_behavior": "tool", "category": "delete", "note": None},

    # ── 3. 근무표 제약 (update_constraint) ──
    {"query": "연속근무 최대 4일로 제한해줘", "expected_skill": "update_constraint",
     "expected_slots": {}, "expected_behavior": "tool", "category": "constraint", "note": "max_conseq_work"},
    {"query": "월 오프 9개로 설정", "expected_skill": "update_constraint",
     "expected_slots": {}, "expected_behavior": "tool", "category": "constraint", "note": "off_days"},
    {"query": "야간 2번 뒤 2오프 켜줘", "expected_skill": "update_constraint",
     "expected_slots": {}, "expected_behavior": "tool", "category": "constraint", "note": "two_offs_after_two_nig"},
    {"query": "이브닝 다음날 데이 금지로 해줘", "expected_skill": "update_constraint",
     "expected_slots": {}, "expected_behavior": "tool", "category": "constraint", "note": "banned_day_after_eve"},
    # policy_locked 트리키 — 실행단에서 거부
    {"query": "월 최대 야간 횟수를 8개로 바꿔줘", "expected_skill": "update_constraint",
     "expected_slots": {}, "expected_behavior": "reject", "category": "constraint", "note": "max_nig_per_month = policy_locked"},

    # ── 4. 원티드 (wanted / deadline) ──
    {"query": "원티드 마감일 8월 20일로 연장해줘", "expected_skill": "manage_wanted_deadline",
     "expected_slots": {}, "expected_behavior": "tool", "category": "wanted", "note": "exp_date"},
    {"query": "원티드 지금 바로 마감해줘", "expected_skill": "manage_wanted_deadline",
     "expected_slots": {}, "expected_behavior": "tool", "category": "wanted", "note": "status=closed"},
    {"query": "김민지 8월 15일 나이트 원티드 추가, 사유는 병원 방문", "expected_skill": "bulk_mutation",
     "expected_slots": {}, "expected_behavior": "tool", "category": "wanted", "note": "wanted add"},

    # ── 5. 조회 (query_schedule) — 저장값 읽기 ──
    {"query": "김민지 원티드 최대 몇 건으로 돼있어?", "expected_skill": "query_schedule",
     "expected_slots": {}, "expected_behavior": "tool", "category": "read", "note": "wanted_max_requests 읽기"},
    {"query": "연속근무 며칠까지 설정돼 있어?", "expected_skill": "query_schedule",
     "expected_slots": {}, "expected_behavior": "tool", "category": "read", "note": "max_conseq_work 읽기"},
    {"query": "이번달 오프 며칠로 돼있어?", "expected_skill": "query_schedule",
     "expected_slots": {}, "expected_behavior": "tool", "category": "read", "note": "off_days 읽기"},
    {"query": "김민지 야간전담이야?", "expected_skill": "query_schedule",
     "expected_slots": {}, "expected_behavior": "tool", "category": "read", "note": "allowed_shifts 읽기"},

    # ── 6. 미지원 (abstain) ──
    {"query": "간호사들 이번 달 급여 얼마씩 나가?", "expected_skill": None,
     "expected_slots": {}, "expected_behavior": "abstain", "category": "unsupported", "note": "급여 없음"},
    {"query": "근무표를 카카오톡으로 공유해줘", "expected_skill": None,
     "expected_slots": {}, "expected_behavior": "abstain", "category": "unsupported", "note": "외부공유 없음"},
    {"query": "다음 주 환자 수 예측해서 인력 늘려줘", "expected_skill": None,
     "expected_slots": {}, "expected_behavior": "abstain", "category": "unsupported", "note": "예측 없음"},

    # ── 7. 모호 (clarify) ──
    {"query": "김민지 근무 좀 바꿔줘", "expected_skill": None,
     "expected_slots": {}, "expected_behavior": "clarify", "category": "ambiguous", "note": "어느날/뭘로"},
    {"query": "이번 달 오프 좀 조정해줘", "expected_skill": None,
     "expected_slots": {}, "expected_behavior": "clarify", "category": "ambiguous", "note": "누구/며칠"},
]
