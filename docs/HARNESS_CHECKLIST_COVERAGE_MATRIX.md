# Harness Checklist Coverage Matrix (42 Rules)

> 기준: `tools/harness/rules/checklist_core.yaml` + 현재 `tools/harness/runner.py` 구현 상태
> 최신 검증 런 참조: `tools/harness/reports/run-2026-05-20260511-124132`

---

## 1) Coverage Summary

- Total rules: **42**
- **완전커버**: 31
- **부분커버**: 11
- **미커버**: 0

분류 기준:
- **완전커버**: metric 계산 로직이 구현되어 실제 PASS/FAIL/SKIPPED 없이 평가 가능
- **부분커버**: 값은 채워지지만 placeholder/근사치(TODO source) 기반
- **미커버**: 현재 `SKIPPED(metric_not_implemented)`

---

## 2) 미커버(0)

- 현재 기준 `SKIPPED(metric_not_implemented)` 항목 없음.

---

## 3) 부분커버(11) — 정밀화 필요

| Rule ID | Metric | Current | 보완 포인트 |
|---|---|---:|---|
| `B_OFF_SWAP_RECOVERY` | `offswap.recovery_off_converted_count` | PASS | offswap trace 기반 실측 필요 |
| `B_OFF_SWAP_N_ONLY` | `offswap.night_only_off_converted_count` | PASS | offswap trace 기반 실측 필요 |
| `B_OFF_SWAP_FIXED` | `offswap.fixed_off_converted_count` | PASS | offswap trace 기반 실측 필요 |
| `B_OFF_SWAP_JU` | `offswap.ju_converted_count` | PASS | offswap trace 기반 실측 필요 |
| `F_DROPPED_FILTER` | `carryover.dropped_ref_count` | PASS | `prev-tail.schedule_status` 기반(다중 dropped 이력 반영 한계) |
| `B_WEEKLY_OFF` | `off.weekly_off_missing_count` | PASS/FAIL | `/weekly-off/nurses` preview 기반(생성 결과 직접 추적 아님) |
| `C_DEN_BALANCE` | `fairness.den_spread_ratio` | PASS/FAIL | 현재 spread 정의 단순화(정책 합의 필요) |
| `C_TOTAL_BALANCE` | `fairness.total_work_spread` | PASS/FAIL | 현재 총근무일 편차 단순화(가중/제외 규칙 미반영) |
| `C_N_SKEW` | `fairness.n_shift_skew_ratio` | PASS/FAIL | N 집중도 지표 단순화(팀/직급 가중치 미반영) |
| `E_WANTED_APPLY` | `wanted.apply_ratio` | PASS/FAIL | fixed entries 기준 근사(일반 wanted 분리 필요) |
| `G_PRECEPTEE_PAIR_SPREAD` | `preceptee.pair_shift_concentration_ratio` | PASS/FAIL | 동반근무 집중도 근사(정책식 확정 필요) |
| `H_OFF_SWAP_LOG` | `logs.offswap_trace_missing_count` | PASS/FAIL | emit events 기반 + 설정 결합(로그 파이프라인 직결은 아님) |

보완 사항(최근 반영):
- `wanted.apply_ratio`는 상태 분기 사용
  - `wanted.status in {closed, applied, done}`: fixed-wanted 실제 반영률 사용
  - 그 외(`requested` 등): `/wanted/{year}/{month}/submissions` 제출률 사용
- **가드 강화**: fixed-wanted entry가 1개 이상이면 `E_WANTED_APPLY` pass condition을 동적으로 `>= 1.0`로 상향
- `logs.offswap_trace_missing_count`는 `constraint_impact` 전체 blob에서 offswap 신호를 재귀 탐색해 판정

## 3.1 최근 정밀화 완료(완전커버로 승격)

| Rule ID | Metric | 개선 내용 |
|---|---|---|
| `H_RUNTIME` | `system.solve_time_ms_p95` | `constraint_impact.timing_ms` 반복 실행 p95 실측 |
| `H_FALLBACK_OK` | `system.fallback_error_count` | run별 `solver_status` 집계로 fallback/error 실측 |
| `F_DROPPED_FILTER` | `carryover.dropped_ref_count` | `prev-tail.data.schedule_status` 기반 검출 |

---

## 4) 완전커버(28)

### A (9)
`A_1N_SINGLE`, `A_2N_2OFF`, `A_3N_2OFF`, `A_4N_MAX`, `A_MAX_CONSEQ_WORK`, `A_NOD`, `A_NOE`, `A_EOD`, `A_MONTHLY_N_CAP`

### B (3)
`B_OFF_NEAR_CONFIG`, `B_OFF_CAP_EXACT`, `B_OFF_SWAP_TARGET_SINGLE`

### D (6)
`D_D_MIN`, `D_E_MIN`, `D_N_MIN`, `D_M_MIN`, `D_MAX_OVER`, `D_MAX_ENABLED_INTEGRITY`

### E (2)
`E_FIXED_LOCK`, `E_BAN_N_BEFORE_FIXED_OFF`

### F (3)
`F_PREV_TRANSITION`, `F_PREV_CONSEQ_WORK`, `F_PREV_N_RECOVERY`

### G (4)
`G_PRECEPTEE_SYNC`, `G_PRECEPTEE_MAPPING`, `G_ROLE_NULL`, `G_GRADE_CONSTRAINT`

### H (1)
`H_NO_INFEASIBLE`

---

## 5) CI Integration Checklist (Dev 적용용)

아래를 만족해야 “개발 적용 가능” 상태로 본다.

- [ ] GitHub Actions secret 등록
  - `HARNESS_BASE_URL` (예: dev API URL)
  - `HARNESS_ACCESS_TOKEN` (dev 테스트 계정 토큰)
- [ ] Branch protection required check 정책 적용 여부 확인
  - 운영 가이드: `docs/HARNESS_BRANCH_PROTECTION_OPERATIONS.md`
- [ ] `harness-dev-gate` 워크플로우 성공
  - `python tools/harness/runner.py ... --strict`
  - summary status 가 `PASS`
- [ ] 아티팩트 확인
  - `run_result.json`, `summary.json`, `graph_export.json`, `triage.md`
- [ ] 블로킹 정책
  - `blocking_fail_count == 0`
  - `blocking_skipped_count == 0` (`--strict`)
- [ ] 매핑 정합성
  - `summary.graph_consistency.missing_in_graph` 빈 배열
  - `summary.graph_consistency.extra_in_graph` 빈 배열

---

## 6) Next Priorities (Execution Order)

1. **부분커버 14개 정밀화**: proxy/placeholder 제거
2. **offswap 로그 계측 연동**: `H_OFF_SWAP_LOG` 실측화
3. **fairness/wanted 정책식 확정**: C/E/G 계산식 표준화
4. **CI gate 강화**: PR 보호 규칙으로 `harness-dev-gate` required check 설정

---

## 7) Applied Fairness Policy (Current)

- 평가 대상: `active` 간호사 중 아래 제외
  - 월중 입사(`joining_date`가 대상 year/month)
  - preceptee( `preceptor_id` 보유)
  - assignment 기반 제외(휴직/퇴사/파견 active)
- 우선순위: `DEN > 총근무 > N쏠림`
- 강도: 중간
- 전환 정책: **warning → blocking**
  - 기본은 warning
  - CI/운영 승격 시 환경변수로 C그룹 blocking 전환
    - `HARNESS_FAIRNESS_MODE=blocking`
