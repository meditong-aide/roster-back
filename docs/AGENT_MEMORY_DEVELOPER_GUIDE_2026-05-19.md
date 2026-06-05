# AIDE Agent — 메모리 & 운영 가이드 (2026-05-19)

> 본 문서는 AIDE 스케줄링 에이전트의 메모리 시스템 (Tier 1+2 Redis/MSSQL),
> Tier 2 narrative/apply_hint 통합, 운영 준비 (env-gate / PII / tool audit / B1 정책)을
> 다루는 통합 개발문서다. 운영팀과 신규 개발자가 한 페이지에서 시스템을 이해할 수 있도록 정리.

---

## 0. 한 줄 요약

대화창 메시지/Variable Memory는 **MSSQL이 source-of-truth (SOT), Redis는 24h sliding hot cache** 로 운용한다. 사용자 fact (cross-session 학습)와 모든 audit trail은 MSSQL 영구 보존. 모든 read/write는 `group_id` 격리 + `agent_skill_invocation` audit 자동 기록.

---

## 1. 시스템 구조

```
┌─────────────────────────────────────────────────────────────────────┐
│ Client (Chat UI)                                                     │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ POST /agent/chat
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Router → SessionContext (group_id, user_id, conv_id, year, month)    │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ConversationStore.get_or_create  ←→  SessionMemoryRepo (write-through)│
│   ├─ Redis HIT  (~1ms,  24h sliding)                                  │
│   └─ Redis MISS → MSSQL fetch → Redis 재충전                          │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│ SchedulingAgent.run                                                  │
│   ① inject_user_memory  ← UserMemoryRepo (MSSQL, Zep식 valid_to)     │
│   ② _truncate_for_llm   (system + latest + char budget 96000)        │
│   ③ for turn in range(6):                                            │
│        LLM chat → tool_call → middleware.execute_skill              │
│           ├─ permission (HN/ADM, group_id RBAC)                      │
│           ├─ context inject (group_id, year/month, '나'→nurse_id)    │
│           ├─ grounding (이름→ID, 시프트→코드)                         │
│           ├─ skill 실행                                              │
│           └─ AgentSkillInvocation audit                              │
│   ④ consolidate_after_turn → MemoryExtractor (LLM) → UserMemoryRepo  │
│        └─ AgentMemoryAudit row                                       │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│ SessionMemoryRepo.save_messages (write-through MSSQL → Redis)        │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
                          [JSON response]
```

---

## 2. MSSQL 테이블 5종

| 테이블 | 역할 | 영구성 | NOT NULL 핵심 컬럼 |
|---|---|---|---|
| `agent_conversation` | 세션 메타 + variable_memory JSON | 영구 | user_id, group_id |
| `agent_conversation_message` | 모든 turn 메시지 | 영구 | session_id (FK), turn_idx, role |
| `agent_user_memory` | Tier 2 user fact (cross-session) | 영구 + temporal | user_id, group_id, fact_type, source |
| `agent_memory_audit` | 메모리 변경 감사 | append-only | action, tier, **who** (의료 audit) |
| `agent_skill_invocation` | Skill 호출 감사 | append-only | group_id (RBAC), skill_name, status |

설계 의도:
- 모든 NOT NULL `group_id` — D-1/D-2 audit 정책 (cross-group leak 차단)
- `valid_to NULL = currently valid` (Zep식 temporal validity)
- `agent_memory_audit.who` — 의료 audit 요건상 NOT NULL (Architect SUGGESTION 반영)

---

## 3. Redis vs MSSQL 분담

| 데이터 | Redis (24h hot) | MSSQL (영구 SOT) |
|---|---|---|
| 세션 메시지 | ✅ List, `sess:{group_id}:{sid}:msgs` | ✅ `agent_conversation_message` |
| variable_memory | ✅ Hash, `sess:{group_id}:{sid}:vm` | ✅ `agent_conversation.vm_json` |
| 세션 메타 (user_id/group_id/timestamps) | ❌ | ✅ `agent_conversation` |
| Tier 2 user fact | ❌ (양 적음 + temporal query 복잡) | ✅ `agent_user_memory` |
| 메모리 audit | ❌ | ✅ `agent_memory_audit` |
| skill 호출 audit | ❌ | ✅ `agent_skill_invocation` |
| 도메인 데이터 (nurse/schedule/wanted) | ❌ ("thin memory" 정책) | ✅ 기존 테이블 |

