# AIDE Agent — Chat 사용 가이드 (2026-05-19)

> 본 문서는 실제 서비스 페이지(대시보드, 수간호사 관리 등)에 **부착된 floating chat 위젯**을
> 사용하는 가이드다. 로그인한 사용자의 컨텍스트(병동/권한/본인)를 자동 활용한다.
> dev test UI (`/agent/test`) 는 별도 — 인증 없이 컨텍스트 수동 선택용 (§10 참고).

---

## 1. 한 줄 요약

로그인 → 페이지 우측 하단 💬 버튼 클릭 → 자연어로 질의/지시 → 응답.
**별도 로그인 / 컨텍스트 입력 없음** (cookie 인증 자동 사용).

---

## 2. 사용 가능한 페이지

현재 floating chat이 부착된 페이지:

| 페이지 | 경로 | 비고 |
|---|---|---|
| 대시보드 | `/dashboard` | 근무표 만족도 분석 |
| 수간호사 관리 | `/head-nurse-management` | 권한자만 |
| (그 외 페이지) | — | `_agent_floating_chat.html` partial include로 추가 가능 |

→ 새 페이지에 부착하려면 §9 참고.

---

## 3. 사용 흐름 (3 step)

| 단계 | 동작 | 결과 |
|---|---|---|
| 1 | 정상 로그인 (access_token cookie 발급) | 어떤 페이지든 진입 가능 |
| 2 | 페이지 진입 후 우측 하단 💬 버튼 클릭 | chat panel slide-in |
| 3 | 메시지 입력 → Enter | 응답 + trace 표시 |

**미로그인 사용자**: floating 버튼 자체가 표시되지 않음 (`/api/agent/chat/whoami` 401 응답으로 위젯이 자동 숨김).

---

## 4. 자동 적용되는 컨텍스트

로그인 cookie에서 자동 추출하여 모든 query에 적용:

| 필드 | 값 | 영향 |
|---|---|---|
| `office_id` | 사용자 소속 병원 | 도메인 데이터 범위 |
| `group_id` | 사용자 소속 병동 | RBAC 격리 (다른 병동 데이터 안 보임) |
| `nurse_id` | 본인 식별 | "나" 발화의 매핑 대상 |
| `user_role` | ADM / HN / NURSE (자동 판정) | 권한 게이팅 |
| `year` / `month` | 오늘 기준 (또는 명시) | 기본 조회 월 |
| `conversation_id` | sessionStorage 자동 발급/재사용 | 페이지 reload 안전 |

→ 즉, "5월 원티드 미제출자" 라고만 발화해도 **본인 병동의** 5월 미제출자가 자동 조회.

권한 자동 결정:
- `is_master_admin=True` → ADM
- `is_head_nurse=True` 또는 `hn_auth='HN'` → HN
- 그 외 → NURSE

---

## 5. 시도해볼 만한 query

### 조회 (모든 권한)
- "5월 원티드 미제출자 누구야?"
- "내 5월 원티드 신청 내용"
- "야간 최대 횟수 설정 보여줘"
- "팀별 야간 배분 분석"
- "스케줄 정책 전체 보여줘"

### 설정 변경 (HN/ADM 전용)
- "연속 근무 최대 5일로 제한" → 미리보기 → "네" 로 확정
- "이브닝 다음날 데이 금지 해제"
- "박혜미 1팀으로 이동" → 프리셉터 매칭 검증 후 확정

### 정책 차단 / 권한 차단 확인
- "야간 최대 7회로 바꿔줘" → **policy_locked** 자동 거절 + 대안 안내
- (일반 NURSE) "연속 근무 4일로" → **권한 거절** "수간호사 권한 필요"

### 멀티 step
- "박혜미 5월 10일 원티드 취소하고 김민지를 대신 배치" → 단계별 확인
- "5월 근무표 위반사항 보여주고 자동으로 고쳐줘"

### Edge case
- "없는간호사 5월 원티드 보여줘" → 한국어 안내
- "13월 근무표 생성해줘" → 유효성 거절

전체 50건 시나리오는 `docs/AGENT_QA_SCENARIOS_2026-05-18.md` 참고.

---

## 6. 위젯 동작 디테일

### 6-1. 입력
- **Enter** → 전송, **Shift+Enter** → 줄바꿈
- 빈 메시지는 전송 안 됨

### 6-2. 대화 이력
- 현 페이지 panel은 in-memory (페이지 reload 시 panel은 비지만 conversation_id는 유지)
- **MSSQL/Redis에는 모든 turn 영구 저장** (감사 + 24h sliding hot cache)
- 24h 후 재접속 시에도 동일 conversation_id로 이어 발화 가능

### 6-3. 새 대화 시작
- panel 상단 🔄 버튼 → 서버에 pending_approval clear + sessionStorage 비움
- 다음 메시지부터 새 conversation_id 발급

