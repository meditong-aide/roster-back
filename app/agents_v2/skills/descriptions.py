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
            "병동의 근무·인사·설정 데이터를 읽기 전용으로 조회합니다. "
            "사용자가 '보여줘 / 알려줘 / 누구야 / 몇 명이야 / 리스트업 / 어떻게 돼있어' 등 "
            "상태 확인을 묻는 모든 흐름에서 가장 먼저 진입하는 그라운딩·조회 스킬입니다.\n\n"

            "⛔ 절대 데이터를 수정/삭제하지 않습니다. 변경은 다른 스킬(bulk_mutation, "
            "update_person_attr, update_constraint, repair_schedule)이 담당합니다.\n\n"

            "─────────── scope (조회 도메인) ───────────\n"
            "scope는 '어떤 종류의 데이터를 보고 싶은가'를 의미합니다. 키워드 매칭이 아니라, "
            "사용자가 묻는 정보가 어느 데이터 도메인에 속하는지 판단해서 고르세요.\n\n"
            "- `wanted_campaign` — 원티드 캠페인 자체의 운영 메타(wanted 테이블). "
            "  마감일(exp_date), 운영 상태(status: requested/closed), 캠페인 존재 여부. "
            "  '원티드 마감 언제야', '제출 기한 언제까지', '4월 원티드 닫혔어?', "
            "  '캠페인 열려있어?', '원티드 마감일 알려줘'처럼 신청 내용·제출자가 아니라 캠페인 일정 자체를 묻는 경우. "
            "  ⚠️ '마감/기한/언제까지/열렸어/닫혔어/캠페인' 같은 운영 일정 단어가 핵심이면 이 scope. "
            "  '미제출자 명단'(누가)·'신청 내용'(무엇)과 의미적으로 다른 축임을 구분.\n"
            "- `wanted_submissions` — 제출 메타데이터 전용: 누가 제출했는지/안 했는지, 제출 시각. "
            "  '미제출자 누구야', '몇 명 제출했어', '제출 현황'처럼 제출 여부·집계만 묻는 경우. "
            "  ※ 마감일은 여기서 안 나옴 → `wanted_campaign` 으로.\n"
            "- `wanted_adjustment` — 원티드 신청 내역의 정식 조회 경로(fixed_wanted_entries 테이블). "
            "  간호사가 신청한 시프트의 날짜·코드, 수간호사 조정 결과를 모두 담고 있음. "
            "  '4월 원티드 신청 내역', '김민지 4월 원티드 뭐 냈어', '5/3 원티드 어떻게 돼있어' 등 "
            "  신청 내용(어느 날 무슨 시프트)을 묻는 모든 흐름은 여기로.\n"
            "  ※ 응답의 `source_type` 값으로 의지 분기:\n"
            "     · `original` → `shift_id`가 간호사 신청 그대로\n"
            "     · `modified` → 수간호사가 코드 변경. 간호사 원의지는 `original_shift_id` 필드\n"
            "     · `added` → 간호사 신청 없이 수간호사가 단독 추가\n"
            "     · `weekly_off` → 자동 주휴\n"
            "    사용자 발화('순수 신청만' / '조정 후 모습' / '전체')에 맞춰 자연어로 분기 응답.\n"
            "    필요 시 `source_types` 파라미터로 사전 필터링도 가능.\n"
            "- `schedule` — 생성·확정된 근무표 셀(누가 언제 무슨 근무인지). "
            "  특정 간호사·날짜·시프트 등 조건이 있는 근무표 조회에 사용. "
            "  '4/15 데이 누가야', '김민지 4월 근무', '이번 달 나이트 명단'.\n"
            # [NAV_FIRST 2026-05-29] 조건 없는 '전체 근무표 보여줘/열어줘/보러가자'(화면 이동 의도)는
            #   navigate(roster_view) 로 위임. 원복(백엔드 단독 복귀): 이 주석 + 바로 아래 ⛔ 한 줄을
            #   제거하고, NAV_FIRST 마커가 달린 다른 구간(예시 line~95, validate line~339,
            #   navigate desc line~1024/1031/1038/1049)도 함께 되돌리면 query_schedule 이 '보여줘'를 재전담.
            "  ⛔ 조건이 전혀 없는 '전체 근무표 보여줘/열어줘'는 화면 이동 의도 → navigate(roster_view) 사용.\n"
            "- `nurse_info` — 간호사 인사 정보(이름·등급·팀·직급·야간전담·고정근무·메모 등). "
            "  '김민지 정보', '신규 간호사 누구', '팀 구성', '프리셉터 매칭'.\n"
            "- `shift_definitions` — 병동의 시프트 정의(D/E/N/O/M, 한글명, 카테고리). "
            "  '시프트 종류 뭐 있어', '근무 코드'.\n"
            "- `constraint_config` — 스케줄링 제약조건 설정값(연속근무 한도, 주말 휴무 정책 등). "
            "  '현재 설정', '제약 어떻게 돼있어'.\n"
            "- `generation_job` — 최근 근무표 자동생성 작업의 상태/결과. "
            "  '생성 어디까지 됐어', '자동생성 결과'.\n\n"

            "─────────── operation (조회 의도) ───────────\n"
            "사용자가 어떤 형태의 답을 원하는지 의미적으로 판단:\n"
            "- `list` — 개별 항목들을 죽 보고 싶다 (기본값).\n"
            "- `count` — 인원수·현황·집계·제출 비율·명단 분류가 본질이다. "
            "  '몇 명', '미제출자', '아직 안 낸 사람', '제출 현황' 등 현황·분류 의도면 `count`.\n"
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
            "- '원티드 제출 마감 언제야' → scope=wanted_campaign\n"
            "- '4월 원티드 닫혔어?' → scope=wanted_campaign\n"
            "- '캠페인 기한 알려줘' → scope=wanted_campaign\n"
            "- '4월 원티드 신청 내역 보여줘' → scope=wanted_adjustment\n"
            "- '김민지 4월 원티드 뭐 냈어' → scope=wanted_adjustment, nurse_name='김민지'\n"
            "- '4월 첫째 주 김민지가 낸 원티드' → scope=wanted_adjustment, "
            "nurse_name='김민지', date_range='2026-04-01~2026-04-07'\n"
            "- '김민지 4월 근무 보여줘' → scope=schedule, nurse_name='김민지'\n"
            "- '4/15 나이트 누구야' → scope=schedule, date='2026-04-15', shift_name='나이트'\n"
            # [NAV_FIRST 2026-05-29] 조건 없는 전체 근무표 보기는 query_schedule 이 아니라 navigate.
            "- '5월 근무표 보여줘'(특정 간호사/날짜 없음) → navigate(target=roster_view) (query_schedule 아님)\n"
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
                        "wanted_campaign",
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
                        "원티드 캠페인 운영 메타(마감일·status·열림 여부)=wanted_campaign, "
                        "원티드 제출 여부·집계=wanted_submissions, "
                        "원티드 신청 내용(시프트·날짜)=wanted_adjustment, "
                        "근무표 셀=schedule, 간호사 인사정보=nurse_info, "
                        "시프트 정의=shift_definitions, 제약 설정=constraint_config, "
                        "생성잡 상태=generation_job, "
                        "개인별 월 한도 (예: '김민지 5월 N 몇 번', '5월 D 정확히 설정한 간호사')=monthly_limit"
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
                        "원티드 제출일 기준 기간(시프트 일자가 아님). "
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
            "대상이 특정 날짜의 근무가 아니라 간호사의 정적 속성이면 update_person_attr을 사용하세요.\n\n"
            "스킬 선택 가이드:\n"
            "- '5/3', '4월 15일' 같은 특정 날짜가 핵심이면 → bulk_mutation\n"
            "- '김민지 직급', '한혜선 팀', '박춘일 야간 전담' 같은 간호사 속성이면 → update_person_attr\n"
            "- '원티드 마감일', '조정판' 같은 스케줄 운영 메타면 → bulk_mutation\n\n"
            "scope별 의미:\n"
            "- 'wanted_submissions': 간호사가 제출하는 원티드 신청서 (특정 날짜의 근무 희망/회피).\n"
            "- 'wanted_adjustment': 원티드 조정판 (수간호사가 신청들을 모아 조율하는 단계).\n"
            "- 'schedule': 확정된 근무표의 시프트 셀.\n\n"
            "scope × action 유효 조합:\n"
            "- wanted_submissions: cancel / add_shift / change_shift / update_deadline / clear_deadline\n"
            "- wanted_adjustment: apply(=승인) / unapply_off\n"
            "- schedule: change_shift / add_shift / remove_shift\n\n"
            "원티드 '승인/거부'는 wanted_adjustment 로 처리한다:\n"
            "- 승인 = scope=wanted_adjustment, mutation={target_field:is_applied, target_value:1}\n"
            "- 거부 = 같은 scope, target_value:0\n"
            "- **'제출된 거 다 승인/전원 승인'** = per-nurse 필터(nurse_ids) 없이 위 mutation 한 번 →\n"
            "  스킬이 내부에서 **간호사 제출분 전체**를 대상으로 승인(조회 태스크 불필요, 단일 호출).\n\n"
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

            "⚠️ '마감일 없애/제거/삭제/해제/취소/풀어' = NULL 클리어:\n"
            "- 마감일을 해제(설정값 자체를 비우기) 의도면 반드시 action='clear_deadline'.\n"
            "- 이때 new_deadline 파라미터는 절대 채우지 말 것. 직전 값으로 되돌린다 같은 추측도 금지.\n"
            "- 즉 사용자가 '없애줘 / 다시 원래대로 / 마감일 풀어줘 / clear / 삭제' 라고 하면 임의로\n"
            "  과거 값을 골라 update_deadline 하지 말고 clear_deadline 으로 NULL 시킬 것.\n"
            "- 사용자가 '되돌려'라고만 했고 직전 값이 명백하지 않으면, 임의 추측보다 clear 우선 또는\n"
            "  사용자에게 명확화 요청.\n\n"
            "예시:\n"
            "- '5/3 원티드 취소' → scope='wanted_submissions', action='cancel', date='2026-05-03'\n"
            "- '5/15에 Oz 원티드 추가' → scope='wanted_submissions', action='add_shift', date='2026-05-15', shift_name='Oz'\n"
            "- '5/3 원티드 D를 N으로' → scope='wanted_submissions', action='change_shift', date='2026-05-03', new_shift_name='N'\n"
            "- '원티드 전체 취소' → scope='wanted_submissions', action='cancel' (date 없이)\n"
            "- '원티드 마감일 수정' → scope='wanted_submissions', action='update_deadline', new_deadline='2026-04-18'\n"
            "- '원티드 마감일 없애줘 / 그냥 없애 / 마감 풀어줘 / 마감일 삭제' → "
            "scope='wanted_submissions', action='clear_deadline' (new_deadline 미지정)\n"
            "- '조정판 쉬는사람 해제' → scope='wanted_adjustment', action='unapply_off'\n"
            "- '김민지 4/5 D→E' → scope='schedule', action='change_shift'\n\n"
            "⛔ 다음은 bulk_mutation 아님 (update_person_attr로):\n"
            "- '김민지 직급 3으로 변경' (날짜 없음 → 정적 속성)\n"
            "- '한혜선 1팀으로 이동' (특정 근무일 변경 아님)\n"
            "- '박춘일 야간 전담 지정' (간호사 자체 속성)\n"
            "- '이다영 메모 추가' (간호사 메모)\n"
            "- '박혜미 원티드 최대 횟수 5' (사용자 한도, 특정 원티드 신청 아님)\n\n"
            "⚠️ 선행조건(확정 근무표): 조회 결과에 'roster_source: 확정본(IssuedRoster)' 또는 "
            "확정/마감 상태가 보이면, 곧바로 변경하지 말고 먼저 사용자에게 확인하세요 — "
            "\"확정된 근무표입니다. 조정판을 통해 수정할까요, 아니면 확정본을 직접 수정할까요?\""
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
                        "clear_deadline",
                    ],
                    "description": (
                        "수행할 작업. 원티드 날짜별: cancel(취소), add_shift(추가), change_shift(변경). "
                        "근무표: change_shift, add_shift, remove_shift. 조정판: unapply_off, apply. "
                        "마감일: update_deadline(새 값 설정), clear_deadline(마감일 해제 — NULL)."
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
            "기존 근무표가 현재 설정된 제약조건을 위반하는지 정해진 규칙 셋으로 검사합니다.\n"
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
            # [NAV_FIRST 2026-05-29] 원복 시 아래를 "- '단순히 근무표 셀이 보고 싶다' → query_schedule(scope='schedule')" 로 되돌릴 것.
            "- '특정 간호사·날짜의 근무 셀이 보고 싶다' → query_schedule(scope='schedule'); "
            "'전체 근무표 화면을 열어보고 싶다' → navigate(roster_view)\n\n"

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
            "특정 날짜의 특정 시프트에 투입 가능한 후보 간호사를 찾아 점수순으로 추천합니다. "
            "사용자가 '대체할 사람', '누가 가능해', '빈 자리 누가 맞아', '추천해줘'처럼 "
            "한 자리(date+shift)에 대한 후보 탐색 의도일 때 사용.\n"
            "⚠️ **단 하루의 한 자리만**(언제·누구 대타). '여러 날/빈 날 전부 채우기'나 "
            "'기존 표 빈칸만 메꾸기'는 **지원하지 않는다** — 확정표를 유지한 채 빈칸만 채우는 "
            "부분 기능은 없다. 여러 날 재배치가 필요하면 generate_schedule(전체 재생성)뿐이다"
            "(재생성은 다른 근무도 다시 푼다). recommend 를 여러 날짜로 반복 호출하지 마라.\n\n"

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
            "기존 근무표를 진단해서 불균형·미충원 문제와 그 조정 제안을 생성합니다. "
            "직접 수정하지 않으며, 제안 목록만 반환.\n\n"
            "사용자가 '근무표 조정 제안', '야간 균형 맞춰줘', '문제 있으면 고칠 방법 알려줘'처럼 "
            "개선 방향 탐색 의도일 때 사용.\n\n"

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
            "근무표 또는 원티드 데이터를 분석/집계하여 리포트를 생성합니다. "
            "공정성, 시프트별 분산, 간호사별 비교, 일자별 헤드카운트, 버전 간 diff 등.\n\n"
            "사용자가 '얼마나 차이나', '평균', '분포', '비교', '공정해?', '몇 명씩 들어가있어'처럼 "
            "통계·분포·비교 의도일 때 사용.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 룰 위반 'pass/fail' 판정만 → validate_schedule\n"
            "- 개선 제안(교체쌍, 빈자리 보완) → repair_schedule\n"
            "- 단순히 데이터 행을 보고 싶다(요약 포함) → query_schedule(operation='summarize')\n"
            "- 이 스킬은 수치·분산·diff 자체가 답일 때 적합.\n\n"

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
            "병동 전체에 적용되는 스케줄링 정책을 수정합니다. "
            "특정 간호사 한 명이 아니라 다음 근무표 생성 시 모든 간호사에게 영향이 가는 규칙·인원·정책 값.\n\n"

            "⚠️ 적용 시점: 다음 근무표 생성/조정에 반영. 이미 확정된 근무표는 자동으로 바뀌지 않음.\n"
            "⚠️ 변경 전 반드시 preview_only=true로 현재 값 확인 후 사용자 동의를 거쳐 적용.\n"
            "⛔ 현재 설정값을 단순히 읽기만 하려면 query_schedule(scope='constraint_config') 사용.\n\n"

            "─────────── update_person_attr와의 경계 (가장 자주 혼동) ───────────\n"
            "결정 기준: '한 사람의 속성'인가 / '병동 전체 규칙'인가.\n"
            "- '박춘일 야간 전담' / '김민지 데이 고정근무' → 개인 속성 → update_person_attr\n"
            "- '야간 최대 7회' / '연속 근무 5일 제한' / '데이 필요인원 3명' → 병동 정책 → update_constraint\n"
            "- '김민지 원티드 한도 5건' → 개인 한도 → update_person_attr (wanted_max_requests)\n"
            "- '병동 전체 야간 균등 배분 켜줘' → 병동 정책 → update_constraint\n\n"

            "─────────── 정책 영역 (의미적 그룹) ───────────\n"
            "[A] 시프트별 필요인원·경력 (RosterConfig)\n"
            "  • day_req / eve_req / nig_req — 각 시프트 기본 필요인원 (정수)\n"
            "  • off_days — 월 오프 일수 (정수)\n"
            "  • min_exp_per_shift — 교대당 필요한 최소 경력 '연수' (정수, 예 3 = 3년차 이상). ⚠️ 사람 '수'가 아니라 '연차'.\n"
            "  • req_exp_nurses — 교대당 필요한 경력 간호사 '수' (정수). ⚠️ 위 min_exp_per_shift(연차)와 구분.\n\n"
            "[B] 연속·휴무 규칙\n"
            "  • max_nig_per_month — 월 야간 최대 횟수 (정수)\n"
            "  • max_conseq_work — 연속 근무 최대 일수 (정수)\n"
            "  • three_seq_nig — 3연속 야간 허용 여부 (bool)\n"
            "  • two_offs_after_three_nig / two_offs_after_two_nig — 야간 후 2일 휴무 (bool)\n"
            "  • two_offs_per_week — 주 2회 오프 보장 (bool)\n"
            "  • banned_day_after_eve — 이브닝 다음날 데이 금지 (bool)\n"
            "  • not_one_night — 단발성 야간(하루짜리 N) 금지 (bool)\n"
            "  • nod_noe — 야간 다음 데이/이브닝(N→O→D/E) 패턴 최소화 (bool)\n"
            "  • sequential_offs — 오프 연속 배치 선호 (bool)\n"
            "  • even_nights — 야간 균등 배분 (bool)\n\n"
            "[C] 구조 정책\n"
            "  • preceptee_on — 프리셉터-프리셉티 매칭 활성 (bool)\n"
            "  • preceptee_shift_count — 프리셉티를 시프트 필요인원 카운트에 포함 (bool, preceptee_on=true 일 때만 유효)\n\n"
            "[D] 시프트 슬롯별 인원 (ShiftManage) — 슬롯 단위 미세 조정\n"
            "  • field='manpower' + nurse_class('RN'/'AN') + shift_slot(정수) + value=정수\n"
            "  • 사용자가 'RN 데이 슬롯 인원 4명으로'처럼 슬롯을 특정할 때.\n\n"

            "─────────── 그라운딩 ───────────\n"
            "사용자의 자연어를 위 영역 [A]~[D] 중 어디에 속하는지 의미적으로 판단하고, "
            "DB 필드명을 `field`에, 정규화 값을 `value`에 넣으세요. "
            "bool 필드의 '켜줘/허용/적용' → true, '꺼줘/금지/해제' → false. "
            "필드명이 모호한 표현('야간 최대'='max_nig_per_month' vs '야간 필요인원'='nig_req')은 "
            "사용자에게 의미를 한 번 더 확인.\n"
            "⚠️ 혼합 발화 처리(B3 후속, 2026-06-01): 사용자가 한 메시지에서 "
            "'어떻게 설정하는지 모르겠어' (meta) + '11개로 해줘봐' (요청) 처럼 두 절을 "
            "함께 보낼 때, 요청 절('해줘'/'바꿔')에 우선순위를 두고 바로 update_constraint 호출. "
            "'수정 도구가 연결돼 있지 않습니다' 같은 false claim 금지 — 이 스킬이 바로 수정 도구.\n\n"

            "─────────── 예시 ───────────\n"
            "- '야간 최대 7회로' → field='max_nig_per_month', value=7\n"
            "- '연속 근무 5일 제한' → field='max_conseq_work', value=5\n"
            "- '이브닝 다음날 데이 금지 해제' → field='banned_day_after_eve', value=false\n"
            "- '데이 필요인원 4명' → field='day_req', value=4\n"
            "- '시프트당 3년차 이상 경력자 필수' → field='min_exp_per_shift', value=3\n"
            "- '교대마다 경력 간호사 2명은 있어야 해' → field='req_exp_nurses', value=2\n"
            "- '단발 나이트 금지' → field='not_one_night', value=true\n"
            "- 'RN 데이 1슬롯 인원 5명' → field='manpower', nurse_class='RN', shift_slot=1, value=5\n\n"

            "⛔ 거절 예시 (update_person_attr 영역):\n"
            "- '박춘일 야간 전담' → update_person_attr (allowed_shifts)\n"
            "- '김민지 데이 고정근무' → update_person_attr (fixed_shift)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": (
                        "변경 대상 정책 필드. RosterConfig 필드명(예: max_nig_per_month, "
                        "max_conseq_work, banned_day_after_eve, day_req 등) "
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
            "✅ 사용 시점: 발화에 특정 날짜가 없고, 대상이 간호사 자체의 속성(직급/팀/역할/"
            "야간전담/고정근무/수간호사/프리셉터/메모/한도/주말휴무 등)일 때.\n"
            "⛔ 사용하지 말 것: 발화에 특정 날짜(예: '5/3', '4월 15일')가 있고 그 날짜의 "
            "원티드/근무를 변경하는 경우 → bulk_mutation 사용.\n\n"
            "동사가 '변경/수정/지정/고정'이라도 대상이 정적 속성이면 이 도구를 쓰세요.\n\n"
            "필드 매핑 (field 파라미터에 DB 필드명 사용):\n"
            "- 직급/등급/grade → grade (정수)\n"
            "- 경력/연차 → experience (정수)\n"
            "- 직책/역할/role → role (예: 'RN', 'AN')\n"
            "- 팀/팀이동/팀변경 → team_id (정수 ID 또는 팀 이름 문자열; 스킬이 내부 매핑)\n"
            "  ⛔ **병동이동/병동 옮기기/소속 병동 변경은 이 스킬 아님** → manage_assignment(kind=병동이동)로 처리. "
            "여긴 group_id 를 직접 바꾸지 마라(월 발효·팀/등급 이관을 못 함).\n"
            "- 수간호사/HN 지정 → is_head_nurse (true/false)\n"
            "- 야간 전담/데이 전담/N전담/시프트 전담 → allowed_shifts (시프트 코드 리스트)\n"
            "- 프리셉터 지정/멘토 지정 → preceptor_id (간호사 ID)\n"
            "- 데이 고정근무/N 고정근무 (평일만, 주말 휴무) → fixed_shift "
            "(코드 'D'/'E'/'N'/'M'/'O' 또는 한글 '데이'/'이브닝'/'나이트'/'미드'/'오프'; "
            "스킬이 내부 매핑. 해제는 '')\n"
            "- 주말 오프 → is_weekend_off (true/false)\n"
            "- 메모/비고 → nurse_memo (문자열)\n"
            "- 원티드 최대 횟수 → wanted_max_requests (정수)\n"
            "- 주휴 활성화 → weekly_off_enabled (true/false)\n"
            "- 주휴 요일 → weekly_off_weekday (정수 0~6, 월=0)\n"
            "- AIDE 기능 → enable_aide (true/false)\n"
            "- 퇴사/퇴사 처리/퇴직 → resignation_date (날짜 'YYYY-MM-DD'). "
            "⚠️ 퇴사 처리는 반드시 퇴사일이 필요합니다. 날짜가 없으면 스킬이 되묻습니다. "
            "퇴사 취소/해제는 value='해제'.\n"
            "  (※ '명단에서 삭제/근무자 삭제'는 별개 — navigate(nurse_management)로 안내. "
            "퇴사 처리는 재직 기록에 퇴사일을 남겨 이후 근무표 생성에서 자동 제외됩니다.)\n\n"
            "⚠️ allowed_shifts는 시프트 코드의 리스트입니다 (true/false 아님).\n"
            "  • 야간 전담/N 전담 → ['N']\n"
            "  • 데이 전담/D 전담 → ['D']\n"
            "  • 이브닝 전담 → ['E']\n"
            "  • 'N 제외' / 'D와 E만' → ['D', 'E']\n"
            "  • 전담 해제 → []\n\n"
            "⚠️ team_id 변경 시: 프리셉터-프리셉티는 같은 팀이어야 합니다. "
            "이 도구가 자동으로 검사하며, 매칭이 깨지면 needs_clarification=true 응답을 반환합니다. "
            "이 경우 사용자에게 (1) 프리셉터/프리셉티 관계 해제, (2) 함께 이동, (3) 취소 중 선택을 요청하세요. "
            "임의로 진행하지 말 것.\n"
            "✅ 사용자 선택 후 묶음 confirm 원칙(B3, 2026-06-01): 옵션 1/2 모두 두 간호사를 동시에 수정해야 하므로 "
            "(옵션 1=프리셉터 해제 + 본인 팀 이동 / 옵션 2=본인 팀 이동 + 짝꿍 팀 이동) "
            "두 update_person_attr 호출의 preview 를 한 turn 안에 함께 제시하고 사용자에게 한 번의 confirm 만 요청. "
            "예: '김예빈 프리셉터 관계 해제 + 이유림 A팀 이동 — 진행할까요?' "
            "각 변경마다 별도 preview/confirm 으로 쪼개지 말 것(불필요한 turn 증가 + 일관성 손실).\n\n"
            "⚠️ '전담' vs '고정근무' 구분 (중요):\n"
            "  • '전담' (예: '데이 전담', 'N 전담', '야간 전담') → allowed_shifts 필드. "
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
            "- team_id: 정수 ID 또는 팀 이름 문자열 (예: '1팀', 'A팀', 'B팀'). "
            "팀 이름은 스킬이 내부에서 team_id 로 매핑합니다 — query_schedule 선조회 불필요. "
            "이름이 모호하거나 없으면 스킬이 needs_clarification 으로 후보 반환. "
            "단, '문지영이 있는 팀'처럼 멤버 기반 지칭은 먼저 query_schedule 로 팀 식별 필요.\n"
            "- preceptor_id: 다른 간호사의 nurse_id 문자열. 이름으로 들어오면 먼저 query_schedule로 ID 조회. "
            "해제는 빈 문자열 ''.\n"
            "- role: 통상 'RN'(Registered Nurse) / 'AN'(Aide/Assistant Nurse). 그 외 코드는 사용자가 명시한 값을 그대로 전달.\n"
            "- nurse_memo: 기본 시맨틱은 덮어쓰기(replace). 사용자가 '메모 추가/덧붙여'라고 명시하면 "
            "먼저 query_schedule로 기존 메모 조회 후 합쳐서 새 문자열로 전달 (LLM이 책임).\n"
            "- weekly_off_weekday: 0=월요일, 1=화, 2=수, 3=목, 4=금, 5=토, 6=일.\n"
            "- enable_aide: AIDE 자동화 기능 활성 여부. 끄면 해당 간호사는 AI 자동 배정 보조에서 제외.\n"
            "- wanted_max_requests: 사용자가 한 달간 신청 가능한 원티드 최대 건수(개인 한도). "
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
            "- '한혜선 데이 전담 고정' → field='allowed_shifts', value=['D']\n"
            "- '박춘일 야간 전담 지정' → field='allowed_shifts', value=['N']\n"
            "- '이윤지 야간 전담 해제' → field='allowed_shifts', value=[]\n"
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
                        "단일 필드 변경 시. 필드 타입에 맞는 값. allowed_shifts는 리스트 "
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
            "병동의 근무표를 자동 생성하는 비동기 잡을 큐에 등록합니다. "
            "사용자가 '근무표 생성', '자동으로 짜줘', '돌려줘'처럼 신규 생성 의도를 표현할 때 사용.\n\n"

            "⚠️ 고위험·비동기: 잡 등록 후 SQS를 통해 백그라운드에서 실행됨. 결과는 즉시 나오지 않음. "
            "보통 15~30초 정도 소요됨 — 잡 등록 직후 사용자에게 예상 소요 시간(약 15~30초)을 자연스럽게 안내하고, "
            "사용자가 '어떻게 됐어?'/'결과 보여줘' 등으로 물으면 get_job_status (또는 query_schedule scope='generation_job')로 확인할 것.\n"
            "⚠️ 사전 조건:\n"
            "  • 해당 병동의 RosterConfig가 존재해야 함 (없으면 update_constraint로 먼저 설정 필요).\n"
            "  • 같은 병동에 QUEUED/RUNNING 상태 잡이 없어야 함 (있으면 충돌 거부).\n"
            "⚠️ preview/confirm 흐름: 첫 호출은 preview_only=true로 영향 범위(year/month/config_id) 표시 후 "
            "사용자 동의를 받고, 동의 시 preview_only=false로 재호출하여 실제 잡 생성.\n\n"

            "출력:\n"
            "- preview: {preview:true, year, month, message, _internal:{group_id, config_id}}\n"
            "- 실제 등록: {year, month, status:'queued', message, _internal:{job_id, sqs_dispatch_required, generation_params}}. "
            "실제 SQS 디스패치는 호출 레이어가 _internal 을 읽어 처리.\n\n"

            "⚠️ 사용자에게 절대 노출 금지: job_id / sqs / config_id / _internal.* 같은 시스템 식별자. "
            "사용자 응답은 반드시 message 필드의 자연어만 활용 — '근무표 생성 시작했어요. 완료되면 알려드릴게요.' "
            "처럼 자연스럽게. ID 나 status 코드(QUEUED/RUNNING)를 한국어 답변에 그대로 넣지 말 것.\n\n"

            "🔎 결과 추적 (FAILED 시): 잡이 FAILED 상태면 get_job_status (또는 query_schedule scope='generation_job') "
            "의 결과에 `infeasibility` 필드가 포함됨. 이 필드에는:\n"
            "  • summary_ko: 한 줄 요약\n"
            "  • problems[]: 왜 못 만들었는지 (한국어 문장 리스트)\n"
            "  • actions[]: 어떻게 해결할지 (한국어 문장 리스트)\n"
            "  • trade_offs[]: 각 해결책의 부작용\n"
            "  • hard_case: 어려운 케이스 분류 (true면 운영자 검토 권장)\n"
            "  • apply_hint: 재시도용 설정 변경 힌트\n"
            "사용자에게 답할 때는 narrative 필드를 활용해 '왜 실패했고 어떻게 풀 수 있는지' 자연스럽게 설명할 것.\n\n"

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
            "간호사 개인의 월 시프트 한도(D/E/N/O × min/max/exact)를 설정/수정합니다. "
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
            "- '김민지 야간 전담' (개인 속성) → update_person_attr (allowed_shifts)\n"
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
    {
        "name": "manage_grade",
        "description": (
            "병동의 등급(grade)별 근무 정책을 조회·수정합니다. 등급은 간호사의 역량 레벨이며, "
            "근무표 생성 시 '특정 시프트에 특정 등급을 몇 명 배치할지'를 결정합니다.\n\n"

            "─────────── 무엇을 다루나 ───────────\n"
            "1) 등급별 최소 인원 — '나이트에 시니어 최소 2명'처럼 시프트마다 등급별 하한.\n"
            "2) 등급별 최대 인원(anti-pair) — '야간에 1년차는 최대 1명'처럼 상한.\n"
            "3) 제약 완화(soft) 여부 — 등급 정원 때문에 근무표가 안 짜질 때 완화할지(soft) "
            "엄격히 지킬지(hard). '등급 때문에 표가 안 나오면 좀 느슨하게' → 완화. '등급 꼭 지켜줘' → 엄격.\n"
            "4) 등급 이름 — 등급 번호에 표시 이름 부여. '1등급을 주니어로'.\n\n"

            "⚠️ 적용 시점: 다음 근무표 생성부터 반영. 이미 확정된 근무표는 자동으로 바뀌지 않음.\n"
            "⚠️ 변경(set_*)은 preview_only=true(기본)로 먼저 미리보기 → 사용자 동의 후 적용.\n"
            "⛔ 조회(read)를 포함한 모든 작업이 수간호사(HN)·관리자(ADM) 전용입니다. "
            "등급(역량) 정보는 일반 간호사에게 노출되어선 안 되는 민감 정보이므로 권한 없는 요청은 거부됩니다.\n"
            "⛔ 사용자에게 등급 '번호'나 내부 JSON 을 노출하지 마세요. 항상 등급 '이름'으로 말하세요. "
            "등급 이름이 설정돼 있지 않으면 스킬이 재질의합니다.\n"
            "⛔ 아직 등급에 위계(순서)가 없습니다. '맨 위 등급', '제일 높은 등급' 같은 표현은 "
            "임의로 해석하지 말고, 어떤 등급인지 사용자에게 되물으세요.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 개인의 야간전담/고정근무 등 한 사람 속성 → update_person_attr\n"
            "- 시프트 전체 필요인원(day_req 등)·연속근무·팀밸런스 등 등급 무관 정책 → update_constraint\n"
            "- 단순 현재 등급 설정 조회만 → operation='read'\n\n"

            "─────────── operation 별 파라미터 ───────────\n"
            "[read] 현재 등급 정책 조회 (파라미터 없음).\n"
            "[set_requirement] 등급별 인원 설정. shift_name(자연어 '나이트/데이/이브닝/미드') + "
            "grade_name(등급 이름, 예 '시니어') + min_count 와/또는 max_count.\n"
            "  · '최소 인원 제한 없애줘' → min_count=0.  '최대 인원 제한 없애줘' → max_count=-1.\n"
            "[set_soft_fallback] soft_enabled=true(완화) / false(엄격).\n"
            "[set_grade_name] target_grade_name(기존 이름 또는 번호) + new_name.\n\n"

            "─────────── 예시 (입력 → operation/파라미터) ───────────\n"
            "- '나이트에 시니어 최소 2명은 꼭 넣어줘' → set_requirement, shift_name='나이트', grade_name='시니어', min_count=2\n"
            "- '데이에 신규 한 명은 있어야 해' → set_requirement, shift_name='데이', grade_name='신규', min_count=1\n"
            "- '야간에 1년차는 최대 1명만' → set_requirement, shift_name='나이트', grade_name='1년차', max_count=1\n"
            "- '나이트 시니어 최소 2명, 최대 3명' → set_requirement, shift_name='나이트', grade_name='시니어', min_count=2, max_count=3\n"
            "- '등급 때문에 근무표가 안 짜지면 좀 느슨하게 해줘' → set_soft_fallback, soft_enabled=true\n"
            "- '등급 제약 꼭 지켜줘' → set_soft_fallback, soft_enabled=false\n"
            "- '등급별 인원 설정 어떻게 돼있어?' → read\n"
            "- '1등급을 주니어로 바꿔줘' → set_grade_name, target_grade_name='1', new_name='주니어'\n"
            "- '맨 위 등급한테 나이트 몰아줘' → (위계 없음) 어떤 등급인지 사용자에게 되물을 것\n"
            "- '시니어 좀 더 넣어줘' → (수량 모호) 몇 명으로 할지 되물을 것"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "set_requirement", "set_soft_fallback", "set_grade_name"],
                    "description": "수행할 작업. 조회=read, 인원 설정=set_requirement, 완화 토글=set_soft_fallback, 이름 설정=set_grade_name.",
                },
                "shift_name": {
                    "type": "string",
                    "description": "set_requirement 시 대상 근무. 자연어 '데이/이브닝/나이트/미드'.",
                },
                "grade_name": {
                    "type": "string",
                    "description": "set_requirement 시 대상 등급의 이름(예 '시니어'). 등급 번호도 가능.",
                },
                "min_count": {
                    "type": "integer",
                    "description": "등급별 최소 인원. 제한 해제는 0.",
                },
                "max_count": {
                    "type": "integer",
                    "description": "등급별 최대 인원(anti-pair). 제한 없음은 -1.",
                },
                "soft_enabled": {
                    "type": "boolean",
                    "description": "set_soft_fallback 시 true=완화(soft) / false=엄격(hard).",
                },
                "target_grade_name": {
                    "type": "string",
                    "description": "set_grade_name 시 이름을 바꿀 대상 등급(기존 이름 또는 번호).",
                },
                "new_name": {
                    "type": "string",
                    "description": "set_grade_name 시 새 표시 이름.",
                },
                "preview_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "true 면 변경 미리보기만(DB 미적용). 사용자 동의 후 false 로 적용.",
                },
            },
            "required": ["operation"],
        },
    },
    {
        "name": "manage_team_min",
        "description": (
            "팀별 시프트 최소 인원(특정 팀이 각 근무에 매일 최소 몇 명)을 조회·수정합니다. "
            "예: 'A팀은 나이트에 최소 2명은 있어야 해'.\n\n"

            "─────────── 무엇을 다루나 ───────────\n"
            "- 팀별 시프트 최소 인원 조회 (operation='read')\n"
            "- 팀별 시프트 최소 인원 설정 (operation='set_min')\n"
            "- 특정 시프트(또는 팀 전체) 최소 인원 해제 (operation='clear_min')\n\n"

            "⚠️ 적용 시점: 다음 근무표 생성부터 반영.\n"
            "⚠️ 변경(set_min/clear_min)은 preview_only=true(기본)로 미리보기 → 사용자 동의 후 적용.\n"
            "⛔ 조회(read) 포함 모든 작업이 수간호사(HN)·관리자(ADM) 전용입니다. 권한 없는 요청은 거부됩니다.\n"
            "⛔ 사용자에게 팀 내부 id·JSON 을 노출하지 마세요. 항상 팀 '이름'으로 말하세요.\n\n"

            "─────────── 인접 스킬과의 경계 (혼동 주의) ───────────\n"
            "- '간호사를 어느 팀에 배정/이동' → 팀 멤버 관리(이 스킬 아님).\n"
            "- 등급별 최소 인원 → manage_grade.\n"
            "- 시프트 전체 필요인원(병동 day_req 등) → update_constraint.\n"
            "- 팀별 최소 인원 조회·설정만 이 스킬.\n\n"

            "─────────── 그라운딩 ───────────\n"
            "- 팀은 이름 그대로 team_name 에 ('A팀','1팀'). 스킬이 내부에서 팀을 찾습니다. 못 찾으면 재질의.\n"
            "- 시프트는 자연어 shift_name 에 ('데이/이브닝/나이트/미드') → 내부 D/E/N/M 변환.\n"
            "- 한 번에 한 팀씩. '전 팀 일괄'은 팀 목록을 먼저 read 한 뒤 팀별로 처리.\n\n"

            "─────────── 예시 ───────────\n"
            "- 'A팀은 데이에 최소 2명' → set_min, team_name='A팀', shift_name='데이', min_count=2\n"
            "- '1팀 나이트 최소 1명으로' → set_min, team_name='1팀', shift_name='나이트', min_count=1\n"
            "- 'B팀 이브닝 최소 인원 제한 없애줘' → clear_min, team_name='B팀', shift_name='이브닝'\n"
            "- 'A팀 최소 인원 다 풀어줘' → clear_min, team_name='A팀' (shift_name 생략 = 전체 해제)\n"
            "- '팀별 최소 인원 어떻게 돼있어?' → read\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "set_min", "clear_min"],
                    "description": "조회=read, 최소 인원 설정=set_min, 해제=clear_min.",
                },
                "team_name": {
                    "type": "string",
                    "description": "대상 팀 이름 (예 'A팀', '1팀').",
                },
                "shift_name": {
                    "type": "string",
                    "description": "대상 근무 자연어 ('데이/이브닝/나이트/미드'). clear_min 에서 생략하면 팀 전체 해제.",
                },
                "min_count": {
                    "type": "integer",
                    "description": "set_min 시 최소 인원 (0 이상 정수).",
                },
                "preview_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "true 면 변경 미리보기만(DB 미적용). 사용자 동의 후 false 로 적용.",
                },
            },
            "required": ["operation"],
        },
    },
    {
        "name": "query_generation_job",
        "description": (
            "근무표 자동 생성 작업(job) 의 최신 상태를 조회합니다 (읽기 전용). "
            "예: '근무표 생성 어디까지?', '생성 됐어?', '마지막 생성 결과'.\n\n"

            "─────────── 무엇을 다루나 ───────────\n"
            "- generate_schedule 로 시작한 job 의 status (QUEUED/RUNNING/SUCCESS/FAILED) + progress + 시각\n"
            "- 그룹 단위로 가장 최근 1건 만 반환\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 새 근무표 생성을 트리거 → generate_schedule.\n"
            "- 생성된 근무표 내용 조회 → query_schedule.\n"
            "- 본 스킬은 'job 진행 상태'에 한정. infeasibility/제약 분석은 validate_schedule.\n\n"

            "─────────── 그라운딩 ───────────\n"
            "- group_id 는 세션 컨텍스트에서 자동 주입. 사용자가 별도 식별자 입력 불필요.\n\n"

            "─────────── 예시 ───────────\n"
            "- '근무표 생성 어디까지?' → query_generation_job\n"
            "- '생성 끝났어?' → query_generation_job\n"
            "- '마지막 job 상태' → query_generation_job\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "office_id": {
                    "type": "string",
                    "description": "선택. 그룹 외 추가 스코프 필요 시.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "manage_wanted_deadline",
        "description": (
            "원티드(간호사 희망 근무) 요청의 마감일 변경 또는 즉시 마감. "
            "예: '7월 원티드 마감일 7월 10일로', '이번 달 원티드 마감해줘'.\n\n"

            "─────────── 무엇을 다루나 ───────────\n"
            "- 현재 상태/마감일 조회 (operation='read')\n"
            "- 마감일 변경 (operation='set_deadline')\n"
            "- 즉시 마감 (operation='close')\n\n"

            "⚠️ 변경/마감은 preview_only=true(기본)로 미리보기 → 사용자 동의 후 적용.\n"
            "⛔ 수간호사(HN)·관리자(ADM) 전용. 권한 없는 요청은 거부됩니다.\n"
            "⛔ 이미 마감(closed)된 원티드는 마감일 변경 불가.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 개별 원티드 항목 승인/거부 → bulk_mutation (scope=wanted).\n"
            "- 원티드 한도/연간 정책 → update_constraint.\n"
            "- 원티드 제출 현황 조회 → query_schedule (scope=wanted_submissions).\n"
            "- 본 스킬은 '마감일/마감 상태'에 한정.\n\n"

            "─────────── 그라운딩 ───────────\n"
            "- year/month 는 필수. '이번 달'/'다음 달' 은 호출부에서 해석.\n"
            "- exp_date 는 'YYYY-MM-DD'. None/빈문자열 → '마감일 없음'으로 해석.\n\n"

            "─────────── 예시 ───────────\n"
            "- '7월 원티드 마감일 7월 10일로' → set_deadline, year=2026, month=7, exp_date='2026-07-10'\n"
            "- '이번 달 원티드 마감일 없애줘' → set_deadline, exp_date=null\n"
            "- '7월 원티드 마감해' → close, year=2026, month=7\n"
            "- '7월 원티드 상태 보여줘' → read, year=2026, month=7\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "set_deadline", "close"],
                    "description": "조회=read, 마감일 변경=set_deadline, 즉시 마감=close.",
                },
                "year": {
                    "type": "integer",
                    "description": "대상 연도 (예 2026).",
                },
                "month": {
                    "type": "integer",
                    "description": "대상 월 (1~12).",
                },
                "exp_date": {
                    "type": ["string", "null"],
                    "description": "set_deadline 시 새 마감일 ('YYYY-MM-DD'). null/빈문자열 = '마감일 없음'.",
                },
                "preview_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "true 면 변경 미리보기만(DB 미적용). 사용자 동의 후 false 로 적용.",
                },
            },
            "required": ["operation", "year", "month"],
        },
    },
    {
        "name": "manage_teams",
        "description": (
            "팀 자체의 라이프사이클 (추가·이름변경·삭제·단일 조회) 을 다룹니다. "
            "예: 'A팀 추가', 'B팀 이름을 신생아실로', 'C팀 삭제', 'A팀 멤버 누구야?'.\n\n"

            "─────────── 무엇을 다루나 ───────────\n"
            "- 팀 목록 + 멤버 조회 (operation='read')\n"
            "- 새 팀 추가 (operation='add', team_name)\n"
            "- 팀 이름 변경 (operation='rename', team_name → new_name)\n"
            "- 팀 삭제 (operation='delete', team_name)\n\n"

            "⚠️ 변경(add/rename/delete)은 preview_only=true(기본)로 미리보기 → 사용자 동의 후 적용.\n"
            "⛔ 수간호사(HN)·관리자(ADM) 전용 mutation. 권한 없는 요청은 거부됩니다.\n"
            "⛔ 사용자에게 internal team_id 노출 금지. 항상 team_name 으로 말하세요.\n\n"

            # [NAV_FIRST_TEAMS 2026-06-19] 조건 없는 목록은 navigate 가 우선. 원복 시 이 줄 제거.
            "─────────── nav-first carve ───────────\n"
            "- 무필터 목록 요청('팀 목록 보여줘', '우리 병동 팀 어떻게 돼있어?')은 navigate(nurse_management/team_setting) 가 우선. "
            "본 스킬은 단일 팀 조회('A팀 멤버 누구야?')·mutation(추가/이름변경/삭제)에 사용하세요.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 팀별 최소 인원 ('A팀 나이트 최소 2명') → manage_team_min.\n"
            "- 간호사를 어느 팀에 배정/이동 → update_person_attr (team_id 변경).\n"
            "- 등급별 인원 → manage_grade.\n"
            "- 팀 자동 분배·재분배 (수십명 대상 일괄) → UI 흐름 유지 권장(향후 별도 스킬).\n"
            "- 본 스킬은 '팀 자체의 라이프사이클'에 한정.\n\n"

            "─────────── 그라운딩 ───────────\n"
            "- team_name 은 사용자가 말한 그대로 ('A팀','1팀','신생아실'). 스킬이 내부에서 매칭.\n"
            "- 못 찾으면 needs_clarification 으로 팀 목록 제시.\n\n"

            "─────────── 예시 ───────────\n"
            "- 'A팀 멤버 누구야?' → read (단일 팀 확인)\n"
            "- '신생아실 팀 추가해' → add, team_name='신생아실'\n"
            "- 'A팀 이름을 응급실로 바꿔줘' → rename, team_name='A팀', new_name='응급실'\n"
            "- 'C팀 없애줘' → delete, team_name='C팀'\n"
            # [NAV_FIRST_TEAMS 2026-06-19]
            "- (참고) '팀 목록 보여줘' 같은 무필터 목록은 본 스킬이 아니라 navigate 가 처리.\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "add", "rename", "delete"],
                    "description": "조회=read, 추가=add, 이름변경=rename, 삭제=delete.",
                },
                "team_name": {
                    "type": "string",
                    "description": "대상 팀 이름 (add 에선 새 이름, rename/delete 에선 기존 이름).",
                },
                "new_name": {
                    "type": "string",
                    "description": "rename 시 새 이름.",
                },
                "preview_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "true 면 변경 미리보기만(DB 미적용). 사용자 동의 후 false 로 적용.",
                },
            },
            "required": ["operation"],
        },
    },
    {
        "name": "manage_wanted_limits",
        "description": (
            "원티드(간호사 희망 근무) 한도 초과 처리. "
            "예: '원티드 한도 넘은 사람 누구야?', '박지은 원티드 초과분 정리해줘'.\n\n"

            "─────────── 무엇을 다루나 ───────────\n"
            "- 한도 초과자 목록 조회 (operation='list_over_limit')\n"
            "- 특정 간호사의 초과분 OFF 삭제 (operation='delete_excess_off', preview/apply)\n\n"

            "⚠️ delete_excess_off 는 preview_only=true(기본)로 미리보기 → 사용자 동의 후 적용.\n"
            "⛔ 수간호사(HN)·관리자(ADM) 전용 mutation. 권한 없는 요청은 거부됩니다.\n"
            "⛔ 사용자에게 internal nurse_id 노출 금지. 이름으로 말하세요.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 원티드 마감일 변경/즉시 마감 → manage_wanted_deadline.\n"
            "- 원티드 한도(개인별 wanted_max_requests) 정책 자체 변경 → update_person_attr.\n"
            "- 원티드 제출 현황 조회 → query_schedule (scope=wanted_submissions).\n"
            "- 본 스킬은 '한도 초과 → 정리'에 한정.\n\n"

            "─────────── 그라운딩 ───────────\n"
            "- year/month 는 필수. '이번 달'/'다음 달' 은 호출부에서 해석.\n"
            "- nurse_id 가 필요한 경우 사용자 이름은 query_schedule(scope=nurses) 로 먼저 매핑.\n\n"

            "─────────── 예시 ───────────\n"
            "- '7월 원티드 한도 넘은 사람' → list_over_limit, year=2026, month=7\n"
            "- '박지은 7월 원티드 초과분 정리' → delete_excess_off, nurse_id=...,  preview→confirm\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list_over_limit", "delete_excess_off"],
                    "description": "목록=list_over_limit, 정리=delete_excess_off.",
                },
                "year": {"type": "integer", "description": "대상 연도."},
                "month": {"type": "integer", "description": "대상 월 (1~12)."},
                "nurse_id": {
                    "type": "string",
                    "description": "delete_excess_off 시 대상 간호사 식별자.",
                },
                "nurse_name": {
                    "type": "string",
                    "description": "delete_excess_off — UI 응답용 라벨 (없으면 nurse_id 노출).",
                },
                "preview_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "true 면 변경 미리보기만(DB 미적용). 사용자 동의 후 false 로 적용.",
                },
            },
            "required": ["operation", "year", "month"],
        },
    },
    {
        "name": "resolve_infeasibility",
        "description": (
            "근무표 자동 생성이 실패했을 때, 어떤 변경을 하면 풀 수 있는지 해결 옵션 카탈로그를 보여줍니다. "
            "예: '이번 실패 어떻게 풀어?', '7월 근무표 실패 해결 방법 알려줘', '원인 알겠고 옵션 뭐 있어?'.\n\n"

            "─────────── 무엇을 다루나 ───────────\n"
            "- 최근 FAILED 생성 job 의 솔버 unrecoverable payload 를 읽어 "
            "해결 옵션(action_levers) + 부작용(trade_offs) 카탈로그를 한국어로 노출.\n"
            "- 각 옵션은 어떤 설정 키(config_key)를 어느 방향(direction)으로 조절할지 명시.\n"
            "- 적용 자체는 본 스킬이 아니라 후속 mutation 스킬(manage_team_min/manage_grade/"
            "update_constraint 등)에 LLM 이 chain 으로 위임.\n\n"

            "⛔ read-only 스킬. DB 변경 없음.\n"
            "⛔ 사용자에게 raw enum / treatment_id / job_id 노출 금지. 한국어 rationale 만.\n\n"

            "─────────── 인접 스킬과의 경계 ───────────\n"
            "- 진행 상태/실패 사유 한 줄 요약 → query_generation_job.\n"
            "- 옵션 선택 후 실제 변경 → manage_team_min(팀 최소인원) / manage_grade(등급) / "
            "update_constraint(제약) / update_monthly_limit(월 한도) 등.\n"
            "- 검증/교정 흐름은 validate_schedule / repair_schedule.\n"
            "- 본 스킬은 '실패 → 옵션 카탈로그' 단계에만 한정.\n\n"

            "─────────── 그라운딩 ───────────\n"
            "- year/month 지정 시 그 달의 가장 최근 FAILED job 대상. 미지정 시 그룹의 가장 최근 job.\n"
            "- 옵션 목록이 비어있으면 '실패 원인부터 확인 필요' 라고 안내.\n\n"

            "─────────── 예시 ───────────\n"
            "- '이번 실패 어떻게 풀어?' → operation='list_options' (year/month 생략 → 최근 job)\n"
            "- '7월 근무표 실패 해결 옵션 알려줘' → operation='list_options', year=2026, month=7\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list_options"],
                    "description": "현재는 옵션 카탈로그 조회(list_options)만 지원.",
                },
                "year": {
                    "type": "integer",
                    "description": "특정 month 의 FAILED job 식별 시 사용. month 와 함께. 미지정 시 가장 최근 job.",
                },
                "month": {
                    "type": "integer",
                    "description": "특정 month 의 FAILED job 식별 시 사용. year 와 함께. 미지정 시 가장 최근 job.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "navigate",
        "description": (
            "사용자를 특정 화면/섹션으로 이동시킵니다 (프론트 화면 전환). "
            "'팀 어디서 바꿔?', '원티드 어디서 봐?', '등급 설정 띄워줘', '근무표 만들러 가자', "
            "'대시보드 보여줘'처럼 위치를 묻거나 화면 이동을 원하는 의도에 사용하세요.\n"
            # [NAV_FIRST 2026-05-29] 조건 없는 '근무표 보여줘'도 화면 이동 의도로 보고 roster_view 로.
            "특히 특정 간호사/날짜 조건 없이 '근무표 보여줘 / 5월 근무표 보여줘 / 근무표 보러가자'처럼 "
            "전체 근무표를 보고 싶다는 흐름은 query_schedule 이 아니라 navigate(target=roster_view) 입니다.\n\n"

            "⚠️ 데이터를 텍스트로 답하는 것과 다릅니다 — 이 도구는 실제 화면을 옮깁니다. "
            "조회 결과 자체가 필요하면 query_schedule 을 쓰고, '어디서/어디로/띄워/가자' 처럼 "
            "화면 이동 의도가 명확할 때 navigate 를 쓰세요.\n"
            "⚠️ target enum 에 없는 화면(예: 급여, 출퇴근 기록, 통계청 등)은 호출하지 말고, "
            "그런 화면은 없다고 텍스트로 답하세요.\n"
            # [NAV_FIRST 2026-05-29] 기본값 규칙 추가. 원복 시 이 마커 + 아래 '기본값:' 문장 제거.
            "기본값: 조건 없는 '근무표 보여줘/보러가자'는 roster_view(전체), '내 근무표'는 roster_view_my. "
            "⚠️ 그래도 진짜 모호하면(전체/내근무표/대시보드 중 가늠 불가) "
            "호출하지 말고 어떤 화면인지 되물으세요.\n\n"

            "─────────── target (이동할 화면) ───────────\n"
            "- `home` — 홈\n"
            "- `dashboard` — 분석 대시보드 (간호사용)\n"
            "- `wanted` — 원티드(희망근무) 화면\n"
            # [NAV_FIRST 2026-05-29] roster_view 가 조건 없는 '근무표 보여줘'의 기본 목적지.
            "- `roster_view` — 전체 근무표 조회 (조건 없는 '근무표 보여줘'의 기본 목적지) / "
            "`roster_view_my` — 내 근무표\n"
            "- `nurse_management` — 근무자 관리 (HN/ADM 전용). "
            "sub=`team_setting`(팀 설정)·`grade_setting`(등급 설정). "
            "'명단에서 삭제/근무자 삭제/제외'는 이 화면에서 처리 — sub 없이 화면만 열고 "
            "\"근무자 관리 페이지에서 해당 근무자의 '수정' 버튼을 눌러 삭제하세요\"라고 안내하라.\n"
            "- `roster_create` — 근무표 만들기 (HN/ADM 전용). 모달 중심 워크스페이스. "
            "sub=`manpower`(필요인원/인력 설정)·`wanted_config`(원티드 반영 설정)·"
            "`deadline`(원티드 마감일)·`off_request`(오프 요청 목록)·"
            "`quick_config`(생성 옵션 — OFF·휴가·**주휴**·**월 오프수(off_days)**·근무제한 등. "
            "'주휴 설정', '월 오프수/오프 며칠', '자동 주휴' 요청은 여기)·"
            "`emergency`(긴급 대체 찾기)·`version`(버전 선택). "
            "특정 설정을 '열어줘/설정하러 가자'면 해당 sub 로 바로 모달을 연다.\n"
            "  ⚠️ 발행/저장/삭제/빈근무표생성 같은 '실행' 요청은 sub 로 못 한다 — roster_create 로 "
            "화면만 열고(sub 없이) 사용자가 버튼을 누르게 안내하라. 단 '엑셀 다운로드'는 invoke 사용.\n"
            "- `config` — 근무코드 설정 화면(=/roster_configure, HN/ADM 전용). sub=`shift_codes`. "
            "근무표에 쓰는 코드·시간·원티드 반영 여부를 관리.\n"
            "  ⚠️ '원티드 반영 설정'은 roster_create 의 `wanted_config` 로, '월 오프수/주휴' 등 생성 "
            "관련 정책은 roster_create 모달로 안내하세요(config 에 더는 그 탭이 없음).\n"
            "- `mypage` — 마이페이지 / `support` — 고객센터\n\n"

            "예) '팀 어디서 바꿔?' → target=nurse_management, sub=team_setting\n"
            # [NAV_FIRST_TEAMS 2026-06-19] 무필터 목록도 화면 이동 의도로 본다. 원복 시 이 줄 제거.
            "예) '팀 목록 보여줘' / '우리 병동 팀 어떻게 돼있어?' → target=nurse_management, sub=team_setting\n"
            "예) '등급 설정 화면 띄워줘' → target=nurse_management, sub=grade_setting\n"
            "예) '퇴사자 명단에서 삭제하려고' / '근무자 삭제' → target=nurse_management (sub 없이) + 수정 버튼 안내\n"
            "예) '김민지 8월 31일자로 퇴사 처리해줘' → navigate 아님. update_person_attr(field=resignation_date)\n"
            "예) '원티드 보러 가자' → target=wanted\n"
            # [NAV_FIRST 2026-05-29] 아래 roster_view 예시 추가. 원복 시 이 줄 제거.
            "예) '근무표 보여줘' / '5월 근무표 보여줘' → target=roster_view\n"
            "예) '내 근무표 보여줘' → target=roster_view_my\n"
            "예) '근무표 새로 만들래' → target=roster_create\n\n"
            "⚠️ 병동 전환(예: '9A병동으로 가자', '9B로 바꿔줘')은 navigate 가 아니라 switch_ward 사용. "
            "병동은 화면이 아니라 컨텍스트(상단 셀렉터)다.\n"
            "⚠️ 병동 + 화면이 함께 언급되면 두 도구를 모두 호출한다 — '9A에서 팀 관리 가줘' 처럼 "
            "병동 이름이 발화에 등장하면, 현재 컨텍스트가 그 병동인지 알 수 없으므로 항상 switch_ward 를 "
            "먼저 같이 emit. 단순히 navigate 만 호출하고 '9A 팀 관리로 이동했습니다' 라고 답하면 "
            "실제로는 옛 병동의 팀 관리 화면으로 이동한 거짓 응답이 된다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": [
                        "home", "dashboard", "wanted", "roster_view",
                        "roster_view_my", "nurse_management", "roster_create",
                        "config", "mypage", "support",
                    ],
                    "description": "이동할 화면 (closed enum). 목록에 없는 화면은 호출 금지.",
                },
                "sub": {
                    "type": "string",
                    "enum": [
                        "team_setting", "grade_setting",
                        "shift_codes",
                        "manpower", "wanted_config", "deadline", "off_request",
                        "quick_config", "emergency", "version",
                    ],
                    "description": (
                        "화면 내 섹션/탭/모달. "
                        "nurse_management→team_setting|grade_setting, "
                        "config→shift_codes."
                    ),
                },
                "query": {
                    "type": "object",
                    "description": "진입 시 프리필터 (예: {\"month\": 5, \"team\": \"A팀\"}).",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "prefill",
        "description": (
            "특정 설정 화면으로 이동하면서 입력 폼을 미리 채워 사용자가 확인만 누르면 되는 "
            "상태로 만듭니다. 변경 의도가 명확하고 해당 설정 화면이 존재할 때 사용하세요. "
            "⚠️ 이 도구는 저장하지 않습니다 — 실제 적용은 사용자가 화면에서 직접 누릅니다.\n"
            "예) 'A팀 나이트 최소 1명으로 바꾸려고' → target=nurse_management, sub=team_setting, "
            "values={\"team\": \"A팀\", \"shift\": \"나이트\", \"min_count\": 1}"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": [
                        "home", "dashboard", "wanted", "roster_view",
                        "roster_view_my", "nurse_management", "roster_create",
                        "config", "mypage", "support",
                    ],
                    "description": "폼이 있는 화면 (closed enum).",
                },
                "sub": {
                    "type": "string",
                    "enum": [
                        "team_setting", "grade_setting",
                        "shift_codes",
                        "manpower", "wanted_config", "deadline", "off_request",
                        "quick_config", "emergency", "version",
                    ],
                    "description": "화면 내 섹션/탭/모달.",
                },
                "values": {
                    "type": "object",
                    "description": "폼에 미리 채울 값 (이름 기반, 내부 id 금지).",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "switch_ward",
        "description": (
            "사용자의 상단 병동 셀렉터 컨텍스트를 다른 병동으로 전환합니다. "
            "화면 이동(navigate)이 아니라 현재 화면을 유지한 채 group_id 컨텍스트만 바꾸는 액션입니다.\n\n"

            "사용 시점:\n"
            "- '9A병동으로 가자', '9B로 바꿔줘', '병동 전환해줘', '다른 병동 보고 싶어' 같은 의도\n"
            "- 병동 이름은 사용자 발화에서 추출 (예: '9A', '9B병동', 'ICU1')\n\n"

            "⚠️ 이 도구는 저장하지 않습니다 — 프론트가 사용자의 접근 가능 병동 셀렉터 목록과 "
            "ward_name 을 매칭하고, 권한이 없거나 매칭 실패면 프론트에서 안내합니다 (백엔드 권한 게이트는 "
            "프론트 셀렉터가 SSOT).\n"
            "⚠️ 같은 병동 안에서 화면을 바꾸는 의도(예: '근무표 화면 띄워줘')는 navigate 를 쓰세요. "
            "병동 전환과 화면 이동을 동시에 원하면(예: '9A의 근무표 보여줘') switch_ward 를 먼저 호출하고 "
            "navigate 를 함께 emit 하면 됩니다.\n\n"

            "예) '9A병동으로 이동해줘' → ward_name='9A'\n"
            "예) '9B병동 근무표 보여줘' → switch_ward(ward_name='9B') + navigate(target=roster_view)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ward_name": {
                    "type": "string",
                    "description": (
                        "전환할 병동 이름 (예: '9A', '9B병동', 'ICU1'). "
                        "사용자 발화에서 추출. 내부 group_id 가 아니라 사람이 부르는 이름 그대로."
                    ),
                },
            },
            "required": ["ward_name"],
        },
    },
    {
        "name": "invoke",
        "description": (
            "화면의 '버튼 클릭'을 대신 실행하는 비파괴 명령입니다 (부수효과 없는 액션만). "
            "현재 사용자가 보고 있는 근무표 워크스페이스 상태에 대해 동작합니다.\n\n"

            "⚠️ 등록된 안전 명령만 실행할 수 있습니다. 아래 command enum 밖(발행/저장/삭제/"
            "빈 근무표 생성 등 데이터를 바꾸는 파괴적 액션)은 절대 호출하지 마세요. 그런 요청은 "
            "navigate(target=roster_create)로 화면만 열고 '화면에서 직접 눌러 주세요'라고 안내하세요.\n\n"

            "─────────── command (실행할 안전 명령) ───────────\n"
            "- `excel_download` — 현재 표시 중인 근무표를 엑셀 파일로 내보내기(다운로드). "
            "'근무표 엑셀로 뽑아줘', '이거 다운로드 해줘', '엑셀로 받고 싶어' 같은 흐름.\n\n"

            "예) '근무표 엑셀로 다운로드해줘' → invoke(command=\"excel_download\")"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["excel_download"],
                    "description": "실행할 안전 명령 (closed enum). 목록에 없으면 호출 금지.",
                },
                "params": {
                    "type": "object",
                    "description": "명령에 필요한 부가 파라미터 (선택). 내부 id 금지, 이름 기반.",
                },
            },
            "required": ["command"],
        },
    },
]


# ── 매니페스트 파생 병합 ────────────────────────────────────────
# @skill 로 선언된 신규 스킬의 스키마를 SKILL_TOOLS 에 자동 편입한다.
# 기존 21개(위 리터럴)는 그대로, 매니페스트 스킬만 뒤에 붙는다. 순서: 리터럴 → 매니페스트.
def _merge_manifest_tools() -> None:
    from agents_v2.skills.manifest import load_manifest_skills, manifest_tools

    load_manifest_skills()
    existing = {t["name"] for t in SKILL_TOOLS}
    for schema in manifest_tools():
        if schema["name"] not in existing:
            SKILL_TOOLS.append(schema)


_merge_manifest_tools()
