# Workflow Patterns

에이전트가 처리하는 주요 워크플로 패턴. 각 패턴은 여러 tool을 특정 순서로 조합한다.

## 1. Query + Aggregate
단순 조회 후 집계/정렬/필터링.
예: "이번 달 원티드 신청 많은 순으로 직원별 집계해줘"
흐름: scope 확정 → 데이터 조회 → 집계/정렬 → 응답

## 2. Query + Grounding + Filter
조회 전에 도메인 개념 접지가 필요한 경우.
예: "4월 3주차 야간근무자 명단 알려줘"
흐름: temporal grounding (야간 → shift codes) → schedule 조회 → 해당 코드 필터 → 응답

## 3. Constraint Update → Generate
제약 설정을 변경한 뒤 근무표를 재생성.
예: "연속 나이트 최대 2일로 제한해서 배치해줘"
흐름: 제약 설정 변경 → preview → approval → 근무표 생성

## 4. Bulk Mutation → Generate
일괄 수정 후 재생성.
예: "V요청한 애들 다 적용 안되게 해서 근무표 생성해"
흐름: code grounding → 대상 row 조회 → preview → approval → bulk update → generate

## 5. Single Entry Edit
근무표 단건 수정.
예: "4월 12일 김민지를 D에서 E로 바꿔줘"
흐름: entity grounding → schedule 조회 → 현재 값 확인 → 불일치 시 clarify → update → 응답

## 6. Recommend Candidates
조건에 맞는 후보를 추천.
예: "응급 오프 가능 후보 5명 추천해줘"
흐름: 조건 확정 → schedule/nurse 데이터 조회 → 우선순위 기준 적용 → 후보 산출 → 응답

## 7. Validate + Report
근무표 검증 또는 분석 리포트.
예: "전체 근무표 공정성 분석해서 리포트 보여줘"
흐름: schedule 조회 → 제약 위반 검출 or 통계 계산 → 리포트 생성 → 응답

## 8. Repair / Rebalance
기존 근무표를 부분 수정하거나 재배치.
예: "야간 편차 줄여서 재조정해줘"
흐름: 현재 상태 분석 → 문제 구간 식별 → 수정안 생성 or 재생성 파라미터 조정 → preview → 실행

## 공통 규칙
- 변경/생성이 포함된 워크플로는 반드시 preview → approval 단계를 거친다
- scope가 불명확하면 워크플로 시작 전에 clarify한다
- 여러 워크플로가 합쳐질 수 있다 (e.g. Query + Recommend, Constraint Update + Generate)