**핵심 원칙**:
- Redis가 다운돼도 데이터 손실 X — MSSQL이 SOT
- 모든 write는 **MSSQL 먼저 commit → Redis 갱신** (write-through, 동기)
- TTL은 **Redis 캐시 수명**일 뿐, MSSQL row는 영구

---

## 4. 메모리 라이프사이클

### 4-1. 단기 (Tier 1 — 세션 단위)
- 활성 24h 안에 새 메시지 들어오면 TTL 갱신 (sliding)
- 24h 활동 없음 → Redis 자동 만료. MSSQL row는 그대로 보존
- 24h 후 재접속 → Redis MISS → MSSQL 복원 → Redis 재충전

### 4-2. 장기 (Tier 2 — Cross-session fact)
- 매 turn 종료 시 `MemoryExtractor`가 LLM function calling으로 fact 추출
- 분류: `ADD` / `UPDATE` / `DELETE` / `NOOP`
- `UserMemoryRepo.apply_fact` → MSSQL + audit row
- UPDATE는 기존 row `valid_to=now` 만료 + 새 row insert (이력 보존)
- 다음 세션 시작 시 `query_valid_facts`로 currently-valid만 inject

### 4-3. Token Budget (LLM injection 직전)
- system prompt가 ~31k chars (도메인 지식 + skill descriptions)
- 누적 messages 전체 inject 시 token 폭증 → cost 위험
- `_truncate_for_llm(max_chars=96000)`로 LLM call 직전에만 slice
- 정책: **system 항상 + latest user 항상 + 중간 history budget 안에서 최신순**
- 영구 messages 변수와 MSSQL/Redis 저장은 그대로 — 감사 trail 손실 0

---

## 5. RBAC + PII + Audit 정책

### 5-1. group_id 격리 (D-1/D-2 audit 정책)
- 신규 5 테이블 모두 `group_id NOT NULL`
- Redis key prefix에 group_id 포함 (cross-group key 충돌 불가)
- 모든 read/write 쿼리에 group_id 필터 강제
- `ConversationStore`가 session_id 재사용 시 group_id mismatch → `ValueError`

### 5-2. PII 보호 (한국 개인정보보호법 + 의료법)
- `MemoryExtractor` prompt에 "사람 이름 직접 사용 금지, nurse_id 사용" 강제
- Last-mile 검증: fact_text에 한국어 이름+호칭 패턴 검출 시 row drop + warning log
- 패턴: `[가-힣]{2,4}` + (`간호사` | `선생님` | `님` | `씨` | `쌤` 등)
- `agent_skill_invocation.args_json`에는 PII 잔존 가능 — column-level 암호화는 향후 작업

### 5-3. Audit Trail (HIPAA 2025 + 의료법)
- `agent_memory_audit` — 모든 user_memory mutation 자동 기록 (`who NOT NULL`)
- `agent_skill_invocation` — 모든 skill 호출 기록 (SUCCESS/DENIED/CLARIFICATION_NEEDED/ERROR)
- `args_json` 2000자 truncate (payload bloat 방지)
- audit insert 실패는 silent — skill 실행 자체에 영향 0

### 5-4. 정책-고정 field 차단 (QA §B1)
- `update_constraint` skill의 `_POLICY_LOCKED_FIELDS` 등록 항목 변경 시도 → `policy_locked` 거절
- 현재: `max_nig_per_month` (야간 최대 횟수)
- preview/apply 둘 다 차단 — 사용자 confirm 흐름 진입 불가
- 대안 안내: `update_monthly_limit`으로 개인별 한도 권장

---

## 6. 운영 절차

### 6-1. 환경 변수
| 변수 | 용도 | 기본값 | production 필수 |
|---|---|---|---|
| `REDIS_URL` | Redis connection URL | (없음) | ✅ |
| `ENVIRONMENT` | 환경 식별 (`dev` / `production`) | `dev` | ✅ |
| `DATABASE_URL` (`db/client2.py`) | MSSQL connection | (기존) | ✅ |

**Production env-gate**:
- `ENVIRONMENT=production` + `REDIS_URL` 미설정 → `RuntimeError`
- `ENVIRONMENT=production` + Redis 연결 실패 → `RuntimeError`
- multi-worker uvicorn에서 worker별 fakeredis 분리로 인한 silent corruption 방지

