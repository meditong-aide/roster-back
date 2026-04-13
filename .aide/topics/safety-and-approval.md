# Safety and Approval Policy

## Preview 필수 작업
다음 작업은 실행 전에 반드시 영향 범위를 먼저 보여줘야 한다:
- bulk update (원티드 조정판 일괄 수정 등)
- 근무표 생성 (generate)
- 근무표 발행 (publish)
- 근무표 단건/다건 수정
- 원티드 취소/삭제

## Approval 필수 작업
다음 조건에 해당하면 사용자 확인을 받아야 한다:
- 영향 대상이 10건 이상
- 근무표 생성을 트리거하는 경우
- 다른 사용자의 데이터를 수정하는 경우
- 이미 적용된 요청을 해제하는 경우

## RBAC 규칙
- 일반 간호사: 본인의 원티드/프로필만 조회·수정 가능
- 수간호사(HN): 본인 병동의 전체 데이터 조회·수정·생성 가능
- 관리자(ADM): 전체 병원 데이터 접근 가능
- 다른 사람의 원티드 메모는 수간호사도 수정 불가 (본인만 가능)

## 위험 판단 기준
- read-only 작업: 위험 없음, 바로 실행
- single write: 현재 값 확인 후 실행
- bulk write: preview 필수
- bulk write + generate: preview + approval 필수
- 발행(publish): 반드시 사전 검증(validate) 후 approval

## Clarify 우선 상황
- scope가 불명확할 때 (근무표인지 원티드인지)
- 대상 기간이 불명확할 때
- 동명이인이 있을 때
- "취소"의 의미가 모호할 때 (is_applied=false vs 삭제 vs 철회)
- "신규"의 기준이 불명확할 때
