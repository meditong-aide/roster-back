# 세션 핸드오프 — 간호사 스케줄링 에이전트 개발

작성일: 2026-04-16
브랜치: `feat/agent4` (main: `feat/mssql-transition`)

---

## 1. 프로젝트 방향 (반드시 먼저 읽기)

`CLAUDE.md`에 정의된 **top-down agentic flow** 원칙을 따른다. 핵심:

- LLM이 전체 의도 분석 → 스킬 선택 → 스킬 내부에서 grounding
- regex/hardcoded pattern/lookup table **금지**
- 스킬은 self-describing (JSON schema + Korean description)
- Thin memory: 병원/병동 데이터는 프롬프트에 넣지 않고 런타임 DB 조회

참조 아키텍처: LangChain DeepAgent, Claude Code agentic flow.

---

## 2. 에이전트 구조 (현재)

### 진입점
- `app/agents_v2/agent_v3.py` — `SchedulingAgent.run()` 메인 루프
- `app/agents_v2/llm_client.py` — OpenAI/Anthropic 추상화
- `app/agents_v2/conversation.py` — `ConversationStore` (in-memory, thread/pending_approval/variable_memory)

### 프롬프트 조립
- `app/agents_v2/harness/prompt_builder.py` — 6 섹션(Role / Domain / Abbreviation / Routines / Tools / Rules)
- `.aide/DOMAIN_KNOWLEDGE.md` — 데이터 모델·접근 경로·Routine 정의
- `.aide/ABBREVIATION_DICT.md` — 약어 사전 (LLM이 읽음, 자동 증분)

### 스킬 (9개)
`app/agents_v2/skills/descriptions.py` — LLM에 노출되는 JSON 스키마.
`app/agents_v2/skills/registry.py` — 내부 디스패처.

