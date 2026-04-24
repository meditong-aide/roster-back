"""Skill tool descriptions — JSON schemas for LLM function calling.

These are passed to the LLM's `tools` parameter. The LLM reads the
descriptions and selects the appropriate skill + parameters.

Design principles (research-backed):
- Korean descriptions + Korean examples (compensate 5-15% non-English accuracy drop)
- "When to use" + "When NOT to use" (ToolBench: specificity → accuracy)
- Parameter examples (10-20% filling accuracy improvement)
- Enums where possible (reduce hallucination)
"""

from __future__ import annotations

SKILL_TOOLS: list[dict] = [
    {
        "name": "query_schedule",
        "description": (
            "병동의 근무 관련 데이터를 조회합니다.\n"
            "원티드(희망근무) 신청 내역, 원티드 조정판, 근무표, 간호사 정보, "
            "시프트 정의, 제약조건 설정 등을 조회할 때 사용합니다.\n\n"
            "데이터 수정/삭제에는 사용하지 마세요. 읽기 전용입니다.\n\n"
            "예시:\n"
            "- '원티드 신청 내역 보여줘' → scope='wanted_submissions'\n"
            "- '원티드 미제출자 알려줘' → scope='wanted_submissions', operation='count'\n"
            "- '김민지 근무 조회' → scope='schedule', nurse_name='김민지'\n"
            "- '나이트 근무자 누구야?' → scope='schedule', shift_name='나이트'\n"
            "- '현재 설정값' → scope='constraint_config'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": [
                        "wanted_submissions",
                        "wanted_adjustment",
                        "schedule",
                        "nurse_info",
                        "shift_definitions",
                        "constraint_config",
                        "generation_job",
                    ],
                    "description": (
                        "조회 대상. 원티드=wanted_submissions, "
                        "근무표=schedule, 간호사=nurse_info"
                    ),
                },
                "operation": {
                    "type": "string",
                    "enum": ["list", "count", "summarize"],
                    "description": (
                        "list=개별 항목 목록 조회. "
                        "count=제출/미제출 현황 (submitted + not_submitted 명단 포함). "
                        "summarize=요약. 기본값 list. "
                        " 미제출자, 제출 현황, 몇 명 제출했는지 등을 물으면 반드시 count를 사용하세요."
                    ),
                },
                "nurse_name": {
                    "type": "string",
                    "description": "간호사 이름 (한글, 예: '김민지'). 내부에서 ID로 변환됨",
                },
                "shift_name": {
                    "type": "string",
                    "description": (
                        "시프트명 (예: '데이', '이브닝', '나이트', 'OFF'). "
                        "내부에서 코드로 변환됨"
                    ),
                },
                "date": {
                    "type": "string",
                    "description": "특정 날짜. 반드시 YYYY-MM-DD 형식 (예: '2026-04-05')",
                },
                "date_range": {
                    "type": "string",
                    "description": "날짜 범위. 반드시 YYYY-MM-DD~YYYY-MM-DD 형식 (예: '2026-04-01~2026-04-10')",
                },
            },
            "required": ["scope"],
        },
    },
    {
        "name": "bulk_mutation",
        "description": (
            "근무 데이터를 수정합니다. 원티드 취소, 조정판 수정, 근무표 시프트 변경 등.\n\n"
            "반드시 preview_only=true로 먼저 실행하고 사용자 확인 후 실제 수정하세요.\n"
            "읽기 전용 조회에는 사용하지 마세요. query_schedule을 사용하세요.\n\n"
            "예시:\n"
            "- '김민지 원티드 취소' → scope='wanted_submissions', action='cancel'\n"
            "- '조정판 쉬는사람 해제' → scope='wanted_adjustment', action='unapply_off'\n"
            "- '김민지 4/5 D→E' → scope='schedule', action='change_shift'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["wanted_submissions", "wanted_adjustment", "schedule"],
                },
                "action": {
                    "type": "string",
                    "enum": [
                        "cancel",
                        "approve",
                        "reject",
                        "unapply_off",
                        "apply",
                        "change_shift",
                        "add_shift",
                        "remove_shift",
                    ],
                },
                "nurse_name": {"type": "string"},
                "shift_name": {"type": "string"},
                "new_shift_name": {
                    "type": "string",
                    "description": "변경할 새 시프트",
                },
                "date": {"type": "string"},
                "preview_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "true=미리보기만, false=실제 실행",
                },
            },
            "required": ["scope", "action"],
        },
    },
    {
        "name": "validate_schedule",
        "description": (
            "근무표의 제약조건 위반을 검사합니다. "
            "연속 야간, 최대 근무일, 경력 커버리지 등.\n\n"
            "예시: '근무표 검증해줘', '위반사항 뭐야?'"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "recommend_candidates",
        "description": (
            "특정 날짜/시프트에 투입 가능한 대체 간호사를 추천합니다.\n"
            "해당 날짜에 비번인 간호사 중 자격 요건에 맞는 사람을 찾습니다.\n\n"
            "예시: '4월 5일 나이트 대체자 추천해줘'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "대상 날짜"},
                "shift_name": {"type": "string", "description": "대상 시프트"},
                "exclude_nurse_name": {
                    "type": "string",
                    "description": "제외할 간호사",
                },
            },
            "required": ["date", "shift_name"],
        },
    },
    {
        "name": "repair_schedule",
        "description": (
            "근무표 불균형을 분석하고 조정 제안을 합니다. "
            "직접 수정하지 않습니다.\n\n"
            "예시: '근무표 조정 제안해줘', '야간 균형 맞춰줘'"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "analyze_report",
        "description": (
            "근무표/원티드 데이터를 분석하여 리포트를 생성합니다.\n"
            "공정성 분석, 시프트 분포, 간호사별 비교 등.\n\n"
            "예시: '야간 불균형 보여줘', '간호사별 근무 수 비교'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["schedule", "wanted"],
                },
                "analysis_type": {
                    "type": "string",
                    "enum": [
                        "fairness",
                        "distribution",
                        "comparison",
                        "headcount",
                    ],
                },
                "shift_name": {"type": "string"},
            },
        },
    },
    {
        "name": "update_constraint",
        "description": (
            "근무표 생성 제약조건을 수정합니다. "
            "야간 최대 횟수, 연속 근무일 제한 등.\n\n"
            "⚠️ 다음 근무표 생성에 영향을 미칩니다.\n\n"
            "예시: '야간 최대 7회로 설정', '연속 근무 5일로 제한'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": "설정 필드 (자연어 가능)",
                },
                "value": {
                    "type": ["string", "number", "boolean"],
                },
                "preview_only": {"type": "boolean", "default": True},
            },
            "required": ["field", "value"],
        },
    },
    {
        "name": "update_person_attr",
        "description": (
            "간호사 개인 속성 수정. 직급, 팀, 경력, 야간 전담 등.\n\n"
            "예시: '김민지 직급 3으로 변경', '박지은 야간 요원 지정'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nurse_name": {"type": "string"},
                "field": {"type": "string"},
                "value": {"type": ["string", "number", "boolean"]},
                "preview_only": {"type": "boolean", "default": True},
            },
            "required": ["nurse_name", "field", "value"],
        },
    },
    {
        "name": "generate_schedule",
        "description": (
            "근무표 자동 생성 (비동기). 요청 후 작업 ID가 반환됩니다.\n\n"
            "⚠️ 고위험 작업. 반드시 사용자 확인 후 실행.\n\n"
            "예시: '4월 근무표 생성해줘'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "사용자 확인 여부. false면 확인 요청 반환",
                },
            },
        },
    },
]