### 6-2. MSSQL 마이그레이션

두 가지 방식:

**(A) SQL 직접 실행** — DBA가 SSMS 등에서 실행
- 파일: `migrations/2026_05_19_add_agent_memory_tables.sql`
- 트랜잭션 + 멱등성 (IF NOT EXISTS)
- 끝에 검증 쿼리 3개 포함 (5 테이블 / who NOT NULL / NULL 잔재 0)

**(B) Python 스크립트** — 자동화/CI용
- 파일: `migrations/2026_05_19_add_agent_memory_tables.py`
- `--dry-run`, `--rollback` 옵션
- SQLAlchemy metadata 기반 (다이얼렉트 자동 감지)

적용 순서:
1. DB 백업 (필수)
2. dry-run으로 영향 미리 확인
3. 본 적용 (SQL 또는 Python)
4. 검증 쿼리 결과 0 확인

### 6-3. Redis 인프라
| 항목 | 권장 |
|---|---|
| 호스팅 | Azure Cache for Redis (Standard 1GB 이상) |
| persistence | AOF + RDB 둘 다 활성 (재시작 시 hot cache 복원) |
| eviction policy | `allkeys-lru` (자동 LRU 정리) |
| 최대 메모리 | 활성 세션 × 50KB × 안전계수 2 (예: 1000 세션 → 100MB) |
| 백업 | 일 1회 RDB snapshot (필수는 아님 — MSSQL이 SOT) |
| 모니터링 | hit rate, memory usage, evicted_keys, connected_clients |

### 6-4. 배포 체크리스트
- [ ] 마이그레이션 적용 완료 (5 테이블 + who NOT NULL)
- [ ] `REDIS_URL` 환경 변수 설정
- [ ] `ENVIRONMENT=production` 설정
- [ ] Redis 연결 ping OK
- [ ] 회귀 테스트 1088 passed 확인
- [ ] 운영 환경 smoke test (1개 세션 → 메시지 → 응답 → 24h 후 재접속)
- [ ] audit 테이블 row 증가 확인 (skill 호출마다)

---

## 7. 모듈 / 파일 매핑

| 영역 | 경로 | 비고 |
|---|---|---|
| Redis client (env-gate 내장) | `app/services/memory/redis_client.py` | fakeredis fallback (dev/test) |
| Tier 1 Repo (write-through) | `app/services/memory/session_repo.py` | `save/load_messages`, `touch_ttl` |
| Tier 2 Repo (temporal validity) | `app/services/memory/user_repo.py` | `apply_fact`, `query_valid_facts`, `expire_fact` |
| LLM-기반 fact 추출 | `app/services/memory/extractor.py` | ADD/UPDATE/DELETE/NOOP + PII drop |
| Audit emitter | `app/services/memory/audit_logger.py` | silent fail |
| ConversationStore | `app/agents_v2/conversation.py` | SessionMemoryRepo 위임 |
| Agent loop | `app/agents_v2/agent_v3.py` | inject/consolidate + truncation + apply_hint |
| Middleware (5-stage + audit) | `app/agents_v2/middleware.py` | `_write_skill_audit` |
| narrative 자연어 변환 | `app/agents_v2/tools/narrative_formatter.py` | `format_infeasibility_response` |
| MSSQL 모델 5개 | `app/db/models.py` (line 800+) | AgentConversation* / UserMemory / Audit / SkillInvocation |
| 마이그레이션 | `migrations/2026_05_19_add_agent_memory_tables.{sql,py}` | 양쪽 모두 제공 |

---

## 8. 테스트 커버리지

| 테스트 파일 | 시나리오 수 | 영역 |
|---|---|---|
| `test_memory_session_repo.py` | 6 | write-through + cache miss + TTL + group 격리 |
| `test_conversation_store_redis.py` | 7 | 영속성 + TTL 24h + pending_approval roundtrip |
| `test_memory_user_repo.py` | 11 | ADD/UPDATE/DELETE/NOOP + temporal + RBAC |
| `test_memory_extractor.py` | 11 | LLM function calling + PII drop + edge cases |
| `test_memory_middleware_integration.py` | 8 | inject/consolidate E2E + 격리 |
| `test_memory_audit.py` | 7 | 각 action별 audit row 검증 |
| `test_memory_e2e.py` | 5 | session → consolidate → 다음 세션 inject 흐름 |
| `test_generate_schedule_narrative.py` | 8 | INFEASIBLE → 한국어 + hard_case 안내 |
| `test_apply_hint_flow.py` | 23 | yes/no/cancel/ambiguous |
| `test_tool_audit.py` | 7 | DENIED/SUCCESS/CLARIFICATION/ERROR + args truncation |
| `test_history_truncation.py` | 10 | system 보존 / latest 보존 / oldest drop |
| `test_b1_policy_enforcement.py` | 7 | max_nig 변경 차단 (preview/apply) |

