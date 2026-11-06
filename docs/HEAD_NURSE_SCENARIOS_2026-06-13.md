# 수간호사 시나리오 — AIDE 에이전트 활용 가이드 (2026-06-13)

> 수간호사(HN) 입장의 실 업무 흐름을 자연어 → 스킬 → 화면으로 매핑.
> "이런 말 하면 이 화면으로 가서 이걸 할 수 있다"의 단일 참조표.

## 0. 전체 시간축 — 월 단위 업무 사이클

```
[월초 D-15 ~ D-1]      [당월 D ~ D+30]         [월말]
 생성 준비              운영·조정              회고·정산
   │                     │                     │
   ▼                     ▼                     ▼
 설정·원티드·인원       승인·변경·대체        분포 분석·인수인계
```

각 단계의 일거리와 우리 agent가 받을 발화를 아래에 표로 정리.

---

## 1. 월초 — 생성 준비

### 1.1 정책/설정 점검

| HN 발화 (자연어) | 매칭 스킬 | 결과 화면 |
|---|---|---|
| "월 오프수 제한 어디서 봐?" | navigate(target=config, sub=month_off) | `/roster_configure` tab 0 |
| "근무 코드 설정 보여줘" | navigate(target=config, sub=shift_codes) | `/roster_configure` tab 1 |
| "주휴/오프 설정 어디?" | navigate(target=config, sub=weekoff) | `/roster_configure` tab 3 |
| "원티드 마감일 설정" | navigate(target=config, sub=wanted_setting) | `/roster_configure` tab 4 |
| "전직원 7월 오프 11개로 해줘" | update_constraint(field=off_days, value=11) | (인플레이스 실행) |
| "주말 연속 근무 한도 알려줘" | update_constraint(read) | 답변 텍스트 |

### 1.2 인원·등급·팀 정책

| HN 발화 | 스킬 | 비고 |
|---|---|---|
| "팀별 최소 인원 뭐로 되어 있어?" | manage_team_min(read) | 답변 |
| "A팀 나이트 최소 2명으로 해줘" | manage_team_min(set) | preview→confirm |
| "등급별 인원 정책 보여줘" | manage_grade(read) | 답변 |
| "시니어 최소 2명으로 설정" | manage_grade(set_requirement) | preview→confirm |
| "등급 명 바꿔줘" | manage_grade(set_grade_name) | preview→confirm |
| "이번 달 grade cascade 적용돼?" | manage_grade(read) | allow_soft_fallback 표시 |

### 1.3 근무자 명단 점검 (전입/전출 SSOT 활용)

| HN 발화 | 스킬 | 비고 |
|---|---|---|
| "이번 달 우리 병동 누구 있어?" | query_schedule(scope=nurses, year, month) | **SSOT 적용** — 전입자 포함·전출자 제외 |
| "이유림 어느 팀이야?" | query_schedule(scope=nurses, nurse_ids=[이유림]) | 답변 |
| "전입자 누구야?" (현재 갭) | — | **신규 스킬 필요?** members status=inbound 필터 |
| "이유림 A팀으로 바꿔줘" | update_person_attr(team_id=A) | **SSOT 기록** — NurseTeamPeriod+캐시 양쪽 |
| "김민지 시니어로 변경" | update_person_attr(grade=2) | preview→confirm |
| "박지은 야간 전담으로" | update_person_attr(is_night_nurse=True) | preview→confirm |

### 1.4 원티드 운영

| HN 발화 | 스킬 | 비고 |
|---|---|---|
| "원티드 마감일 7월 10일로" | update_constraint(wanted_deadline=2026-07-10) | preview→confirm |
| "이번 달 원티드 미제출자 누구야?" | query_schedule(scope=wanted_submissions, filter=미제출) | 답변+표 |
| "원티드 일괄 승인" | bulk_mutation(scope=wanted, action=approve) | preview→confirm |
| "김민지 원티드 거부" | bulk_mutation(scope=wanted, nurse_ids=[김민지], action=deny) | preview→confirm |

### 1.5 생성

