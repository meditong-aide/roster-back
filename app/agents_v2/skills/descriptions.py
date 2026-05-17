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
            "병동의 근무·인사·설정 데이터를 **읽기 전용**으로 조회합니다. "
            "사용자가 '보여줘 / 알려줘 / 누구야 / 몇 명이야 / 리스트업 / 어떻게 돼있어' 등 "
            "**상태 확인**을 묻는 모든 흐름에서 가장 먼저 진입하는 그라운딩·조회 스킬입니다.\n\n"

            "⛔ 절대 데이터를 수정/삭제하지 않습니다. 변경은 다른 스킬(bulk_mutation, "
            "update_person_attr, update_constraint, repair_schedule)이 담당합니다.\n\n"

            "─────────── scope (조회 도메인) ───────────\n"
            "scope는 **'어떤 종류의 데이터를 보고 싶은가'**를 의미합니다. 키워드 매칭이 아니라, "
            "사용자가 묻는 정보가 어느 데이터 도메인에 속하는지 판단해서 고르세요.\n\n"
            "- `wanted_submissions` — **제출 메타데이터 전용**: 누가 제출했는지/안 했는지, 제출 시각. "
            "  '미제출자 누구야', '몇 명 제출했어', '제출 현황'처럼 **제출 여부·집계**만 묻는 경우.\n"
            "- `wanted_adjustment` — **원티드 신청 내역의 정식 조회 경로**(fixed_wanted_entries 테이블). "
            "  간호사가 신청한 시프트의 날짜·코드, 수간호사 조정 결과를 모두 담고 있음. "
            "  '4월 원티드 신청 내역', '김민지 4월 원티드 뭐 냈어', '5/3 원티드 어떻게 돼있어' 등 "
            "  **신청 내용**(어느 날 무슨 시프트)을 묻는 모든 흐름은 여기로.\n"
            "  ※ 응답의 `source_type` 값으로 의지 분기:\n"
            "     · `original` → `shift_id`가 간호사 신청 그대로\n"
            "     · `modified` → 수간호사가 코드 변경. 간호사 원의지는 `original_shift_id` 필드\n"
            "     · `added` → 간호사 신청 없이 수간호사가 단독 추가\n"
            "     · `weekly_off` → 자동 주휴\n"
            "    사용자 발화('순수 신청만' / '조정 후 모습' / '전체')에 맞춰 자연어로 분기 응답.\n"
            "    필요 시 `source_types` 파라미터로 사전 필터링도 가능.\n"
            "- `schedule` — 생성·확정된 **근무표 셀**(누가 언제 무슨 근무인지). "
            "  '4/15 데이 누가야', '김민지 4월 근무', '이번 달 나이트 명단'.\n"
            "- `nurse_info` — **간호사 인사 정보**(이름·등급·팀·직급·야간전담·고정근무·메모 등). "
            "  '김민지 정보', '신규 간호사 누구', '팀 구성', '프리셉터 매칭'.\n"
            "- `shift_definitions` — 병동의 **시프트 정의**(D/E/N/O/M, 한글명, 카테고리). "
            "  '시프트 종류 뭐 있어', '근무 코드'.\n"
            "- `constraint_config` — 스케줄링 **제약조건 설정값**(연속근무 한도, 주말 휴무 정책 등). "
            "  '현재 설정', '제약 어떻게 돼있어'.\n"
            "- `generation_job` — 최근 **근무표 자동생성 작업**의 상태/결과. "
            "  '생성 어디까지 됐어', '자동생성 결과'.\n\n"

            "─────────── operation (조회 의도) ───────────\n"
            "사용자가 **어떤 형태의 답**을 원하는지 의미적으로 판단:\n"
            "- `list` — 개별 항목들을 죽 보고 싶다 (기본값).\n"
            "- `count` — 인원수·현황·집계·제출 비율·명단 분류가 본질이다. "
            "  '몇 명', '미제출자', '아직 안 낸 사람', '제출 현황' 등 **현황·분류 의도**면 `count`.\n"
            "- `summarize` — 양이 많거나 한눈에 보고 싶다는 신호("
            "'요약', '한눈에', '대충 어떤지').\n\n"

            "💡 (자동) `schedule` scope에서 필터 없이 60+행이면 자동 요약됩니다. "
            "사용자가 작은 단위(특정 간호사·날짜)를 원하면 `nurse_name` 또는 `date`를 함께 주세요.\n\n"

            "─────────── 그라운딩 (이름·표현 → 파라미터) ───────────\n"
            "- 사람 이름은 그대로 `nurse_name`에 넣으세요. 스킬 내부에서 nurse_id로 해석합니다.\n"
            "- 시프트 표현('데이/이브닝/나이트/오프/미들')은 `shift_name`에 자연어로 넣어도 됩니다.\n"
            "- 날짜·기간은 반드시 `YYYY-MM-DD` / `YYYY-MM-DD~YYYY-MM-DD` 형식으로 정규화해서 주세요. "
            "  '이번 달', '4월 둘째 주' 같은 상대 표현은 LLM이 절대 날짜로 변환 후 전달.\n"
            "- '신규/시니어' 같은 등급 모호 표현은 먼저 `scope='nurse_info'`로 분포를 확인한 뒤 "
            "  `grade=정수`로 다시 호출하세요. (등급 체계는 병동마다 다름)\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 제약 위반 여부·왜 못 짜졌는지 → `validate_schedule`\n"
            "- 분포 비교·공정성·통계 분석 → `analyze_report`\n"
            "- 빈 자리에 누가 가능한지 추천 → `recommend_candidates`\n"
            "- 단순히 '데이터를 읽어서 보여달라'면 모두 `query_schedule`\n\n"

            "─────────── 예시 ───────────\n"
            "- '4월 원티드 미제출자 리스트업' → scope=wanted_submissions, operation=count\n"
            "- '몇 명 제출했어' → scope=wanted_submissions, operation=count\n"
            "- '4월 원티드 신청 내역 보여줘' → scope=wanted_adjustment\n"
            "- '김민지 4월 원티드 뭐 냈어' → scope=wanted_adjustment, nurse_name='김민지'\n"
            "- '4월 첫째 주 김민지가 낸 원티드' → scope=wanted_adjustment, "
            "nurse_name='김민지', date_range='2026-04-01~2026-04-07'\n"
            "- '김민지 4월 근무 보여줘' → scope=schedule, nurse_name='김민지'\n"
            "- '4/15 나이트 누구야' → scope=schedule, date='2026-04-15', shift_name='나이트'\n"
            "- '신규 간호사 누구야' → scope=nurse_info (등급 분포 먼저 확인)\n"
            "- '현재 제약 설정' → scope=constraint_config\n"
            "- '자동생성 어디까지 됐어' → scope=generation_job\n"
            "- '이번 달 시프트 종류' → scope=shift_definitions"
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
                        "monthly_limit",
                    ],
                    "description": (
                        "조회 도메인. 사용자가 묻는 정보가 어느 데이터 영역에 속하는지로 결정. "
                        "원티드 **제출 여부·집계**=wanted_submissions, "
                        "원티드 **신청 내용**(시프트·날짜)=wanted_adjustment, "
                        "근무표 셀=schedule, 간호사 인사정보=nurse_info, "
                        "시프트 정의=shift_definitions, 제약 설정=constraint_config, "
                        "생성잡 상태=generation_job, "
                        "**개인별 월 한도** (예: '김민지 5월 N 몇 번', '5월 D 정확히 설정한 간호사')=monthly_limit"
                    ),
                },
                "operation": {
                    "type": "string",
                    "enum": ["list", "count", "summarize"],
                    "description": (
                        "조회 형태. list=개별 항목 나열(기본). "
                        "count=인원수·현황·분류 의도(예: 미제출자, 제출 현황, 몇 명). "
                        "summarize=양이 많아 요약 의도. "
                        "사용자가 '명단/현황/몇 명/누가 안 냈어' 등 분류적 답을 원하면 count."
                    ),
                },
                "nurse_name": {
                    "type": "string",
                    "description": "간호사 이름(한글, 예: '김민지'). 스킬 내부에서 nurse_id로 그라운딩.",
                },
                "shift_name": {
                    "type": "string",
                    "description": (
                        "시프트명을 자연어 그대로 (예: '데이', '이브닝', '나이트', 'OFF', '미들'). "
                        "스킬 내부에서 shift_code로 해석."
                    ),
                },
                "date": {
                    "type": "string",
                    "description": "특정 일자. YYYY-MM-DD 형식 (예: '2026-04-15'). 상대표현 금지.",
                },
                "date_range": {
                    "type": "string",
                    "description": (
                        "기간. YYYY-MM-DD~YYYY-MM-DD 형식 (예: '2026-04-01~2026-04-07'). "
                        "'이번 주', '둘째 주' 등은 절대 날짜로 변환 후 전달."
                    ),
                },
                "grade": {
                    "type": "integer",
                    "description": (
                        "등급 필터(정수). 병동별 체계가 다르므로 '신규/시니어' 같은 표현은 "
                        "먼저 scope=nurse_info로 분포를 확인한 뒤 호출."
                    ),
                },
                "include_cancelled": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "원티드 조회 시 취소·미제출 건도 포함할지. 기본 false(제출 완료만). "
                        "'취소된 원티드', '미제출 원티드' 의도면 true."
                    ),
                },
                "submitted_date_range": {
                    "type": "string",
                    "description": (
                        "원티드 **제출일** 기준 기간(시프트 일자가 아님). "
                        "YYYY-MM-DD~YYYY-MM-DD. '지난주에 등록된 원티드' 같은 제출 시점 필터에 사용."
                    ),
                },
                "source_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["original", "modified", "added", "weekly_off"],
                    },
                    "description": (
                        "scope=wanted_adjustment 전용. 응답을 source_type별로 사전 필터링. "
                        "사용자 발화로부터 의지 분기를 명확히 추론할 수 있을 때만 지정. "
                        "기본 미지정 시 전체 반환 → 응답의 source_type/original_shift_id 보고 자연어로 분기. "
                        "예: '간호사가 신청한 것만'이면 ['original','modified'] 추가 후 modified는 original_shift_id로 해석."
                    ),
                },
                "is_applied": {
                    "type": "boolean",
                    "description": (
                        "scope=wanted_adjustment 전용. 조정판에서 실제 적용된 항목만(true) / 미적용만(false) 필터. "
                        "기본 미지정 시 전체."
                    ),
                },
            },
            "required": ["scope"],
        },
    },
    {
        "name": "bulk_mutation",
        "description": (
            "특정 날짜/시프트가 결합된 '스케줄 데이터'를 수정합니다. "
            "원티드(특정 날짜의 근무 신청), 조정판, 확정 근무표의 시프트 변경.\n\n"
            "⚠️ 반드시 preview_only=true로 먼저 실행하고 사용자 확인 후 실제 수정하세요.\n"
            "⛔ 읽기 전용 조회에는 사용하지 마세요. query_schedule을 사용하세요.\n\n"
            "⛔ 간호사 자체의 속성(직급/팀/역할/야간전담/고정근무/수간호사/메모/원티드 한도 등) 변경에는 "
            "절대 사용하지 마세요. 이는 update_person_attr의 영역입니다. "
            "한국어 동사 '변경/수정/지정/고정' 단어가 있어도, "
            "**대상이 특정 날짜의 근무가 아니라 간호사의 정적 속성**이면 update_person_attr을 사용하세요.\n\n"
            "스킬 선택 가이드:\n"
            "- '5/3', '4월 15일' 같은 **특정 날짜**가 핵심이면 → bulk_mutation\n"
            "- '김민지 직급', '한혜선 팀', '박춘일 야간 전담' 같은 **간호사 속성**이면 → update_person_attr\n"
            "- '원티드 마감일', '조정판' 같은 **스케줄 운영 메타**면 → bulk_mutation\n\n"
            "scope별 의미:\n"
            "- 'wanted_submissions': 간호사가 제출하는 원티드 신청서 (특정 날짜의 근무 희망/회피).\n"
            "- 'wanted_adjustment': 원티드 조정판 (수간호사가 신청들을 모아 조율하는 단계).\n"
            "- 'schedule': 확정된 근무표의 시프트 셀.\n\n"
            "scope × action 유효 조합:\n"
            "- wanted_submissions: cancel / approve / reject / add_shift / change_shift / update_deadline\n"
            "- wanted_adjustment: apply / unapply_off\n"
            "- schedule: change_shift / add_shift / remove_shift\n\n"
            "날짜 포맷:\n"
            "- date 파라미터는 항상 YYYY-MM-DD (예: '2026-05-03').\n"
            "- 사용자가 '5/3', '5월 3일'처럼 부분 표기하면 컨텍스트의 연도/월을 합쳐 변환.\n"
            "- new_deadline도 동일.\n\n"
            "nurse_name 필요 여부:\n"
            "- 단일 간호사 대상이면 필수 (예: '김민지 4/5 D→E').\n"
            "- '원티드 전체 취소' 같은 전사 작업은 생략 가능 (서버가 권한 범위 내 모두 처리).\n"
            "- 이름은 query_schedule로 nurse_id 그라운딩 후 사용 권장.\n\n"
            "⚠️ '한도'와 '마감일' 혼동 주의:\n"
            "- '원티드 최대 횟수/한도' (간호사 개인 설정) → update_person_attr.wanted_max_requests\n"
            "- '원티드 마감일/제출 기한' (운영 일정) → bulk_mutation action='update_deadline'\n\n"
            "예시:\n"
            "- '5/3 원티드 취소' → scope='wanted_submissions', action='cancel', date='2026-05-03'\n"
            "- '5/15에 Oz 원티드 추가' → scope='wanted_submissions', action='add_shift', date='2026-05-15', shift_name='Oz'\n"
            "- '5/3 원티드 D를 N으로' → scope='wanted_submissions', action='change_shift', date='2026-05-03', new_shift_name='N'\n"
            "- '원티드 전체 취소' → scope='wanted_submissions', action='cancel' (date 없이)\n"
            "- '원티드 마감일 수정' → scope='wanted_submissions', action='update_deadline', new_deadline='2026-04-18'\n"
            "- '조정판 쉬는사람 해제' → scope='wanted_adjustment', action='unapply_off'\n"
            "- '김민지 4/5 D→E' → scope='schedule', action='change_shift'\n\n"
            "⛔ 다음은 bulk_mutation 아님 (update_person_attr로):\n"
            "- '김민지 직급 3으로 변경' (날짜 없음 → 정적 속성)\n"
            "- '한혜선 1팀으로 이동' (특정 근무일 변경 아님)\n"
            "- '박춘일 야간 전담 지정' (간호사 자체 속성)\n"
            "- '이다영 메모 추가' (간호사 메모)\n"
            "- '박혜미 원티드 최대 횟수 5' (사용자 한도, 특정 원티드 신청 아님)"
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
                        "update_deadline",
                    ],
                    "description": (
                        "수행할 작업. 원티드 날짜별: cancel(취소), add_shift(추가), change_shift(변경). "
                        "근무표: change_shift, add_shift, remove_shift. 조정판: unapply_off, apply."
                    ),
                },
                "nurse_name": {"type": "string"},
                "shift_name": {"type": "string"},
                "new_shift_name": {
                    "type": "string",
                    "description": "변경할 새 시프트",
                },
                "date": {"type": "string"},
                "new_deadline": {
                    "type": "string",
                    "description": "새 마감일. 반드시 YYYY-MM-DD 형식 (예: '2026-04-18')",
                },
                "comment": {
                    "type": "string",
                    "description": (
                        "사유/메모. 원티드 추가/수정 시 사유가 있으면 포함. "
                        "예: '부모님 병원 방문', '개인 사정'"
                    ),
                },
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
            "기존 근무표가 **현재 설정된 제약조건을 위반하는지** 정해진 규칙 셋으로 검사합니다.\n"
            "사용자가 '맞게 짜졌어?', '위반된 거 뭐야?', '검증해줘', '룰 위반 있어?'처럼 "
            "규정 준수 여부를 묻는 의도일 때 사용.\n\n"

            "검사 항목 (config 기반 자동 적용):\n"
            "- consecutive_night_exceeded — 야간 연속 한도(2 또는 3) 초과\n"
            "- max_consecutive_work_exceeded — 연속 근무일(max_conseq_work) 초과\n"
            "- grade_coverage_missing — 시프트별 최소 경력자(min_exp_per_shift) 미충족\n"
            "- night_distribution_uneven — 야간 균등 배분(even_nights) 위반 (variance>2.0)\n\n"

            "출력: violation_count, violations 배열, status('pass'|'fail'). "
            "각 위반에는 nurse_id/nurse_name·shift_id·날짜 정보 포함.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- '왜 이 위반이 생겼는지·어떻게 고칠지' → validate 결과를 바탕으로 repair_schedule\n"
            "- '시프트별 분포·공정성 통계가 보고 싶다' → analyze_report (위반 여부 아님)\n"
            "- '단순히 근무표 셀이 보고 싶다' → query_schedule(scope='schedule')\n\n"

            "예시:\n"
            "- '4월 근무표 위반사항 뭐야' → year=2026, month=4\n"
            "- '검증해줘' → 현재 컨텍스트의 (year, month) 사용"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "검증 대상 연도 (예: 2026)"},
                "month": {"type": "integer", "description": "검증 대상 월 (1~12)"},
                "schedule_id": {
                    "type": "string",
                    "description": "특정 버전을 지정할 때만 사용. 미지정 시 (year,month)에서 자동 해석.",
                },
            },
        },
    },
    {
        "name": "recommend_candidates",
        "description": (
            "**특정 날짜의 특정 시프트에 투입 가능한 후보 간호사**를 찾아 점수순으로 추천합니다. "
            "사용자가 '대체할 사람', '누가 가능해', '빈 자리 누가 맞아', '추천해줘'처럼 "
            "**한 자리(date+shift)에 대한 후보 탐색** 의도일 때 사용.\n\n"

            "동작:\n"
            "- 해당 날짜에 비번(off)인 간호사를 모은 뒤,\n"
            "- 각 간호사의 work_shifts(가능 시프트)에 target_shift가 포함되는지 자격 필터,\n"
            "- exclude(이미 배정/제외 대상) 제거,\n"
            "- 등급(grade) 내림차순 정렬해서 후보 반환.\n\n"

            "출력: candidate_count + candidates(nurse_id, name, grade, experience, team_id, current_shift).\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- '병동 전체 균형이 깨졌다 → 어떻게 손볼지 제안' → repair_schedule (전체 진단·교체쌍)\n"
            "- '추천된 사람으로 실제 근무 변경' → bulk_mutation(scope='schedule', action='change_shift')\n"
            "- '단순히 그날 누가 비번인지' → query_schedule(scope='schedule', date=...)\n\n"

            "그라운딩:\n"
            "- date는 반드시 YYYY-MM-DD.\n"
            "- 시프트는 사용자가 '나이트/이브닝/데이' 등 자연어로 부르면 그대로 shift_codes 배열에 넣어 호출.\n"
            "- 제외 대상(이미 배정된 사람, 본인 제외 등)은 nurse_ids 배열로.\n\n"

            "예시:\n"
            "- '4/5 나이트 대체자 추천' → date='2026-04-05', shift_codes=['N']\n"
            "- '4/15 데이에 김민지 빼고 가능한 사람' → date='2026-04-15', shift_codes=['D'], "
            "nurse_ids=[김민지의 nurse_id]"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "대상 날짜. YYYY-MM-DD 필수.",
                },
                "shift_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "대상 시프트 코드 배열(예: ['N'] 또는 ['D','E']).",
                },
                "shift_name": {
                    "type": "string",
                    "description": "자연어 시프트명. shift_codes 대신 사용 가능 (예: '나이트').",
                },
                "nurse_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "후보에서 제외할 간호사 ID 배열(이미 배정된 인원 등).",
                },
                "schedule_id": {
                    "type": "string",
                    "description": "특정 버전 지정 시. 미지정 시 (year, month)에서 자동 해석.",
                },
            },
            "required": ["date"],
        },
    },
    {
        "name": "repair_schedule",
        "description": (
            "기존 근무표를 진단해서 **불균형·미충원 문제와 그 조정 제안**을 생성합니다. "
            "직접 수정하지 않으며, 제안 목록만 반환.\n\n"
            "사용자가 '근무표 조정 제안', '야간 균형 맞춰줘', '문제 있으면 고칠 방법 알려줘'처럼 "
            "**개선 방향 탐색** 의도일 때 사용.\n\n"

            "검출하는 문제 유형:\n"
            "- rebalance_nights — 특정 야간 시프트에서 간호사 간 횟수 분산이 큼(>1.5) → 가장 많은↔적은 간호사 교체쌍 제안\n"
            "- understaffed — 특정 날짜·시프트에 working 인원 0명 → 최소 1명 배정 권고\n\n"

            "출력: suggestion_count + suggestions 배열. 각 항목에 type, 관련 nurse/date/shift, suggested_swaps.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- '룰 위반 여부만 보고 싶다' → validate_schedule (yes/no 판정)\n"
            "- '제안된 교체를 실제 근무표에 반영' → bulk_mutation(scope='schedule', action='change_shift')\n"
            "- '특정 자리에 누가 가능한지' → recommend_candidates\n"
            "- '분포 통계만 보고 싶다' → analyze_report\n\n"

            "권장 흐름: validate_schedule → repair_schedule → (사용자 동의) → bulk_mutation.\n\n"

            "예시:\n"
            "- '4월 근무표 야간 균형 맞춰줘' → year=2026, month=4\n"
            "- '문제 있으면 고칠 방법' → 현재 컨텍스트의 (year, month)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "대상 연도"},
                "month": {"type": "integer", "description": "대상 월(1~12)"},
                "schedule_id": {
                    "type": "string",
                    "description": "특정 버전 지정 시. 미지정 시 (year,month)에서 자동 해석.",
                },
            },
        },
    },
    {
        "name": "analyze_report",
        "description": (
            "근무표 또는 원티드 데이터를 **분석/집계**하여 리포트를 생성합니다. "
            "공정성, 시프트별 분산, 간호사별 비교, 일자별 헤드카운트, 버전 간 diff 등.\n\n"
            "사용자가 '얼마나 차이나', '평균', '분포', '비교', '공정해?', '몇 명씩 들어가있어'처럼 "
            "**통계·분포·비교** 의도일 때 사용.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 룰 위반 'pass/fail' 판정만 → validate_schedule\n"
            "- 개선 제안(교체쌍, 빈자리 보완) → repair_schedule\n"
            "- 단순히 데이터 행을 보고 싶다(요약 포함) → query_schedule(operation='summarize')\n"
            "- 이 스킬은 **수치·분산·diff** 자체가 답일 때 적합.\n\n"

            "scope 의미:\n"
            "- 'draft_schedule' / 'schedule' — 현재 근무표 분석\n"
            "- 'wanted_submissions' / 'wanted_adjustment' — 원티드 제출 분석 (제출 현황 + 시프트별 신청 분포)\n\n"

            "operation 의미:\n"
            "- 'summarize' (기본) — 시프트별 분산·간호사별 카운트·일별 헤드카운트 종합 리포트\n"
            "- 'compare' — 동일 (year, month) 의 두 버전 간 diff (셀 단위 변경 목록)\n\n"

            "shift_codes 필터: 특정 시프트만 분석할 때(예: 야간만 공정성).\n\n"

            "예시:\n"
            "- '4월 야간 공정해?' → scope='schedule', shift_codes=['N']\n"
            "- '간호사별 근무 수 비교' → scope='schedule', operation='summarize'\n"
            "- '버전 1, 2 비교' → scope='schedule', operation='compare'\n"
            "- '4월 원티드 신청 분포' → scope='wanted_submissions'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": [
                        "schedule",
                        "draft_schedule",
                        "wanted_submissions",
                        "wanted_adjustment",
                    ],
                    "description": "분석 대상 도메인.",
                },
                "operation": {
                    "type": "string",
                    "enum": ["summarize", "compare"],
                    "description": "summarize=종합 리포트(기본), compare=두 버전 간 diff.",
                },
                "shift_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "특정 시프트만 필터(예: ['N']로 야간만 공정성 분석).",
                },
                "shift_name": {
                    "type": "string",
                    "description": "자연어 시프트명. shift_codes 대안.",
                },
                "year": {"type": "integer"},
                "month": {"type": "integer"},
            },
        },
    },
    {
        "name": "update_constraint",
        "description": (
            "병동 **전체에 적용되는 스케줄링 정책**을 수정합니다. "
            "특정 간호사 한 명이 아니라 **다음 근무표 생성 시 모든 간호사**에게 영향이 가는 규칙·인원·정책 값.\n\n"

            "⚠️ 적용 시점: 다음 근무표 생성/조정에 반영. 이미 확정된 근무표는 자동으로 바뀌지 않음.\n"
            "⚠️ 변경 전 반드시 preview_only=true로 현재 값 확인 후 사용자 동의를 거쳐 적용.\n"
            "⛔ 현재 설정값을 단순히 **읽기만** 하려면 query_schedule(scope='constraint_config') 사용.\n\n"

            "─────────── update_person_attr와의 경계 (가장 자주 혼동) ───────────\n"
            "결정 기준: **'한 사람의 속성'인가 / '병동 전체 규칙'인가**.\n"
            "- '박춘일 야간 전담' / '김민지 데이 고정근무' → 개인 속성 → update_person_attr\n"
            "- '야간 최대 7회' / '연속 근무 5일 제한' / '데이 필요인원 3명' → 병동 정책 → update_constraint\n"
            "- '김민지 원티드 한도 5건' → 개인 한도 → update_person_attr (wanted_max_requests)\n"
            "- '병동 전체 야간 균등 배분 켜줘' → 병동 정책 → update_constraint\n\n"

            "─────────── 정책 영역 (의미적 그룹) ───────────\n"
            "[A] **시프트별 필요인원 (RosterConfig)**\n"
            "  • day_req / eve_req / nig_req — 각 시프트 기본 필요인원 (정수)\n"
            "  • off_days — 월 오프 일수 (정수)\n\n"
            "[B] **연속·휴무 규칙**\n"
            "  • max_nig_per_month — 월 야간 최대 횟수 (정수)\n"
            "  • max_conseq_work — 연속 근무 최대 일수 (정수)\n"
            "  • three_seq_nig — 3연속 야간 허용 여부 (bool)\n"
            "  • two_offs_after_three_nig / two_offs_after_two_nig — 야간 후 2일 휴무 (bool)\n"
            "  • two_offs_per_week — 주 2회 오프 보장 (bool)\n"
            "  • banned_day_after_eve — 이브닝 다음날 데이 금지 (bool)\n"
            "  • sequential_offs — 오프 연속 배치 선호 (bool)\n"
            "  • even_nights — 야간 균등 배분 (bool)\n\n"
            "[C] **구조 정책**\n"
            "  • preceptee_on — 프리셉터-프리셉티 매칭 활성 (bool)\n"
            "  • team_balance_enable / team_balance_gauge — 팀 밸런스 활성/강도(0~10)\n\n"
            "[D] **시프트 슬롯별 인원 (ShiftManage)** — 슬롯 단위 미세 조정\n"
            "  • field='manpower' + nurse_class('RN'/'AN') + shift_slot(정수) + value=정수\n"
            "  • 사용자가 'RN 데이 슬롯 인원 4명으로'처럼 슬롯을 특정할 때.\n\n"

            "─────────── 그라운딩 ───────────\n"
            "사용자의 자연어를 위 영역 [A]~[D] 중 어디에 속하는지 의미적으로 판단하고, "
            "DB 필드명을 `field`에, 정규화 값을 `value`에 넣으세요. "
            "bool 필드의 '켜줘/허용/적용' → true, '꺼줘/금지/해제' → false. "
            "필드명이 모호한 표현('야간 최대'='max_nig_per_month' vs '야간 필요인원'='nig_req')은 "
            "사용자에게 의미를 한 번 더 확인.\n\n"

            "─────────── 예시 ───────────\n"
            "- '야간 최대 7회로' → field='max_nig_per_month', value=7\n"
            "- '연속 근무 5일 제한' → field='max_conseq_work', value=5\n"
            "- '이브닝 다음날 데이 금지 해제' → field='banned_day_after_eve', value=false\n"
            "- '팀 밸런스 켜줘' → field='team_balance_enable', value=true\n"
            "- '팀 밸런스 강도 7' → field='team_balance_gauge', value=7\n"
            "- '데이 필요인원 4명' → field='day_req', value=4\n"
            "- 'RN 데이 1슬롯 인원 5명' → field='manpower', nurse_class='RN', shift_slot=1, value=5\n\n"

            "⛔ 거절 예시 (update_person_attr 영역):\n"
            "- '박춘일 야간 전담' → update_person_attr (is_night_nurse)\n"
            "- '김민지 데이 고정근무' → update_person_attr (fixed_shift)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": (
                        "변경 대상 정책 필드. RosterConfig 필드명(예: max_nig_per_month, "
                        "max_conseq_work, banned_day_after_eve, day_req, team_balance_enable 등) "
                        "또는 'manpower'(ShiftManage 갱신용)."
                    ),
                },
                "value": {
                    "type": ["string", "number", "boolean"],
                    "description": "정규화된 새 값. bool 필드는 true/false, 정수 필드는 숫자.",
                },
                "nurse_class": {
                    "type": "string",
                    "description": "ShiftManage 갱신 시(field='manpower') 직군. 보통 'RN' 또는 'AN'.",
                },
                "shift_slot": {
                    "type": "integer",
                    "description": "ShiftManage 갱신 시(field='manpower') 시프트 슬롯 번호.",
                },
                "preview_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "true면 변경 미리보기만 반환(DB 미적용). 사용자 동의 후 false로 재호출.",
                },
            },
            "required": ["field", "value"],
        },
    },
    {
        "name": "update_person_attr",
        "description": (
            "간호사 개인 속성을 수정합니다. "
            "근무표(스케줄)와는 무관한 간호사 자체의 정적 데이터입니다.\n\n"
            "⚠️ 이 도구는 근무표가 없어도 호출 가능합니다.\n"
            "✅ 사용 시점: 발화에 **특정 날짜가 없고**, 대상이 **간호사 자체의 속성**(직급/팀/역할/"
            "야간전담/고정근무/수간호사/프리셉터/메모/한도/주말휴무 등)일 때.\n"
            "⛔ 사용하지 말 것: 발화에 **특정 날짜**(예: '5/3', '4월 15일')가 있고 그 날짜의 "
            "원티드/근무를 변경하는 경우 → bulk_mutation 사용.\n\n"
            "동사가 '변경/수정/지정/고정'이라도 대상이 정적 속성이면 이 도구를 쓰세요.\n\n"
            "필드 매핑 (field 파라미터에 DB 필드명 사용):\n"
            "- 직급/등급/grade → grade (정수)\n"
            "- 경력/연차 → experience (정수)\n"
            "- 직책/역할/role → role (예: 'RN', 'AN')\n"
            "- 팀/팀이동/팀변경 → team_id (정수)\n"
            "- 병동/병동이동 → group_id (문자열)\n"
            "- 수간호사/HN 지정 → is_head_nurse (true/false)\n"
            "- 야간 전담/데이 전담/N전담/시프트 전담 → is_night_nurse (시프트 코드 리스트)\n"
            "- 프리셉터 지정/멘토 지정 → preceptor_id (간호사 ID)\n"
            "- 데이 고정근무/N 고정근무 (평일만, 주말 휴무) → fixed_shift ('D'/'E'/'N'/'M'/'O', 해제는 '')\n"
            "- 주말 오프 → is_weekend_off (true/false)\n"
            "- 메모/비고 → nurse_memo (문자열)\n"
            "- 원티드 최대 횟수 → wanted_max_requests (정수)\n"
            "- 주휴 활성화 → weekly_off_enabled (true/false)\n"
            "- 주휴 요일 → weekly_off_weekday (정수 0~6, 월=0)\n"
            "- AIDE 기능 → enable_aide (true/false)\n\n"
            "⚠️ is_night_nurse는 시프트 코드의 리스트입니다 (true/false 아님).\n"
            "  • 야간 전담/N 전담 → ['N']\n"
            "  • 데이 전담/D 전담 → ['D']\n"
            "  • 이브닝 전담 → ['E']\n"
            "  • 'N 제외' / 'D와 E만' → ['D', 'E']\n"
            "  • 전담 해제 → []\n\n"
            "⚠️ team_id 변경 시: 프리셉터-프리셉티는 같은 팀이어야 합니다. "
            "이 도구가 자동으로 검사하며, 매칭이 깨지면 needs_clarification=true 응답을 반환합니다. "
            "이 경우 사용자에게 (1) 프리셉터/프리셉티 관계 해제, (2) 함께 이동, (3) 취소 중 선택을 요청하세요. "
            "임의로 진행하지 말 것.\n\n"
            "⚠️ '전담' vs '고정근무' 구분 (중요):\n"
            "  • '전담' (예: '데이 전담', 'N 전담', '야간 전담') → is_night_nurse 필드. "
            "    매일(주말 포함) 해당 시프트만 근무.\n"
            "  • '고정근무' (예: '데이 고정근무', '평일 데이 고정', '주말 휴무 + N 고정') → fixed_shift 필드. "
            "    평일만 해당 시프트, 주말은 휴무. 사용자 발화에 '평일', '주말 쉬어/휴무', '고정근무' 단어가 있으면 fixed_shift.\n"
            "  • 모호하면 ('데이로 고정해줘'만 있을 때) → 사용자에게 '전담(매일)인가요, 고정근무(평일+주말휴무)인가요?' 묻기.\n"
            "⚠️ fixed_shift 변경 시 is_weekend_off가 자동 동반 변경됩니다 "
            "(코드 설정 → True, 빈 값으로 해제 → False). 결과의 coupled_changes에서 확인 가능.\n"
            "⚠️ 'is_weekend_off (주말 휴무)' 단독 변경도 가능합니다 — fixed_shift 없이도 주말만 쉬는 인력 설정에 사용.\n\n"
            "필드별 추가 가이드:\n"
            "- group_id: 병동 식별자 문자열 (VARCHAR, 예: '101358f6de7b'). "
            "사용자가 '9A 병동'처럼 부르면 먼저 query_schedule로 그룹 ID를 조회. 사용자에게 ID 직접 묻지 말 것.\n"
            "- team_id: 정수. 사용자는 보통 '1팀', 'B팀', '문지영이 있는 팀'처럼 부름 → "
            "먼저 query_schedule로 팀 목록/멤버 조회하여 ID 매핑. 모호하면 사용자에게 후보 제시.\n"
            "- preceptor_id: 다른 간호사의 nurse_id 문자열. 이름으로 들어오면 먼저 query_schedule로 ID 조회. "
            "해제는 빈 문자열 ''.\n"
            "- role: 통상 'RN'(Registered Nurse) / 'AN'(Aide/Assistant Nurse). 그 외 코드는 사용자가 명시한 값을 그대로 전달.\n"
            "- nurse_memo: 기본 시맨틱은 **덮어쓰기(replace)**. 사용자가 '메모 추가/덧붙여'라고 명시하면 "
            "먼저 query_schedule로 기존 메모 조회 후 합쳐서 새 문자열로 전달 (LLM이 책임).\n"
            "- weekly_off_weekday: 0=월요일, 1=화, 2=수, 3=목, 4=금, 5=토, 6=일.\n"
            "- enable_aide: AIDE 자동화 기능 활성 여부. 끄면 해당 간호사는 AI 자동 배정 보조에서 제외.\n"
            "- wanted_max_requests: 사용자가 한 달간 신청 가능한 원티드 **최대 건수**(개인 한도). "
            "특정 날짜의 원티드 변경과 다름 (그건 bulk_mutation).\n\n"
            "배치 처리 (여러 간호사 동시 수정):\n"
            "- nurse_ids 파라미터에 ID 배열을 전달하면 batch.\n"
            "- '신규 간호사 모두', '프리셉티 전부', '1팀 전체' 같은 그룹 지시는: "
            "먼저 query_schedule로 대상 ID 목록 확보 → 같은 field/value로 batch 호출.\n"
            "- 결과는 affected_count + 개별 results 배열로 반환.\n\n"
            "이름→ID 그라운딩 원칙:\n"
            "- LLM이 사용자 발화의 이름/별칭을 임의로 ID로 변환하지 말 것 (동명이인 위험).\n"
            "- 항상 query_schedule을 먼저 호출하여 정확한 ID를 얻은 뒤 update_person_attr 호출.\n"
            "- 동명이인 또는 모호한 발화 시 사용자에게 confirm.\n\n"
            "다중 속성 동시 변경 (mutations 배열):\n"
            "- 한 번의 호출로 같은 간호사의 여러 필드를 동시에 변경할 수 있습니다.\n"
            "- 단일 필드: field/value 그대로 사용. 다중 필드: mutations=[{field,value},...]로 전달.\n"
            "- 처리 단위: 간호사 1명당 트랜잭션. mutations 안 하나라도 실패하면 전체 거부 (DB 미수정).\n"
            "- 처리 순서: mutations 배열 순서대로 시뮬레이션. 같은 필드가 두 번 나오면 마지막 값 적용.\n"
            "- 자동 동반(coupled_changes)은 명시 mutation에 의해 덮어쓰기 가능 "
            "(coupled_log에 overridden_by_explicit=true로 표시).\n"
            "- 의미적 모순(예: fixed_shift='D' + is_weekend_off=false)은 contradictory_state로 거부됨.\n"
            "- 결과: applied_mutations(요청 변경), coupled_changes(자동 동반), changed_fields(실제 DB 변경 필드).\n\n"
            "예시 (다중):\n"
            "- '이지영 1팀으로 이동 + 직급 3' → mutations=[{field:'team_id',value:1},{field:'grade',value:3}]\n"
            "- '한혜선 데이 고정근무 + 메모 추가' → mutations=[{field:'fixed_shift',value:'D'},{field:'nurse_memo',value:'평일 데이 고정'}]\n"
            "- (모순 케이스) '데이 고정근무인데 주말도 일하게' → 거부됨 (contradictory_state)\n\n"
            "예시 (단일):\n"
            "- '김민지 직급 3으로' → field='grade', value=3\n"
            "- '한혜선 데이 전담 고정' → field='is_night_nurse', value=['D']\n"
            "- '박춘일 야간 전담 지정' → field='is_night_nurse', value=['N']\n"
            "- '이윤지 야간 전담 해제' → field='is_night_nurse', value=[]\n"
            "- '한혜선 데이 고정근무로 (평일만, 주말 휴무)' → field='fixed_shift', value='D' "
            "(is_weekend_off=true 자동 동반)\n"
            "- '박춘일 N 고정근무 해제' → field='fixed_shift', value='' "
            "(is_weekend_off=false 자동 동반)\n"
            "- '장지예 주말 휴무만 활성화' → field='is_weekend_off', value=true (fixed_shift 없이 단독)\n"
            "- '노윤희 1팀으로 이동' → field='team_id', value=1\n"
            "- '박혜미 수간호사 지정' → field='is_head_nurse', value=true\n"
            "- '장지예 주말 오프' → field='is_weekend_off', value=true\n"
            "- '박혜미 원티드 최대 횟수 5' → field='wanted_max_requests', value=5\n"
            "- '박춘일 주휴 활성화' → field='weekly_off_enabled', value=true\n"
            "- '이다영 메모 추가' → field='nurse_memo', value='메모 내용'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nurse_name": {"type": "string"},
                "field": {
                    "type": "string",
                    "description": "단일 필드 변경 시. 다중 변경은 mutations 사용.",
                },
                "value": {
                    "description": (
                        "단일 필드 변경 시. 필드 타입에 맞는 값. is_night_nurse는 리스트 "
                        "(예: ['N'], ['D'], ['D','E'], []), boolean 필드는 true/false, "
                        "숫자 필드는 정수, 문자열 필드는 문자열."
                    ),
                },
                "mutations": {
                    "type": "array",
                    "description": (
                        "다중 속성 동시 변경. 배열 안 순서대로 시뮬레이션되어 트랜잭션으로 적용됨. "
                        "단일 필드만 바꿀 때는 field/value 사용 가능 (둘 중 하나만 사용)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "value": {},
                        },
                        "required": ["field", "value"],
                    },
                },
                "preview_only": {"type": "boolean", "default": True},
            },
            "required": ["nurse_name"],
        },
    },
    {
        "name": "generate_schedule",
        "description": (
            "병동의 **근무표를 자동 생성하는 비동기 잡**을 큐에 등록합니다. "
            "사용자가 '근무표 생성', '자동으로 짜줘', '돌려줘'처럼 신규 생성 의도를 표현할 때 사용.\n\n"

            "⚠️ **고위험·비동기**: 잡 등록 후 SQS를 통해 백그라운드에서 실행됨. 결과는 즉시 나오지 않음.\n"
            "⚠️ 사전 조건:\n"
            "  • 해당 병동의 RosterConfig가 존재해야 함 (없으면 update_constraint로 먼저 설정 필요).\n"
            "  • 같은 병동에 QUEUED/RUNNING 상태 잡이 없어야 함 (있으면 충돌 거부).\n"
            "⚠️ preview/confirm 흐름: 첫 호출은 preview_only=true로 영향 범위(year/month/config_id) 표시 후 "
            "사용자 동의를 받고, 동의 시 preview_only=false로 재호출하여 실제 잡 생성.\n\n"

            "출력:\n"
            "- preview: {preview:true, group_id, year, month, config_id, message}\n"
            "- 실제 등록: 잡 레코드 + _sqs_dispatch_required + _generation_params(year,month,config_id). "
            "실제 SQS 디스패치는 호출 레이어가 담당.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 기존 근무표 진단·교체 제안 → repair_schedule (재생성 아님)\n"
            "- 진행 중인 잡 상태 확인 → query_schedule(scope='generation_job')\n"
            "- 제약 정책 변경 후 재생성하고 싶다 → update_constraint → generate_schedule 순서\n\n"

            "예시:\n"
            "- '4월 근무표 생성' → year=2026, month=4, preview_only=true → 사용자 동의 → preview_only=false\n"
            "- '5월 근무표 다시 돌려줘' → 충돌 잡 있는지 먼저 확인(query_schedule scope='generation_job'). "
            "있으면 사용자에게 안내."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "생성 대상 연도 (예: 2026)"},
                "month": {"type": "integer", "description": "생성 대상 월 (1~12)"},
                "preview_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "true=영향 범위만 표시(잡 미등록). 사용자 동의 후 false로 재호출.",
                },
                "requester_nurse_id": {
                    "type": "string",
                    "description": "요청자 nurse_id. 미지정 시 'agent'로 기록됨.",
                },
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "update_monthly_limit",
        "description": (
            "**간호사 개인의 월 시프트 한도**(D/E/N/O × min/max/exact)를 설정/수정합니다. "
            "예: '김민지 5월 야간 4번으로 맞춰줘' / '박혜미 5월 D 최소 8회' / '이영희 5월 N 최대 5회로 제한'.\n\n"

            "⚠️ 권한: 수간호사(HN) 또는 관리자(ADM) 만 가능 (다른 간호사 한도 조정).\n"
            "⚠️ 변경 전 반드시 preview_only=true 로 영향 범위 표시 → 사용자 동의 후 preview_only=false 로 재호출.\n\n"

            "─────────── 필드 매핑 ───────────\n"
            "  • 'N 몇 번'/'야간 정확히'   → n_exact (정수)\n"
            "  • 'N 최대'/'야간 한도'      → n_max (정수)\n"
            "  • 'N 최소'/'야간 최소'      → n_min (정수)\n"
            "  • D / E / O 도 동일 패턴 (d_exact, d_min, d_max, e_*, o_*)\n\n"

            "─────────── 인접 skill 과의 경계 ───────────\n"
            "- '병동 전체 야간 최대 7회' (정책) → update_constraint (max_nig_per_month)\n"
            "- '김민지 야간 전담' (개인 속성) → update_person_attr (is_night_nurse)\n"
            "- '김민지 5월 야간 4번' (개인 월 한도) → update_monthly_limit (n_exact)\n\n"

            "예시:\n"
            "- '김민지 5월 야간 4번' → nurse_ids=['김민지의 nurse_id'], year=2026, month=5, n_exact=4\n"
            "- '박혜미 5월 D 최소 8회' → nurse_ids=[...], year=2026, month=5, d_min=8"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nurse_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "대상 간호사 ID. 보통 단일.",
                },
                "year": {"type": "integer"},
                "month": {"type": "integer"},
                "d_exact": {"type": "integer", "description": "데이 정확히 몇 번"},
                "d_min": {"type": "integer"},
                "d_max": {"type": "integer"},
                "e_exact": {"type": "integer", "description": "이브닝 정확히 몇 번"},
                "e_min": {"type": "integer"},
                "e_max": {"type": "integer"},
                "n_exact": {"type": "integer", "description": "야간 정확히 몇 번"},
                "n_min": {"type": "integer"},
                "n_max": {"type": "integer"},
                "o_exact": {"type": "integer", "description": "오프 정확히 몇 번"},
                "o_min": {"type": "integer"},
                "o_max": {"type": "integer"},
                "preview_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "true 면 미리보기, false 면 적용.",
                },
            },
            "required": ["nurse_ids", "year", "month"],
        },
    },
]
