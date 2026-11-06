# AIDE 에이전트 — 위젯에서 백엔드까지 (2026-06-12)

> TypeScript/리액트 몰라도 흐름이 이해되도록, "버튼 누르면 무슨 일이 벌어지나"를 따라가는 문서.

## 1. 전체 그림 (한눈에)

```
┌─────────────────────────────────────────────────────────────┐
│   [FRONTEND]  React 앱 (브라우저)                              │
│                                                             │
│   ① 우측하단 갈색 FAB (둥근 버튼)                                │
│        │ 클릭                                                │
│        ▼                                                    │
│   ② 슬라이드 패널 — 채팅창                                       │
│        │ 메시지 입력 + 엔터                                     │
│        ▼                                                    │
│   ③ useAgentChat 훅 ─ "보내기" 담당                            │
│        │ HTTP POST                                          │
│        ▼                                                    │
│   ④ sendAgentMessage  →  /api/agent/chat/send  (axios)      │
│                                                             │
└───────────────────────┬─────────────────────────────────────┘
                        │  네트워크
                        ▼
┌─────────────────────────────────────────────────────────────┐
│   [BACKEND]  FastAPI (포트 :8001, agent worktree)             │
│                                                             │
│   ⑤ chat_router  (POST /send)                               │
│        │                                                    │
│        ▼                                                    │
│   ⑥ SchedulingAgent.run                                     │
│        │  ┌──────────────────────────────────────┐          │
│        │  │  Agent 루프 (LLM이 운전)                │          │
│        │  │  - system prompt 구성                  │          │
│        │  │  - 스킬 목록(자기설명) 노출              │          │
│        │  │  - LLM ↔ Tool 호출 반복                │          │
│        │  └──────────────────────────────────────┘          │
│        ▼                                                    │
│   ⑦ Skills (자기설명 + 내부 grounding)                          │
│      - query_schedule   (조회)                              │
│      - bulk_mutation    (배치 수정)                          │
│      - update_*         (단건 수정)                          │
│      - navigate/prefill (UI 위임 = client-action)            │
│      - generate / validate / repair / recommend ...         │
│                                                             │
│   ⑧ ChatResponse 응답 (JSON)                                 │
│      { answer, conversation_id, ui_actions[], ... }         │
└───────────────────────┬─────────────────────────────────────┘
                        │  응답
                        ▼
┌─────────────────────────────────────────────────────────────┐
│   [FRONTEND] (계속)                                          │
│                                                             │
│   ⑨ useAgentChat 가 응답 수신                                  │
│        │                                                    │
│        ├─ answer        → 채팅창에 말풍선으로 표시               │
│        │                                                    │
│        └─ ui_actions[]  → runUiActions 호출                  │
│                              │                              │
│                              ▼                              │
│   ⑩ uiActions 서비스 — "navigate target/sub" 해석              │
│        │  TARGET_ROUTES['nurse_management'] = '/head_nurse_management'│
│        ▼                                                    │
│   ⑪ react-router navigate('/head_nurse_management',         │
│                    { state: { section: 'team_setting' } })  │
│        │                                                    │
│        ▼                                                    │
│   ⑫ 목적지 페이지의 useEffect → modalGate 가 모달/탭 결정         │
│      - 같은 모달 재오픈 X (중첩 방지)                            │
│      - team↔grade 상호 배타                                  │
│      - 같은 탭이면 setState skip                              │
└─────────────────────────────────────────────────────────────┘
```

## 2. 프론트엔드 — "갈색 동그라미"부터 시작해 모달 열기까지

### 2.1 위젯이 어디에 사는가

| 무엇 | 어디 | 한 줄 설명 |
|---|---|---|
| 위젯 컴포넌트 | `src/components/feature/aide-widget/AgentChatWidget.tsx` | 우측하단 FAB + 슬라이드 패널 본체 |
| 앱에 마운트 | `src/App.tsx` `<AgentChatWidget />` | 로그인된 상태에서만 노출 |
| 채팅 로직 훅 | `src/hooks/useAgentChat.ts` | 메시지 누적·전송·응답 처리 |
| HTTP 호출 | `src/service/agentChat.ts` | `POST /api/agent/chat/send` |
| 백엔드 주소 토글 | `.env.dlocal`: `VITE_AGENT_API_BASE_URL=http://localhost:8001` | 메인 API(:8000)와 에이전트(:8001) 분리 |

### 2.2 위젯이 하는 일 — 입력→전송→표시 (3단계)

1. **입력 받기**
   - 우측하단 FAB 클릭 → 패널이 열리고 textarea 포커스
   - 한글 IME(조합 중) Enter 는 무시 → "ㅎ→ㅏ→안" 음절 확정용. Shift+Enter는 줄바꿈, 그냥 Enter는 전송.

2. **백엔드에 보내기** (`useAgentChat.send`)
   - 사용자 말풍선을 먼저 화면에 띄움
   - HTTP POST 페이로드:
     ```json
     {
       "message": "팀 어디서 바꿔?",
       "conversation_id": "abc-123",       // 이전 대화 이어가기
       "ui_metadata": { "current_route": "/roster_view" }
     }
     ```

