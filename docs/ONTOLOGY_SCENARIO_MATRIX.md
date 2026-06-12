# conflict_scenarios 6종 × 병동 전체 테스트 매트릭스

준거 = `ontology.yaml` conflict_scenarios. 각 (병동×시나리오): 주입 → infeasible → 그래프 generic 진단 → 최소변경 액션 → 적용 → resolved.

정직성: min-max 자원 충돌은 그래프가 진단·수선. 시퀀스/타이밍 충돌은 현재 미진단 → **GAP**(MUS wrap 필요).


## 9B 병동 (baseline 실근무 281건)

| 시나리오 | infeasible | 진단된 원인 | 추천 액션 | 적용 후 | 결과 |
|---|---|---|---|---|---|
| GRADE_MIN_VS_GRADE_MAX | ✅ (0건) | GradeMin×GradeMax@N_grade1(하한1>상한0) | GradeMax 하드→소프트(전역) | 실근무 281건 | ✅ PASS |
| COVERAGE_MIN_VS_GRADE_MAX | ✅ (0건) | CoverageMin×GradeMax@N_coverage(하한2>상한0) | GradeMax 하드→소프트(전역) | 실근무 281건 | ✅ PASS |
| TEAM_MIN_VS_GRADE_MAX_INTERSECTION | ✅ (0건) | TeamMin×GradeMax@team2_N(하한6>상한0) | GradeMax 하드→소프트(전역) | 실근무 281건 | ✅ PASS |
| BTBAN_VS_FIXED_SEQUENCE | — | — | — | — | ⚠ GAP: 전이금지↔고정 시퀀스 — 시간축 충돌, 현재 구조검출기 미지원(MUS wrap 필요) |
| NIGHT_RECOVERY_VS_COVERAGE_MIN | — | — | — | — | ⚠ GAP: 야간회복 OFF 몰림 — 타이밍 충돌, 미지원(MUS wrap 필요) |
| NOT_ONE_NIGHT_VS_FIXED_OFF_NEIGHBOR | — | — | — | — | ⚠ GAP: 1N 회피↔인접 고정 — 시퀀스 충돌, 미지원(MUS wrap 필요) |
| OFFCAP_VS_WEEKEND_OFF_NARROW_WINDOW | — | — | — | — | ⚠ GAP: OFF cap↔주말OFF 좁은창 — 부분 표현 가능하나 활성창/마스크 모델 필요(미구현) |


## ICU 병동 (baseline 실근무 540건)

| 시나리오 | infeasible | 진단된 원인 | 추천 액션 | 적용 후 | 결과 |
|---|---|---|---|---|---|
| GRADE_MIN_VS_GRADE_MAX | ✅ (0건) | GradeMin×GradeMax@N_grade1(하한1>상한0) | GradeMax 하드→소프트(전역) | 실근무 540건 | ✅ PASS |
| COVERAGE_MIN_VS_GRADE_MAX | ✅ (0건) | CoverageMin×GradeMax@N_coverage(하한6>상한0) | GradeMax 하드→소프트(전역) | 실근무 540건 | ✅ PASS |
| TEAM_MIN_VS_GRADE_MAX_INTERSECTION | — | — | — | — | N/A: 해당 병동에 미적용(예: 팀 없음) |
| BTBAN_VS_FIXED_SEQUENCE | — | — | — | — | ⚠ GAP: 전이금지↔고정 시퀀스 — 시간축 충돌, 현재 구조검출기 미지원(MUS wrap 필요) |
| NIGHT_RECOVERY_VS_COVERAGE_MIN | — | — | — | — | ⚠ GAP: 야간회복 OFF 몰림 — 타이밍 충돌, 미지원(MUS wrap 필요) |
| NOT_ONE_NIGHT_VS_FIXED_OFF_NEIGHBOR | — | — | — | — | ⚠ GAP: 1N 회피↔인접 고정 — 시퀀스 충돌, 미지원(MUS wrap 필요) |
| OFFCAP_VS_WEEKEND_OFF_NARROW_WINDOW | — | — | — | — | ⚠ GAP: OFF cap↔주말OFF 좁은창 — 부분 표현 가능하나 활성창/마스크 모델 필요(미구현) |


## 요약

- **9B**: PASS 3 / GAP 4 / N-A 0 / 전체 7
- **ICU**: PASS 2 / GAP 4 / N-A 1 / 전체 7

> PASS = 그래프가 두 하드제약 충돌을 원인으로 짚고, 최소변경 액션으로 실제 해결.
> GAP = 시퀀스/타이밍 충돌이라 현재 구조검출기로 미진단(처음 실증한 MUS wrap 갭과 동일 지점).