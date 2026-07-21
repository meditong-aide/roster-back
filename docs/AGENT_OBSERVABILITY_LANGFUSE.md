# AIDE Agent 관측 — Langfuse (self-host)

에이전트의 **매 턴을 trace**, **매 LLM 호출을 generation**, **파이프라인 Stage 를 span**,
**턴 결과(outcome)를 score** 로 Langfuse 에 적재한다. Langfuse UI 에서 개별 턴을 waterfall
로 드릴다운하고, 실패(verification_failed 등)로 필터·검색할 수 있다.

## 왜 self-host

트레이스에 **간호사 실명·근무표·병동 배정이 원문으로** 담긴다. 의료 데이터라 외부(Langfuse
Cloud)로 내보내지 않고 **병원 내부 인스턴스**에만 적재한다. `LANGFUSE_HOST` 를 내부 URL 로.

## 키 게이팅 (중요)

`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` **셋 다** 있고 `langfuse`
패키지가 설치돼 있을 때만 활성. 하나라도 없으면 **완전 no-op** — 코드 경로·성능·테스트에
영향 0. 모든 Langfuse 호출은 try/except 로 감싸 **실패해도 에이전트 턴을 절대 깨지 않는다**.

## 셋업

### 1) Langfuse self-host 띄우기

```bash
git clone https://github.com/langfuse/langfuse
cd langfuse
docker compose up -d            # web + postgres + clickhouse + redis
# 기본 http://localhost:3000
```

브라우저에서 접속 → 회원가입(첫 계정) → 프로젝트 생성 → **Settings → API Keys** 에서
Public/Secret 키 복사.

### 2) 백엔드 .env 에 키 주입

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx
LANGFUSE_HOST=http://<내부호스트>:3000    # 예: http://localhost:3000
```

### 3) 패키지 설치

```bash
pip install "langfuse>=2.53,<3"     # requirements.txt 에 이미 있음
```

### 4) 서버 재기동 → 자동 활성

로그에 `[obs] Langfuse 활성 — host=...` 가 뜨면 켜진 것. 안 뜨면(키 누락 등) no-op.

## 무엇이 적재되나

| Langfuse 개념 | AIDE | 내용 |
|---|---|---|
| **trace** | 턴 1개 | session=conversation_id, user=nurse_id, input=사용자 발화, output=최종 답변, metadata.group_id |
| **generation** | LLM 호출 | name=용도(router/turn/planner/llm), model, input=메시지, output, usage=토큰 |
| **span** | 파이프라인 Stage | routing / plan / plan_staleness 등 (status·detail·duration) |
| **score** | 턴 outcome | OK / CLARIFICATION / PREVIEW / VERIFICATION_FAILED / PERMISSION_DENIED / GENERIC_ERROR |

## 구현

- `app/agents_v2/observability.py` — 키 게이팅 + trace/generation/span/score API. contextvar
  로 현재 trace·purpose 격리(동시 요청 안전).
- `llm_client.get_llm_client / get_router_llm_client` — 생성 클라이언트를 `wrap_client` 로 감싸
  매 `.chat()` 을 generation 으로(키 있을 때만; 없으면 원본 그대로).
- `agent_v3.run()` — 턴 전체를 `obs.turn(...)` 으로 감싸고 종료 시 `finish_turn`(출력·Stage·score).
- purpose 태그: 라우터=`router`, 메인 턴=`turn`, DAG 플래너=`planner`.

## 비용/사용량과의 관계

기존 `agent_llm_usage` 테이블(토큰·비용 집계, `GET /api/agent/usage`)은 **그대로 유지**된다.
Langfuse 는 그 위에 **턴 내부 트레이스(디버깅·검증)** 를 더한 것 — 둘은 상호보완.