| HN 발화 | 스킬 | 비고 |
|---|---|---|
| "7월 근무표 만들어줘" | generate_schedule(year=2026, month=7) | 비동기 SQS, job_id 반환 |
| "근무표 만들기 어디서 해?" | navigate(target=roster_create) | `/roster_create` |
| "생성 진행 상황 보여줘" (현재 갭) | — | **신규 스킬 필요?** query_generation_job_status |
| "infeasible 인데 어떻게 풀어?" | (apply_hint 흐름) | 자동 — preview 옵션 카드 |

---

## 2. 월중 — 운영·조정

### 2.1 조회

| HN 발화 | 스킬 | 비고 |
|---|---|---|
| "이번 달 근무표 보여줘" | navigate(target=roster_view, query={month}) | `/roster_view` |
| "내 근무표" | navigate(target=roster_view_my) | `/roster_view/my` |
| "5월 3일 나이트 누가 들어가?" | query_schedule(scope=schedule, date, shift_codes=[N]) | 답변+표 (data 인라인) |
| "이유림 5월 근무표" | query_schedule(scope=schedule, nurse_ids=[이유림]) | 답변+표 |
| "이번 달 그레이드 1 데이 근무자" | query_schedule(scope=schedule, grade=1, shift_codes=[D]) | 답변+표 |
| "팀별 최소 안 채워진 곳" | query_schedule(scope=teamlist, filter=under_min) | 답변 (data 인라인) |

### 2.2 변경

| HN 발화 | 스킬 | 비고 |
|---|---|---|
| "5월 3일 김민지 나이트로 바꿔" | bulk_mutation(schedule, nurse=김민지, date, new_shift_code=N) | preview→confirm |
| "5월 3일 김민지 자리 OFF로" | bulk_mutation(schedule, date, new_shift_code=OFF) | preview→confirm |
| "이 자리 누가 대체 가능?" | recommend_candidates(date, shift) | 답변+후보 리스트 |
| "김민지 대신 들어갈 사람 추천" | recommend_candidates(replacing=김민지) | 답변+후보 리스트 |
| "위반 사항 있나?" | validate_schedule | 답변+위반 리스트 |
| "왜 이렇게 짜졌어?" | validate_schedule | 답변 (제약 위반 근거) |
| "이 근무표 교정해" | repair_schedule | 제안 (auto-apply X) |

### 2.3 긴급 대체

| HN 발화 | 스킬 | 비고 |
|---|---|---|
| "오늘 김민지 결근, 대체 누구?" | recommend_candidates(date=today, replacing=김민지) | 답변+후보 |
| "그 사람으로 바꿔" | bulk_mutation(schedule, ...) | preview→confirm |

---

## 3. 월말 — 회고·정산

| HN 발화 | 스킬 | 비고 |
|---|---|---|
| "이번 달 OFF 분포 분석" | analyze_report(metric=off_distribution) | 답변+표 |
| "팀별 나이트 분배 공정성" | analyze_report(metric=night_fairness, by=team) | 답변+표 |
| "4월하고 5월 비교" | analyze_report(comparison=true, months=[4,5]) | 답변+표 |
| "간호사별 근무 부담" | analyze_report(metric=workload, by=nurse) | 답변+표 |
| "주말 근무 분포" | analyze_report(metric=weekend) | 답변+표 |
| "공정성 점수" | analyze_report(metric=fairness_score) | 답변 |

---

## 4. 현재 갭 (사용자 가치 가능성 순)