3. **응답 처리**
   - `answer` → 에이전트 말풍선
   - `ui_actions[]` → **즉시 페이지 전환/모달 오픈 수행**

### 2.3 UI 의도(`ui_actions`)의 정체

백엔드는 컴포넌트나 라우트 경로를 모릅니다. 대신 **논리적 의도**만 내려보냅니다.

```json
{
  "action": "navigate",
  "target": "nurse_management",
  "sub": "team_setting"
}
```

이걸 프론트의 매핑 테이블(`TARGET_ROUTES`)이 실제 경로로 번역합니다.

| target (백엔드 enum) | 실제 라우트 | 설명 |
|---|---|---|
| `nurse_management` | `/head_nurse_management` | 근무자관리 |
| `config` | `/roster_configure` | 설정 화면 |
| `roster_view` | `/roster_view` | 근무표 보기 |
| `wanted` | `/roster_wanted` | 원티드 |
| `roster_create` | `/roster_create` | 근무표 만들기 |
| `roster_view_my` | `/roster_view/my` | 내 근무표 |
| `dashboard` | `/roster_dashboard` | 대시보드 |
| `mypage` | `/myPage` | 마이페이지 |
| `home` | `/` | 홈 |
| `support` | `/support` | 지원 |

`sub`는 페이지 안의 모달/탭을 지정합니다 (예: `team_setting` → 팀설정 모달, `weekoff` → 주휴/오프 탭).

### 2.4 모달 중첩 방지 — `modalGate`

같은 응답이 두 번 와도 모달이 두 번 안 뜨도록 두 단계 방어:

- **1차** `runUiActions` 시그니처 dedupe — 같은 (action, target, sub) 은 한 번만 실행
- **2차** 페이지의 `modalGate` 헬퍼 — 이미 같은 모달이 열려있으면 무시, 다른 모달이 열려있으면 닫고 새 모달 열기 (상호 배타)

## 3. 백엔드 — `/send` 가 받고 어떻게 답하나

### 3.1 엔드포인트 요약

| 무엇 | 어디 |
|---|---|
| 라우터 | `app/agents_v2/chat_router.py` (`prefix=/api/agent/chat`) |
| 진입점 | `SchedulingAgent.run()` (`app/agents_v2/agent_v3.py`) |
| 스킬 정의 | `app/agents_v2/skills/*.py` |
| UI 위임 스킬 | `app/agents_v2/skills/client_actions.py` |
| 스킬 설명서 | `app/agents_v2/skills/descriptions.py` |

### 3.2 한 턴이 처리되는 흐름

```
사용자 메시지 도착
     │
     ▼
[1] system prompt 빌드
     - 도메인 지식 + 사용자 기억 + 보안 경계
     - 가용 스킬 목록(자기설명) 포함
     │
     ▼
[2] 대기 중인 승인이 있나?  ─── 있고 사용자 답이 "예/아니오"면 → 바로 그 분기 실행
     │  없으면 ↓
     ▼
[3] LLM ↔ Tool 루프  (DeepAgent 패턴)
     LLM이 보고 결정: "어떤 스킬을 어떤 파라미터로 호출할까?"
        │
        ├─ navigate/prefill 호출   →  ui_actions 누적 (실제 DB 조작 X)
        ├─ query_schedule 호출     →  DB 조회 결과를 LLM에 다시 줌
        ├─ update_person_attr 호출 →  DB 수정 + 결과 반환
        ├─ generate_schedule 호출  →  비동기 작업 큐(SQS)에 넣고 job_id 반환
        └─ ... (총 9개 스킬)
     │
     ▼
[4] LLM이 더 호출할 게 없다고 판단 → 최종 자연어 답변 생성
     │
     ▼
[5] ChatResponse 응답
     { answer, conversation_id, ui_actions, awaiting_approval, ... }
```

### 3.3 스킬 9종 — 무엇을 할 수 있나

| 스킬 | 한 줄 설명 | DB 조작? |
|---|---|---|
| `query_schedule` | 근무표·원티드·간호사 정보 조회 | 읽기만 |
| `bulk_mutation` | 일괄 승인/거부·일괄 시프트 변경 | 쓰기 |
| `update_constraint` | 스케줄링 제약(N 연속, OFF 최소…) 수정 | 쓰기 |
| `update_person_attr` | 간호사 팀/등급/경력 수정 | 쓰기 |
| `generate_schedule` | 근무표 자동 생성 (비동기) | 작업 큐 |
| `validate_schedule` | 제약 위반 검증 | 읽기 |
| `recommend_candidates` | 대체 간호사 추천 | 읽기 |
| `repair_schedule` | 기존 근무표 보정 | 쓰기 |
| `analyze_report` | 공정성·분포 리포트 | 읽기 |
| **`navigate` / `prefill`** | **(클라이언트 액션) 화면 전환·폼 프리필** | **없음 — UI만 위임** |

### 3.4 스킬의 핵심 규약 — "스스로 설명한다, 스스로 grounding한다"

