# 변경 기록 + 원복 가이드 — "근무표 보여줘" 라우팅을 navigate 우선으로 (2026-05-29)

## 무엇을 / 왜 바꿨나
- **이전(백엔드 단독):** "5월 근무표 보여줘" → LLM 이 `query_schedule(scope=schedule)` 호출 →
  서버가 DB 조회·요약 → LLM 이 채팅 텍스트로 답. roster 데이터가 LLM 컨텍스트를 통과(토큰 비용),
  실제 화면 이동 없음.
- **이후(navigate 우선):** 조건(특정 간호사/날짜/시프트) 없는 "근무표 보여줘"는
  `navigate(target=roster_view)` 로 가서 **프론트가 자기 엔드포인트로 테이블을 렌더**.
  `query_schedule(scope=schedule)` 는 **필터/특정 사실 질의 전용**으로 좁힘.
- 동기: (1) roster 전체를 LLM 에 안 넣어 비용↓, (2) 실제 인터랙티브 테이블 = UX↑,
  (3) client-action(navigate) 아키텍처와 일관.

## 핵심: 코드 동작은 안 건드림, **프롬프트 라우팅 가이드만** 수정
- `query_schedule` 핸들러(`skills/query_schedule.py`)는 그대로. scope=schedule 의 필터/요약 로직 유지.
- 바뀐 것은 `app/agents_v2/skills/descriptions.py` 의 **자연어 설명(LLM 라우팅 신호)** 뿐.
- 분리 메커니즘(server skill vs client-action)도 그대로: `client_actions.py` / `agent_v3.py` 무변경.

## 변경 위치 (모두 `# [NAV_FIRST 2026-05-29]` 주석으로 표시)
`grep -n "NAV_FIRST 2026-05-29" app/agents_v2/skills/descriptions.py`

| 구간 | 내용 |
|---|---|
| query_schedule `schedule` scope 불릿 | "특정 조건 있는 조회에만" + ⛔ 전체보기는 navigate |
| query_schedule 예시 | "'5월 근무표 보여줘'(조건없음) → navigate(roster_view)" 추가 |
| validate 인접스킬 경계 | "특정 셀→query_schedule / 전체화면→navigate" 로 분리 |
| navigate 상단 desc | "조건없는 근무표 보여줘 → roster_view" 추가 |
| navigate 모호성 노트 | 기본값 규칙(전체→roster_view, 내→roster_view_my) 추가 |
| navigate target 목록 | roster_view = 기본 목적지 명시 |
| navigate 예시 | "'근무표 보여줘' → roster_view" 예시 추가 |

## 백엔드 단독으로 원복하는 법
1. `grep -n "NAV_FIRST 2026-05-29" app/agents_v2/skills/descriptions.py` 로 7개 마커 위치 확인.
2. 각 마커 주석 + 그 직후 추가된 문장/⛔/예시 줄을 제거하고, 마커에 적힌 "원복 시 …" 지시대로
   원문(`'단순히 근무표 셀이 보고 싶다' → query_schedule(scope='schedule')` 등)으로 되돌린다.
3. `query_schedule` 핸들러는 변경된 적 없으므로 손댈 것 없음.
4. 검증: `uv run --with pytest python -m pytest tests/test_agent_v3.py tests/agent_qa/ -q`
   (전부 deterministic double 기반이라 통과 유지) + 실 LLM 으로 "근무표 보여줘" 가 다시
   `query_schedule(scope=schedule)` 로 가는지 `/agent/test/chat` 확인.

## 주의
- 라우팅은 실 LLM 판단이라 100% 결정론적이지 않음. 표현이 애매한 케이스("근무표 좀")는
  navigate 모호성 노트에 따라 되묻거나 roster_view 기본값으로 갈 수 있음.
- 필터 있는 "김민지 5월 근무 보여줘" 는 의도적으로 **여전히 query_schedule** 로 남김(인라인 답).
