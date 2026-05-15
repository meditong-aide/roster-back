# NO_ASSIGNMENT Direct-Reason Case Matrix (Rule-Based, No Score)

> 목적: `NO_ASSIGNMENT`를 단일 결과코드로 끝내지 않고,
> `capacity_shortage / eligibility_lock / fixed_lock / carryover_lock` 축으로
> **직접 reason화**할 때 누락 없이 검증하기 위한 케이스 매트릭스.

---

## 1) Canonical Sub-Reasons (점수/가중치 없음)

- `capacity_shortage`: 수요 대비 공급 총량/풀 부족
- `eligibility_lock`: allowed shift/role/team-grade 자격 잠금으로 배정 불가
- `fixed_lock`: fixed assignment/forbidden 충돌로 탐색공간 봉쇄
- `carryover_lock`: 전월 경계 회복/전이 제약으로 월초 배정 봉쇄

---

## 2) Evidence Mapping Rules

### A. capacity_shortage
- reason_code: `CAPACITY_TOTAL_SHORTAGE`, `GLOBAL_DAY_CAPACITY_SHORTAGE`, `TEAM_MIN_EXCEEDS_GLOBAL_NEED`, `GRADE_MIN_SUM_EXCEEDS_NEED`
- 또는 `pool_snapshot.shortages` 존재

### B. eligibility_lock
- reason_code: `ALLOWED_SHIFTS_ISOLATES_NURSE`, `TEAM_SHIFT_ALLOWED_SHORTAGE`
- pattern: `allowed_shift_mask`, `n_only_vs_caps`

### C. fixed_lock
- reason_code: `FIXED_ASSIGN_EXCEEDS_NEED`, `FIXED_ASSIGN_VIOLATES_ALLOWED`, `FIXED_ASSIGN_BREAKS_TEAM_MIN`
- pattern: `fixed_assignment`, `initial_forbidden`

### D. carryover_lock
- reason_code prefix: `PREV_`
- pattern: `carryover_boundary`, `carryover_recovery_*`

---

## 3) Case Catalog (24)

| # | Case ID | Input Signal Set | Expected Breakdown |
|---|---|---|---|
| 1 | NA-BASE-001 | `NO_ASSIGNMENT` only | `[]` (근거 부족) |
| 2 | NA-CAP-002 | `NO_ASSIGNMENT` + pool shortage 1 | `capacity_shortage` |
| 3 | NA-CAP-003 | `NO_ASSIGNMENT` + 다중 shortage | `capacity_shortage` |
| 4 | NA-CAP-004 | `NO_ASSIGNMENT` + `CAPACITY_TOTAL_SHORTAGE` | `capacity_shortage` |
| 5 | NA-CAP-005 | `NO_ASSIGNMENT` + `GLOBAL_DAY_CAPACITY_SHORTAGE` | `capacity_shortage` |
| 6 | NA-CAP-006 | `NO_ASSIGNMENT` + `TEAM_MIN_EXCEEDS_GLOBAL_NEED` | `capacity_shortage` |
| 7 | NA-ELI-007 | `NO_ASSIGNMENT` + `ALLOWED_SHIFTS_ISOLATES_NURSE` | `eligibility_lock` |
| 8 | NA-ELI-008 | `NO_ASSIGNMENT` + `TEAM_SHIFT_ALLOWED_SHORTAGE` | `eligibility_lock` |
| 9 | NA-ELI-009 | `NO_ASSIGNMENT` + pattern `allowed_shift_mask` | `eligibility_lock` |
| 10 | NA-ELI-010 | `NO_ASSIGNMENT` + pattern `n_only_vs_caps` | `eligibility_lock` |
| 11 | NA-FIX-011 | `NO_ASSIGNMENT` + `FIXED_ASSIGN_EXCEEDS_NEED` | `fixed_lock` |
| 12 | NA-FIX-012 | `NO_ASSIGNMENT` + `FIXED_ASSIGN_VIOLATES_ALLOWED` | `fixed_lock` |
| 13 | NA-FIX-013 | `NO_ASSIGNMENT` + `FIXED_ASSIGN_BREAKS_TEAM_MIN` | `fixed_lock` |
| 14 | NA-FIX-014 | `NO_ASSIGNMENT` + pattern `fixed_assignment` | `fixed_lock` |
| 15 | NA-FIX-015 | `NO_ASSIGNMENT` + pattern `initial_forbidden` | `fixed_lock` |
| 16 | NA-CAR-016 | `NO_ASSIGNMENT` + reason `PREV_MONTH_TRANSITION` | `carryover_lock` |
| 17 | NA-CAR-017 | `NO_ASSIGNMENT` + pattern `carryover_boundary` | `carryover_lock` |
| 18 | NA-CAR-018 | `NO_ASSIGNMENT` + pattern `carryover_recovery_2n2off_boundary` | `carryover_lock` |
| 19 | NA-MIX-019 | capacity + eligibility | `capacity_shortage, eligibility_lock` |
| 20 | NA-MIX-020 | capacity + fixed | `capacity_shortage, fixed_lock` |
| 21 | NA-MIX-021 | capacity + carryover | `capacity_shortage, carryover_lock` |
| 22 | NA-MIX-022 | eligibility + fixed | `eligibility_lock, fixed_lock` |
| 23 | NA-MIX-023 | eligibility + carryover | `eligibility_lock, carryover_lock` |
| 24 | NA-MIX-024 | 4축 전부 | `capacity_shortage, eligibility_lock, fixed_lock, carryover_lock` |

---

## 4) Direct-Reason Migration Plan

현재: `NO_ASSIGNMENT` + 부가신호로 분해 추론

목표: solver/validator가 아래 세부 reason을 직접 emit
- `NO_ASSIGNMENT_CAPACITY`
- `NO_ASSIGNMENT_ELIGIBILITY`
- `NO_ASSIGNMENT_FIXED`
- `NO_ASSIGNMENT_CARRYOVER`

이행 규칙:
1. direct reason이 있으면 해당 축을 우선 채택
2. direct reason이 없으면 기존 신호 기반 fallback 유지
3. 둘 다 있으면 direct reason을 source-of-truth로 기록

---

## 5) Test Contract

- 최소 자동화: 위 24 케이스 중 12개 핵심 + 4개 복합 = 16개
- 필수 assertion:
  - `fix_plan.no_assignment_breakdown` exact set
  - `fix_plan.actions[].action_id` 우선순위 정합
  - `fix_plan.actions[].targets` (shortage case) 포함