**현재 baseline**: 1088 passed / 3 pre-existing fail (`test_nurse_monthly_limits_api.py` — 별개 영역).
**회귀 0건 보장** — Architect THOROUGH verification APPROVED.

---

## 9. 정책 요약 (실수 방지 체크리스트)

- **regex 금지** — LLM 의미 분류에 regex 사용 X (보안 검증용은 별개로 허용)
- **thin memory** — 병원 데이터를 system prompt에 적재 X. 런타임 DB 조회
- **MSSQL이 SOT** — Redis는 항상 derived. Redis 다운 시 MSSQL에서 복원 가능해야 함
- **group_id 격리** — 모든 read/write 함수가 group_id 인자 받음 (NOT NULL)
- **mutation 2-step** — preview → 사용자 confirm → apply. 1-step 적용은 정책 위반
- **정책-고정 field** — `_POLICY_LOCKED_FIELDS` 등록 항목은 변경 불가
- **audit silent fail** — audit insert 실패가 skill 실행을 깨뜨리지 않음
- **backward-compat hack 회피** — 미사용 코드는 깨끗이 삭제

---

## 10. 향후 작업 (미해결)

### 🟡 운영 안정화 (Sprint B 후보)
- fact deduplication (semantic + confidence 기반)
- memory priority/decay (token budget 효율)
- memory READ audit (현재 WRITE만 — 의료 audit 완전)
- 실제 Redis docker-compose 통합 테스트

### 🔵 Agent 기능 (Sprint C 후보 — QA OPEN 8건)
- O-5 산술 skill (`min_exp_per_shift` feasibility 검증)
- O-6 시프트 불가 nurse 메타 (`Nurse.unavailable_shifts`)
- O-7 repair 실패 시 대안 가이드
- O-2 daily_shift_by_day skill 매핑

### 🟣 Architectural (Sprint D 후보)
- Constraint Adjustment Control Layer (`CONSTRAINT_AGENT_CONTROL_DRAFT.md` 구현)
- 쿼리 카테고리화 telemetry
- `descriptions.py` → `skills/*.md` 분리 (DevEx)
- `agent_skill_invocation.args_json` column-level 암호화

### 🔴 의료 도메인 강화 (장기)
- fact retention 정책 (의료법 보관 기간 후 hard delete)
- 사용자 memory UI (Mem0 식 — 사용자가 자신의 fact 보기/삭제)
- multi-region replication
- PHI 마스킹 자동화 (NER 기반 entity 치환)

---

## 11. 관련 문서 인덱스

| 문서 | 내용 |
|---|---|
| `CLAUDE.md` | agent 아키텍처 정책 + 안티패턴 |
| `docs/AGENT_HANDOFF_2026-05-17.md` | 이전 세션 핸드오프 (D-1/D-2 audit) |
| `docs/AGENT_QA_SCENARIOS_2026-05-18.md` | 50건 QA 시나리오 + OPEN 8건 |
| `docs/CONSTRAINT_AGENT_CONTROL_DRAFT.md` | Tier 3 PRD (constraint adjustment) |
| `.omc/prd.json` | ralph PRD (Redis + narrative 7 stories) |
| `.omc/progress.txt` | ralph 진행 기록 |

---

## 12. 커밋 이력 (이번 sprint)

| 커밋 | 내용 |
|---|---|
| `99c729a` | D-2 group_id audit + QA 시나리오 50건 |
| `ac6685d` | Redis 메모리 (Tier 1+2) + narrative/apply_hint + env-gate/PII/tool-audit |
| `2981d86` | History truncation (token budget) |
| `1895bf1` | 운영 준비 마무리 (마이그레이션 + B1 정책 + audit.who NOT NULL) |

**End of guide.** 신규 개발자는 본 문서 + `CLAUDE.md` + `AGENT_QA_SCENARIOS_2026-05-18.md` 3개로 시스템 인지 가능.
