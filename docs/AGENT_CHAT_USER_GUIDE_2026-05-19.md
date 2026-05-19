# AIDE Agent — Chat UI 실사용 가이드 (2026-05-19)

> 본 문서는 개발/검증 목적으로 AIDE 에이전트를 브라우저 chat UI에서 직접 시험해볼 수 있는
> step-by-step 가이드다. 운영 배포 전 smoke test, 신규 시나리오 검증, 데모용으로 활용.

---

## 1. 빠른 시작 (5단계)

| 단계 | 동작 | 결과 |
|---|---|---|
| 1 | 서버 실행 — `uvicorn app.main:app --reload --port 8000` | localhost:8000 LIVE |
| 2 | 브라우저 → `http://localhost:8000/agent/test` | Chat UI 진입 |
| 3 | "Setup DB" 버튼 (있다면) 또는 시드 DB 자동 로드 대기 | 시나리오 데이터 준비 |
| 4 | Office / Group / Nurse 선택 → 세션 컨텍스트 확정 | 권한 + 격리 적용 |
| 5 | 자연어 query 입력 → 응답 + trace 확인 | agent 동작 검증 |

---

## 2. 사전 준비

### 2-1. 의존성
| 항목 | 필요? | 안 깔려있어도? |
|---|---|---|
| Python 3.11+ | ✅ | 필수 |
| MSSQL | 운영 | dev는 SQLite로 자동 fallback (testing fixture) |
| Redis | 운영 | dev는 fakeredis로 자동 fallback (in-memory) |
| OpenAI / Anthropic API 키 | LLM 호출 시 | DeterministicLLMClient로 대체 가능 |

### 2-2. 환경 변수 (선택)
| 변수 | dev 기본값 | production 필수 |
|---|---|---|
| `DATABASE_URL` | (sqlite 자동) | MSSQL connection string |
| `REDIS_URL` | (fakeredis 자동) | ★ 필수 |
| `ENVIRONMENT` | `dev` | `production` 명시 |
| `OPENAI_API_KEY` | (없으면 LLM 호출 실패) | ✅ |
| `ANTHROPIC_API_KEY` | (fallback) | 권장 |

dev 환경은 환경변수 없어도 chat UI 진입 + 시드 DB로 시험 가능.

---

## 3. 서버 실행

`roster-back` 디렉토리에서 `uvicorn app.main:app --reload --port 8000` 실행. `--reload`는 코드 변경 시 자동 재시작 (dev 전용). 첫 실행에 5-10초 소요 (DB 연결 + 모델 로드).

성공 시그널: "Uvicorn running on http://0.0.0.0:8000" + "Application startup complete".

---

## 4. Chat UI 진입

브라우저에서 `http://localhost:8000/agent/test` 접속.

### 화면 구성 (개략)
```
┌──────────────────────────────────────────────────────────────┐
│  Office: [select]  Group: [select]  Nurse: [select]  Role: HN │
│  Conversation ID: [auto / clear]                              │
├──────────────────────────────────────────────────────────────┤
│  ┌──────────────────── Chat history ─────────────────────┐    │
│  │ [user] 4월 야간 최대 횟수 설정 보여줘                  │    │
│  │ [agent] 현재 야간 최대 7회로 설정되어 있습니다 ...     │    │
│  │ [user] ...                                             │    │
│  └────────────────────────────────────────────────────────┘    │
├──────────────────────────────────────────────────────────────┤
│  [Trace 패널]  planning → grounding → execution → answer       │
│   skill: query_schedule  scope: constraint_config  duration:.. │
├──────────────────────────────────────────────────────────────┤
│  [입력]  메시지 입력...                                [전송]  │
└──────────────────────────────────────────────────────────────┘
```

### 라우트 인덱스 (참고)
| 메서드 | 경로 | 용도 |
|---|---|---|
| GET | `/agent/test` | UI 페이지 |
| POST | `/agent/test/chat` | 메시지 전송 (JSON request/response) |
| POST | `/agent/test/setup-db` | 시드 DB 초기화 (dev SQLite) |
| GET | `/agent/test/status` | DB + LLM 상태 |
| GET | `/agent/test/offices` | Office 목록 |
| GET | `/agent/test/groups` | Group 목록 |
| GET | `/agent/test/nurses` | Nurse 목록 |

---

## 5. 세션 컨텍스트 설정 (필수)

agent는 **누가 어느 병동에서 무슨 권한으로** 발화하는지 알아야 동작한다.

| 필드 | 의미 | 예시 |
|---|---|---|
| **Office** | 병원/사업장 식별 | `OFF001` |
| **Group** | 병동 식별 (RBAC 격리 핵심) | `GRP001` |
| **Nurse** | 본인 (myself) — "나" 발화 대상 | `N001` 김민지 |
| **Role** | HN(수간호사) / NURSE(일반) / ADM(관리자) | 권한 게이팅 |
| **Conversation ID** | 세션 식별 (자동 생성 or 재사용) | UUID-like |