### 6-4. 확인 대기 상태
- mutation 응답 후 "✔ 확인 대기 중 — 진행하시려면 '네' / 취소는 '아니오'" 안내
- 다음 메시지로 "네"/"아니오" 발화하면 처리

---

## 7. 라우트 인덱스 (frontend 참고)

| 메서드 | 경로 | 용도 |
|---|---|---|
| GET | `/api/agent/chat/whoami` | 현재 로그인 사용자 정보 (위젯 초기화) |
| POST | `/api/agent/chat/send` | 메시지 전송 (인증 필수) |
| POST | `/api/agent/chat/reset?conversation_id=...` | 세션 리셋 |

요청/응답 schema:
- `POST /send` request: `{ message, conversation_id?, year?, month? }`
- response: `{ answer, conversation_id, awaiting_approval, preview }`

cookie 기반 인증이므로 frontend에서 별도 token 헤더 불필요. fetch 시 `credentials: 'include'`만.

---

## 8. 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| 💬 버튼이 안 보임 | 로그인 안 됨 또는 access_token 만료. 페이지 새로고침 후 재로그인 |
| 401 응답 / "로그인 만료" 메시지 | cookie 만료. 로그아웃→재로그인 |
| 응답이 느림 (>10초) | OpenAI/Anthropic API 응답 지연. 키 / 네트워크 / cold start 확인 |
| "처리 중 오류" 500 | 백엔드 로그 확인 (`[chat] agent.run failed`). DB / LLM 키 점검 |
| 권한 거절 (예상 외) | role 자동 판정 결과 확인 → `whoami` 응답의 role 필드 |
| 다른 그룹 데이터가 보임 | 🚨 RBAC 위반 가능성. 즉시 운영팀 보고. `agent_skill_invocation` audit 확인 |
| policy_locked 응답 | 정상 — 정책-고정 field 변경 시도. 대안(`update_monthly_limit`) 안내 따라야 함 |
| 같은 질의에 다른 응답 | LLM 비결정성. 중요 의사결정은 preview 단계에서 사용자 확인 필수 |

---

## 9. 다른 페이지에 위젯 부착하기

### Frontend
Jinja2 템플릿의 `</body>` 직전에 1줄 추가:
- `{% include "_agent_floating_chat.html" %}`

별도 CSS/JS import 불필요 — partial 안에 모두 인라인 포함.

### Backend
이미 main.py에 등록됨 (`/api/agent/chat/*`). 추가 작업 없음.

### 의존성 체크
- 페이지가 인증 필요한 view인지 (미인증이면 위젯 자체 숨김)
- `<head>`에 charset UTF-8 (한국어 표시)
- z-index 충돌 (위젯은 `z-index: 2147483000` — 사실상 최상위)

---

## 10. Dev Test UI (`/agent/test`) — 참고

별도 dev tool. 사용 사례:
- 로그인 없이 시드 DB로 시험
- Office/Group/Nurse 임의 선택해서 권한별 동작 확인
- LLM trace 상세 확인 (운영 위젯보다 풍부한 debug 정보)

운영 환경에서는 비활성 권장 (또는 admin 전용 gate). 현재 라우터는 `/agent/test` 그대로 노출.

---

## 11. 운영 시연 시나리오 (5분)

| 흐름 | 시간 | 메시지 |
|---|---|---|
| 1) 로그인 후 대시보드 진입 | 30초 | floating 💬 클릭 |
| 2) 일상 조회 | 30초 | "이번 달 야간 분포 어때?" |
| 3) 본인 데이터 | 30초 | "내 5월 원티드 보여줘" |
| 4) 정책 차단 | 30초 | "야간 최대 7회로" → 자동 거절 |
| 5) 권한 차단 (Nurse role) | 30초 | "정책 변경 시도" → 거절 |
| 6) Multi-step | 1분 | "박혜미 5월 10일 원티드 취소" → preview → 확정 |
| 7) Infeasibility narrative | 1분 | "5월 근무표 만들어줘" → 자연어 원인 + apply_hint |
| 8) 새 대화 → 동일 사용자 fact 활용 | 30초 | 🔄 클릭 → 새 conv → 이전 학습 fact가 system prompt에 inject 확인 |

---

## 12. 관련 문서

| 문서 | 용도 |
|---|---|
| `docs/AGENT_MEMORY_DEVELOPER_GUIDE_2026-05-19.md` | 백엔드 메모리 아키텍처 / 운영 |
| `docs/AGENT_QA_SCENARIOS_2026-05-18.md` | 50건 QA 시나리오 정의 |
| `CLAUDE.md` | 에이전트 정책 / 안티패턴 |
| `app/templates/_agent_floating_chat.html` | 위젯 partial template |
| `app/agents_v2/chat_router.py` | 인증 chat 라우터 |

---

**End of guide.** 신규 사용자는 로그인 + 페이지 진입 + 💬 클릭으로 즉시 사용 가능. 별도 설정 불필요.
