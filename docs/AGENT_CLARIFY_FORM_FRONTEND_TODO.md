# [프론트 핸드오프 예정] 다중 clarify → 선택형 clarify_form

## 배경
5-행동 복합쿼리에서 여러 태스크가 각각 되물음(clarification)을 필요로 할 때, 현재는
**첫 되물음에서 멈춰 순차 왕복**한다(5개 빠지면 5번 왕복). 자유 타이핑 강제라 UX 나쁨.

## 방향 (Claude Code escalation 방식 = AskUserQuestion)
- 백엔드가 실행 **전 pre-flight**로 계획 전체를 스캔 → **빠진/모호한 파라미터를 다 수집**
- **하나의 구조화 payload** `clarify_form`으로 방출 → 프론트가 **선택 UI**로 렌더
- 열거 가능(병동/간호사/시프트/승인범위)=칩·드롭다운(DB 그라운딩), 스칼라(날짜/횟수)=프리셋+입력,
  **항상 자유입력 탈출구(Other)**

## 백엔드가 낼 payload (계약안)
```json
{ "type": "clarify_form",
  "questions": [
    {"param":"ward","q":"어느 병동으로 파견?","type":"select","options":["중환자실1","응급실"]},
    {"param":"start_date","q":"파견 시작일?","type":"date","presets":["다음 달 1일"]},
    {"param":"approve_scope","q":"원티드 승인 범위?","type":"select",
     "options":["제출된 전원","직접 선택"]}
  ] }
```
- 옵션은 백엔드가 @skill manifest 파라미터 스키마(required·enum)에서 자동 도출 + DB 열거.

## 프론트 필요 작업 (추후 요청)
1. `clarify_form` UI-action 타입 렌더러 (AgentChatWidget): select→칩/드롭다운, date→피커,
   number→스텝퍼, 각 질문에 "직접 입력" 탈출구
2. 사용자 선택 결과를 `{param: value}` 맵으로 백엔드에 회신 → 백엔드가 파라미터 채워 재개
3. 기존 named UI-action 계약([[project_agent_frontend_handoff]]) 확장선상

## 상태
- 백엔드 clarify_form **생성**은 집합값 Tier1 이후 착수 예정
- 프론트 렌더링은 이 문서로 추후 핸드오프