### 권한별 가능 동작
| Role | 정책 변경 | 타인 데이터 수정 | 본인 mutation | 조회 |
|---|---|---|---|---|
| HN/ADM | ✅ | ✅ | ✅ | ✅ |
| NURSE | ❌ | ❌ | 본인만 ✅ | 본인 + 공개 ✅ |

→ 권한 거절 시 한국어 안내: "병동 전체 설정 변경은 수간호사 권한이 필요합니다" 등.

---

## 6. 시도해볼 만한 query (Track 별)

### A. 설정 조회 (read-only, 권한 무관)
- "야간 최대 횟수 설정 보여줘"
- "데이 필요인원 몇 명이지?"
- "주2회 오프 보장 켜져 있어?"
- "스케줄 정책 전체 보여줘"

### B. 원티드 조회
- "5월 원티드 미제출자 누구야?" *(group_id 격리 검증)*
- "김민지 5월 원티드 신청 내용"
- "5월 15일 원티드 신청자"
- "5월에 N 원티드 신청한 사람"

### C. 설정 변경 (HN/ADM 전용 + 정책 차단 검증)
- "연속 근무 최대 5일로 제한" → preview → confirm
- "이브닝 다음날 데이 금지 해제" → preview → confirm
- **"야간 최대 7회로 바꿔줘"** → **policy_locked 거절 (B1 정책)**, 대안 안내

### D. 원티드 확정반영
- "김민지 5월 15일 원티드 승인해줘" → preview → "네"
- "박혜미 5월 10일 원티드 거부"
- "박혜미 5월 5일 원티드 삭제" → 재확인 단계 거쳐 처리

### E. 개인별 N 한도 (Tier 2 메모리 학습 관찰용)
- "김민지 5월 N 4번으로 맞춰줘"
- → 다음 세션에서 "김민지 N 한도 몇번?" 물으면 inject 활용

### F. 복합 (agent의 multi-step 흐름)
- "박혜미 5월 10일 원티드 취소하고 김민지를 대신 배치"
- "5월 미제출자 확인하고 그 사람들 마감일 연장할까?" → 거절 + clarify
- "5월 근무표 위반사항 보여주고 자동으로 고쳐줘"

### G. Edge case
- "없는간호사 5월 원티드 보여줘" → 한국어 안내
- "13월 근무표 생성해줘" → 유효성 거절
- "이번 달 위반사항 알려줘" *(근무표 미생성 시 "없습니다" 자연어)*

전체 50개 시나리오는 `docs/AGENT_QA_SCENARIOS_2026-05-18.md` 참고.

---

## 7. 메모리 동작 관찰 포인트

### 7-1. Session Memory (Tier 1 — 24h sliding)
같은 conversation_id로 여러 turn 이어 발화:
1. turn1: "원티드 미제출자 보여줘"
2. turn2: "그중에 김민지 있어?"
3. turn3: "그 사람 알림 다시 보내줘"

→ agent는 turn1의 결과를 기억하고 turn2/3에서 활용. **24h 후 재접속해도 MSSQL에서 복원**.

### 7-2. User Fact Memory (Tier 2 — Cross-session 학습)
세션 A:
- "나는 김민지 간호사로 활동해" or "내 야간 한도 4로 맞춰줘"

세션 B (며칠 후, 다른 conversation_id):
- "내 야간 한도 알려줘"
- → agent가 system prompt에 inject된 fact로 응답 (DB에서 fact 로드된 결과)

**관찰**: trace 패널에 "memory_block injected" 같은 stage가 나오는지 확인 가능.

### 7-3. Apply_hint 재시도 (Tier 2 narrative)
1. "5월 근무표 만들어줘" (의도적으로 infeasible한 상태)
2. agent → "team_min 제약을 풀고 재시도하시겠습니까?"
3. "네" → constraint_adjustments 채워 재호출 → 결과

---

## 8. 디버깅 — Trace + Audit 확인

### 8-1. UI Trace 패널
매 응답에 5-stage trace 표시:
1. **planning** — LLM이 어떤 skill을 선택했는지
2. **permission** — RBAC 결과 (pass/block)
3. **context_injection** — group_id/year/month 등 자동 주입 내역
4. **grounding** — 이름→ID, 시프트→코드 해석
5. **execution** — skill 실행 + duration_ms

### 8-2. Audit 테이블 직접 조회 (DBA)
| 테이블 | 검색 예 |
|---|---|
| `agent_skill_invocation` | 최근 1시간 skill 호출 + status 분포 |
| `agent_memory_audit` | user_memory 변경 이력 |
| `agent_conversation_message` | 특정 session의 모든 turn |

→ "왜 이 사용자가 이걸 못 했나?" 같은 운영 문의는 audit으로 99% 재구성 가능.

