# Concept Grounding Strategy

에이전트가 사용자 표현을 해석할 때, 먼저 개념 유형을 판별하고 해당 유형에 맞는 접지 전략을 사용한다.

## 1. Lexical Alias
짧고 안정적인 코드/별칭 토큰: V, OFF, D, E, N, 조정판, 확정본 등
→ shift definitions tool로 group별 코드 정의를 조회해서 canonical code/category로 확정

## 2. Temporal Concept
시간대 기반 표현: 아침 근무, 오전, 점심, 야간, 이브닝 등
→ shift definitions의 start_time/end_time을 조회해서, 해당 시간대와 겹치는 shift code 집합을 계산
→ "아침 = D"를 하드코딩하지 않는다. 병동마다 다를 수 있다.

## 3. Status Concept
상태 기반 표현: 쉬는사람, 근무자, 휴가자, 공가자, 대체자, 신규 간호사 등
→ scope(published_schedule / wanted_submissions / wanted_adjustment / draft_schedule)에 따라 의미가 달라진다
→ code category(leave/off/work)나 assignment status를 tool로 확인 후 predicate를 결정

## 4. Workflow Intent
행위 연쇄 표현: 적용 안되게, 반영 빼고, 다시 생성해, 해제하고 돌려줘, 취소해줘 등
→ mutation action chain으로 분해: set_field, bulk_update, generate, cancel 등
→ 각 action의 대상 scope와 파라미터를 확정

## 5. Entity
사람·조직 참조: 김민지, 박지은, 9병동, A팀 등
→ nurse/group/team 검색 tool로 조회
→ 동명이인이면 clarify, 유사 이름만 있으면 후보 제시, 0명이면 재확인
→ "신규 간호사" 같은 조건형 엔티티는 기준을 먼저 clarify (입사일? 경력?)

## 핵심 원칙
- 어떤 유형이든 먼저 tool로 근거를 확인한 뒤에만 canonical predicate/value를 확정한다
- 확인 없이 추정하면 병동마다 다른 코드 체계 때문에 오류가 생긴다
- 하나의 쿼리에 여러 유형이 섞일 수 있다 (e.g. "김민지의 아침 근무" = entity + temporal)