LLM이 어떤 스킬을 부를지 정하므로, 각 스킬은:

- **자기설명 (description)**: 무엇을 하는지·언제 쓰는지·파라미터 의미·예시를 자연어로 적어둠. LLM이 프롬프트에서 읽음.
- **내부 grounding**: "이유림" 같은 이름은 스킬이 직접 DB 조회해 `nurse_id`로 변환. "나이트"→`N`, "데이"→`D` 같은 한국어→코드 매핑도 스킬 내부에서. LLM에는 사용자가 말한 그대로 흘려보냄.

이래서 코드에 **regex/lookup table** 으로 의도를 파싱하지 않습니다 (CLAUDE.md 안티패턴).

## 4. 끝까지 따라가보기 — 예시 한 줄

> 사용자: **"팀 어디서 바꿔?"**

```
[FRONT]  텍스트 입력 → 엔터
   │
   ▼ POST /api/agent/chat/send
   { message: "팀 어디서 바꿔?", ui_metadata: { current_route: "/roster_view" } }

[BACK]   chat_router → SchedulingAgent.run
   │
   ▼ LLM 판단: "사용자가 팀 설정 위치를 묻는다 → 화면 전환 의도"
   navigate 스킬 호출:
     { target: "nurse_management", sub: "team_setting" }
   │
   ▼ LLM 최종 답변 생성: "근무자관리 → 팀설정에서 변경할 수 있어요."

[BACK]   응답 JSON:
   {
     "answer": "근무자관리 → 팀설정에서 변경할 수 있어요.",
     "conversation_id": "abc-123",
     "ui_actions": [
       { "action": "navigate", "target": "nurse_management", "sub": "team_setting" }
     ]
   }

[FRONT]  useAgentChat 응답 수신
   │
   ├─ 채팅창에 답변 말풍선 추가
   │
   └─ runUiActions → uiActions.resolveUiAction
        │ TARGET_ROUTES["nurse_management"] = "/head_nurse_management"
        ▼
   react-router navigate("/head_nurse_management", { state: { section: "team_setting" } })
        │
        ▼
   페이지 useEffect → modalGate.resolveNurseMgmtModal("team_setting")
        → { team: true, grade: false }     ← 팀설정 모달이 열림
```

## 5. 환경 분리 (포트)

| 백엔드 | 포트 | 용도 | 사용 브랜치 |
|---|---|---|---|
| 메인 API | `:8000` | 근무표 생성·조회·관리 (production 경로) | `feat/mssql-transition` 계열 |
| 에이전트 | `:8001` | LLM tool-loop, 위젯 호출 받는 곳 | `feature/agent-interface` (현재 worktree) |

프론트 `.env.dlocal` 두 줄로 분리:
```
VITE_API_BASE_URL=http://localhost:8000           # 메인
VITE_AGENT_API_BASE_URL=http://localhost:8001     # 에이전트
```

## 6. 아키텍처 원칙 (CLAUDE.md 재진술)

- **LLM-first**: 자연어 의도 분석은 LLM 담당. regex/lookup 금지.
- **Top-down**: 사용자 입력의 전체 의도 → 어떤 스킬을 어떤 순서로 부를지 plan → 스킬은 자기설명·내부 grounding로 처리.
- **Skill self-description**: 새 스킬은 LLM이 읽을 description 필수.
- **No fragment grounding**: 입력을 조각내서 각각 매핑하지 않음. 전체 의도 → 스킬에 위임.
- **Thin memory**: 병원·병동별 데이터는 프롬프트에 박지 않음. 런타임 DB 조회.
- **Transparent pipeline**: intent → plan → execute → verify 모든 단계가 추적 가능.

## 7. 관련 파일 빠른 인덱스

### 프론트엔드 (`nurse_rostering_front/roster_front`)
- `src/components/feature/aide-widget/AgentChatWidget.tsx` — 위젯 UI
- `src/hooks/useAgentChat.ts` — 채팅 훅
- `src/service/agentChat.ts` — HTTP 클라이언트
- `src/service/uiActions.ts` — target/sub → route 매핑 + dedupe
- `src/service/modalGate.ts` — 모달 상태 결정 (순수 함수)
- `src/css/tailwind.css` — `--aide-*` 디자인 토큰 (Claude tone)
- `src/App.tsx` — 인증 상태에서 위젯 마운트

### 백엔드 (`roster-back-agent`, `feature/agent-interface`)
- `app/agents_v2/chat_router.py` — `/api/agent/chat/*` 엔드포인트
- `app/agents_v2/agent_v3.py` — `SchedulingAgent.run` (메인 루프)
- `app/agents_v2/skills/` — 9개 도메인 스킬
- `app/agents_v2/skills/client_actions.py` — `navigate` / `prefill` 정의 + `NAVIGATE_TARGETS` enum
- `app/agents_v2/skills/descriptions.py` — 스킬별 자연어 설명 (LLM이 읽음)
- `app/agents_v2/contract/client_action_targets.json` — 프론트와 공유하는 target 스냅샷(SSOT)
- `scripts/dump_client_action_targets.py` — 위 JSON 동기화 스크립트