| 스킬 | 파일 | 상태 |
|---|---|---|
| query_schedule | `skills/query_schedule.py` | ✅ 안정 |
| bulk_mutation | `skills/bulk_mutation.py` | ✅ 원티드 날짜별 CRUD 추가 완료 |
| update_constraint | `skills/update_constraint.py` | ✅ flat/nested 파라미터 지원 |
| update_person_attr | `skills/update_person_attr.py` | 사용 빈도 낮음 |
| generate_schedule | `skills/generate_schedule.py` | 비동기 SQS 경유 |
| validate_schedule | `skills/validate_schedule.py` | ⚠️ 파라미터 부재 (#16) |
| recommend_candidates | `skills/recommend_candidates.py` | ✅ |
| repair_schedule | `skills/repair_schedule.py` | ⚠️ 파라미터 부재 (#20) |
| analyze_report | `skills/analyze_report.py` | ⚠️ analysis_type 분기 미작동 (#13) |

---

## 3. 최근 완료된 작업

### A. 원티드 날짜별 CRUD + Approval Flow
- `app/agents_v2/tools/wanted_tools.py`:
  - `_create_draft_with_copy()` — 프론트 패턴 따라 새 `request_id` 생성 + `NurseShiftRequest` 복사(수정/제외/추가 적용) + `NursePairRequest` carry-forward
  - `delete_wanted_by_date` / `add_wanted_by_date` / `modify_wanted_by_date`
  - `_submit_draft()` — 프리뷰 후 승인 시 `is_submitted=True` 전환
- `app/agents_v2/skills/bulk_mutation.py` — `cancel+date` / `add_shift` / `change_shift` 라우팅
- `app/agents_v2/harness/prompt_builder.py` rule #12 — 본인 원티드 룰
- 테스트: `tests/test_agent_v3.py::TestWantedPerDateCRUD` (11개), `tests/test_real_llm_wanted.py` (3/3 pass)

### B. 제약조건 수정 스킬
- flat (`field`/`value`) + nested (`mutation.target_field/target_value`) 둘 다 수용
- 테스트: `tests/test_agent_v3.py::TestConstraintUpdate` (6개), `tests/test_real_llm_constraint.py` (6/6 pass)

### C. 제약조건 필드 매핑 검증 (방금 완료)
- `descriptions.py`의 16개 DB 필드 매핑은 **전부 정확**
- 스케줄러(`cp_sat_basic.py` / `cp_sat/fallback_lex.py`) 내부 변환 경로 확인됨

#### 스케줄러에서 발견된 별도 이슈 (에이전트 문제 아님)
1. **`team_balance_enable` / `team_balance_gauge` 하드코딩** (cp_sat_basic.py:471-472) — DB 값 무시, 항상 `True`/`10`으로 동작
2. **`max_nig_per_month` 오버라이드** (cp_sat_basic.py:353-355) — 15가 아닌 값은 모두 17로 강제 변환

> 사용자 지시: roster_config(스케줄러) 쪽 이슈는 나중에 별도 처리. 에이전트 측은 손대지 않아도 됨.

---

## 4. 핵심 파일 맵

### 수정된 파일 (커밋 대기)
```
M app/agents_v2/skills/bulk_mutation.py
M app/agents_v2/skills/query_schedule.py
M app/agents_v2/skills/registry.py
M app/agents_v2/tools/schedule_tools.py
M app/main.py
M tests/test_e2e_agent.py
M app/agents_v2/grounding/__init__.py
M app/agents_v2/harness/__init__.py
M app/agents_v2/schemas/__init__.py
```

### 신규 파일 (커밋 대기)
```
CLAUDE.md
.aide/AGENT_DESIGN_V3.md
.aide/DOMAIN_KNOWLEDGE.md
.aide/HARNESS_DESIGN.md
.aide/QUERY_ANALYSIS.md
app/agents_v2/agent_v3.py
app/agents_v2/conversation.py
app/agents_v2/grounding/internal.py
app/agents_v2/harness/prompt_builder.py
app/agents_v2/llm_client.py
app/agents_v2/middleware.py
app/agents_v2/schemas/session_context.py
app/agents_v2/skills/descriptions.py
app/agents_v2/test_chat_router.py
app/agents_v2/variable_memory.py
app/templates/agent_test_chat.html
app/test_server.py
tests/test_agent_v3.py
```

### 삭제된 파일 (구 v2 아키텍처)
`app/agents_v2/agent.py`, `core/*.py`, `grounding/{dispatcher,entity,lexical,status,temporal,workflow}_grounder.py`, `harness/{agents_loader,memory_manager,skill_matcher,topic_selector}.py`, `router.py`, 일부 `schemas/*.py`

---

## 5. DB 스키마 요약 (핵심만)

### 원티드 관련 (per-date CRUD에서 사용)
- `WantedRequest`: PK=(nurse_id, request_id, month='YYYY-MM'), `is_submitted`, `created_at`, `submitted_at`
- `NurseShiftRequest`: PK=(nurse_id, request_id, detailed_request_id, shift_date), `shift` CHAR(1), `shifts_table_id`, `score`, `comment`
- `NursePairRequest`: PK=(nurse_id, request_id, month, detailed_request_id, target_id), `score`, `partial_request`

**프론트/스케줄러 규약**: 원티드 수정 시 **새 request_id 생성** + shift 전량 복사 + pair carry-forward.
`roster_create_service`는 `max(request_id)`로 최신 버전을 읽음.

### 제약조건
- `RosterConfig` (`app/db/models.py:280~`) — 40+ 필드. 주요 필드는 `descriptions.py` 매핑 참고.

---

## 6. 테스트 커맨드

```bash
# 전체 pytest
cd roster-back && python -m pytest tests/ -v
# (현재 84/84 pass)

# 실제 LLM E2E (OpenAI API 키 필요)
cd roster-back && python -m tests.test_real_llm_wanted      # 3/3
cd roster-back && python -m tests.test_real_llm_constraint  # 6/6

# 로컬 서버
cd roster-back && uvicorn app.main:app --reload --port 8000
# 테스트 채팅 UI: http://localhost:8000/agent/test

# Lint
ruff check app/
```

---

## 7. 남은 작업 (우선순위 순)

### 🔴 HIGH
- **#32 Structural Routine matching with step tracking** — Routine(복합 쿼리 패턴)을 LLM이 놓치지 않도록 구조적으로 매칭하고 각 단계 완료 여부를 추적하는 기능. `DOMAIN_KNOWLEDGE.md` §5에 Routine 정의는 있지만 실제 실행은 LLM 판단에 맡겨져 있음.

### 🟡 MEDIUM
- **#13 `analyze_report` analysis_type 분기 미작동** — `scope`/`analysis_type` 파라미터가 구현부까지 전달되지 않음 (~15 LOC)
- **#16, #20 validate/repair_schedule 파라미터 추가** (~20 LOC) — 현재 파라미터 없이 전체 스케줄만 대상으로 함. 날짜/시프트/간호사 필터 필요
- **#9 메모(comment) 수정 기능** — 현재는 추가만 가능. 기존 원티드의 comment만 수정하는 경로 필요 (~25 LOC)

### 🟢 LOW (엔진 쪽, 나중에)
- **#21, #22 프리셉터 pairing / 팀별 밸런스** — engine-level validation 필요
- **스케줄러 하드코딩 제거** — `cp_sat_basic.py:471-472`, `353-355` 주석 처리된 원래 값 복원 (사용자가 "나중에" 처리하기로)

---

## 8. 주의사항 (하지 말 것)

1. ❌ `cp_sat_adaptive.py`, `cp_sat_basic_legacy.py`, `cp_sat_basic_base.py`, `cp_sat_basic_lagrangian.py` 참조 금지 — 모두 레거시
2. ❌ regex/lookup table로 자연어 파싱하지 말 것 — 반드시 LLM에 위임
3. ❌ 프롬프트에 병원/병동별 데이터 하드코딩 금지
4. ❌ `NursePairRequest`는 수정 시 request_id 변경 없음 (max로 가져옴) — copy만 하면 됨
5. ❌ 원티드는 **자기 자신만** 수정 가능 (이름 없는 요청은 현재 사용자 본인 것으로 간주)

---

## 9. 자주 쓰는 참조

- **아키텍처 문서**: `CLAUDE.md`, `.aide/AGENT_DESIGN_V3.md`, `.aide/HARNESS_DESIGN.md`
- **도메인 지식**: `.aide/DOMAIN_KNOWLEDGE.md` (Routine 패턴 §5 포함)
- **쿼리 분석 샘플**: `.aide/QUERY_ANALYSIS.md`
- **프론트 원티드 서비스**: `app/services/preferences_service.py`, `app/routers/preferences.py`
- **스케줄러 진입점**: `app/services/cp_sat_basic.py` (L347~ 필드 추출), `app/services/cp_sat/fallback_lex.py`
- **로스터 생성**: `app/services/roster_create_service.py` (L1622~ 제약 해석)

---

## 10. 다음 세션 시작 시 권장 프롬프트

```
.aide/HANDOFF.md를 먼저 읽고 현재 상태를 파악해줘.
다음으로 할 작업은 [#32 / #13 / #16 / #9 등] 이야.
```