### 8-3. 로그 확인
- `[middleware] skill audit insert failed` → audit 비활성 / DB 문제 (skill 자체는 통과)
- `[extractor] drop fact — PII detected` → 사용자 발화에 이름 포함 → fact drop (정상)
- `[redis_client] REDIS_URL not set — using fakeredis` → dev에서 정상, production이면 즉시 실패

---

## 9. 실전 검증 시나리오 (5분 smoke test)

| # | 동작 | 기대 |
|---|---|---|
| 1 | UI 진입 → Office/Group/Nurse 선택 (HN) | 세션 컨텍스트 확정 |
| 2 | "5월 원티드 미제출자 보여줘" | 미제출자 목록 (현재 group만) |
| 3 | "야간 최대 7회로 바꿔줘" | **policy_locked 거절** + 대안 안내 |
| 4 | "연속 근무 최대 5일로 제한" → preview → "네" | 정책 변경 적용 |
| 5 | UI Nurse role로 변경 → "연속 근무 4일로" | **권한 거절** "수간호사 권한 필요" |
| 6 | 동일 conv_id로 turn1~3 이어 발화 | session memory 활용 |
| 7 | 새 conv_id로 "지난번 한도 설정 기억해?" | Tier 2 fact inject 확인 (있다면) |
| 8 | DBA → `agent_skill_invocation` 조회 | 위 호출 6-8건 row 확인 |

전 8 시나리오가 정상 동작하면 production 배포 후보.

---

## 10. 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| 페이지 진입 시 500 | DB 연결 실패. `app/db/client2.py` DATABASE_URL 확인 |
| "DB 미초기화" | dev: POST `/agent/test/setup-db` 또는 시드 fixture 자동 실행 |
| LLM 응답 매우 느림 | OpenAI API 키 / 네트워크 / 첫 호출 cold start. 30초 이상이면 키 확인 |
| 응답이 "처리 못함" | LLM이 적절한 skill 찾지 못함. trace의 planning 단계 응답 확인 |
| 권한 거절 (예상 외) | Role 설정 + Nurse 일치 확인. middleware._check_permission 참조 |
| group_id 누락 격리 위반 의심 | `agent_skill_invocation.group_id` 확인. NULL이면 backend 버그 |
| Redis 연결 실패 | dev: 무시 (fakeredis fallback). production: `RuntimeError` 즉시 발생 — `REDIS_URL` 설정 |
| `policy_locked` 응답 | 정상 — 정책-고정 field 변경 시도. 대안 안내 따라야 함 |
| `clarification_needed` 응답 | 정상 — 모호한 발화. 추가 정보 입력 후 재시도 |

---

## 11. 시연 시나리오 (이해관계자 데모용)

| 시연 흐름 | 시간 | 메시지 |
|---|---|---|
| 1) 일상 조회 | 30초 | "야간 분포 어떻게 돼?" → analyze_report |
| 2) Multi-step 작업 | 1분 | "김민지 5월 N 4번으로 맞추고 근무표 다시 돌려" → 한도 변경 → preview → 재생성 |
| 3) 권한 차단 | 30초 | Nurse role로 "정책 변경 시도" → 한국어 거절 |
| 4) 정책 차단 | 30초 | "야간 최대 7회로 바꿔줘" → policy_locked + 대안 |
| 5) Infeasibility narrative | 1분 | 의도적 infeasible 시드 → "근무표 만들어줘" → 한국어 원인 + apply_hint 제안 |
| 6) 메모리 학습 | 1분 | 세션 A에서 발화 → 세션 B에서 fact inject 확인 |

전체 ~5분.

---

## 12. 외부 시연 / 데모용 시드 데이터

- `roster-back/tests/conftest.py` 의 `seed_data` fixture가 SQLite 시드 (6 nurses + ICU 시나리오)
- 정확한 nurse 이름 / 시프트 / 날짜는 fixture 코드 참조
- 시연 전 `POST /agent/test/setup-db`로 시드 재생성 가능

---

## 13. 관련 문서

| 문서 | 용도 |
|---|---|
| `docs/AGENT_MEMORY_DEVELOPER_GUIDE_2026-05-19.md` | 메모리 시스템 + 운영 아키텍처 (개발자) |
| `docs/AGENT_QA_SCENARIOS_2026-05-18.md` | 50건 QA 시나리오 정의서 |
| `CLAUDE.md` | agent 아키텍처 정책 + 안티패턴 |
| `migrations/2026_05_19_add_agent_memory_tables.sql` | DBA용 MSSQL 마이그레이션 |
| `migrations/2026_05_19_add_agent_memory_tables.py` | 자동화용 Python 마이그레이션 |

---

**End of guide.** 다음 사용자/검증자는 본 문서 + `setup-db` 한 번 + 위 §9 8 시나리오로 5분 안에 시스템 동작을 확인할 수 있다.