| # | 갭 | 영향 | 우선순위 |
|---|---|---|---|
| G1 | **전입/전출 명단 자연어 조회** ("전입자 누구?", "이번 달 새로 온 사람") | 월초 점검 단축 | **P0** |
| G2 | **생성 job 상태 조회** ("생성 어디까지 갔어?", "끝났어?") | 생성 흐름 가시화 | **P0** |
| G3 | **OFF/D/E/N 시프트별 인원 부족 알람** ("월 어느 날 D 인원 모자라?") | 사전 조정 | **P1** |
| G4 | **bulk_mutation 시 팀 그라운딩 SSOT** ("A팀 전부 토요일 OFF") | 시점 기반 팀 멤버 보장 | **P1** |
| G5 | **개인 통계 인라인** ("김민지 이번 달 통계") | front 가 이미 dev에 데이터 있음 — agent 매핑만 | **P1** |
| G6 | **공정성 비교 리포트 자동화** ("지난 분기 대비 공정성") | analyze_report 확장 | **P2** |
| G7 | **권한별 안내** ("일반 간호사가 hn-only 기능 물어봤을 때 가이드") | UX | **P2** |
| G8 | **다국어** (영어 모드) | 향후 | P3 |

---

## 5. 프론트와 백 매핑 요약표

| 프론트 라우트 | 백 스킬 (대표) | navigate target | sub |
|---|---|---|---|
| `/roster_dashboard` | (없음 — view-only) | dashboard | — |
| `/roster_wanted` | bulk_mutation, query_schedule | wanted | — |
| `/roster_view` | query_schedule | roster_view | — |
| `/roster_view/my` | query_schedule(nurse_ids=[self]) | roster_view_my | — |
| `/head_nurse_management` | update_person_attr, manage_grade, manage_team_min | nurse_management | team_setting / grade_setting |
| `/roster_create` | generate_schedule | roster_create | — |
| `/roster_configure` (tab 0) | update_constraint, manage 등 | config | month_off / (default) |
| `/roster_configure` (tab 1) | update_constraint(shift) | config | shift_codes |
| `/roster_configure` (tab 3) | update_constraint(weekoff) | config | weekoff |
| `/roster_configure` (tab 4) | update_constraint(wanted) | config | wanted_setting |
| `/myPage` | — | mypage | — |
| `/support` | — | support | — |

---

## 6. 즉시 활용 가능한 데모 발화 10개

수간호사 사용자에게 처음 보여줄 핵심 시나리오:

1. **"팀 어디서 바꿔?"** → 근무자관리 팀설정 모달
2. **"이유림 A팀으로 바꿔줘"** → preview → "그래" → SSOT 기록
3. **"이번 달 원티드 미제출자 누구야?"** → 자연어+표
4. **"7월 근무표 만들어줘"** → infeasible 시 apply_hint 옵션
5. **"5월 3일 나이트 누가 들어가?"** → 자연어+표
6. **"이 자리 대체 후보 추천해줘"** → 후보 리스트
7. **"위반 사항 있나?"** → 위반 리스트
8. **"OFF 분포 보여줘"** → 분포 리포트
9. **"전직원 7월 OFF 11개로"** → preview → confirm
10. **"근무 코드 설정 어디?"** → 설정 화면 탭 1 자동 오픈

---

## 7. 권한 모델 (현재)

| 발화 유형 | 일반 간호사 | 수간호사 |
|---|---|---|
| 본인 근무표 조회 | ✅ | ✅ |
| 본인 원티드 제출 | ✅ | ✅ |
| 타인 근무표 조회 | ❌ → "권한이 없습니다" | ✅ |
| update_person_attr | ❌ | ✅ |
| manage_grade / manage_team_min | ❌ | ✅ |
| generate_schedule | ❌ | ✅ |
| navigate(hn_only=true) | ❌ → 화면 차단 | ✅ |
| analyze_report | ❌ | ✅ |
| validate_schedule | ❌ | ✅ |

NAVIGATE_TARGETS 의 `hn_only` 플래그로 client-action 단에서 즉시 차단됨.

---

## 8. 다음 작업 후보 (이 문서 기반)

- **G1** (전입/전출) — query_schedule 에 scope=members_status 추가 (5줄), corpus 5건
- **G2** (생성 job 상태) — generation_tools.get_latest_job 노출하는 query_generation_job 스킬 (10줄), corpus 5건
- **G5** (개인 통계 인라인) — analyze_report(scope=nurse_workload) 확장 + ChatResponse.data로 노출 (Phase 7 인라인 렌더 활용)
