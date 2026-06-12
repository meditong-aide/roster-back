# 50케이스 × 축소모델 × MCS (실제 검출·검증)

matrix_50_cases(원인 직접주입, 솔버우회)와 달리 **실제 CP-SAT 가 충돌을 만들고**
find_mcs 가 **검증된 최소 수선**을 도출. 재현 못 한 케이스는 정직히 NOT-REPRODUCED.


## 요약: 45/50 PASS(verified) · 45/50 재현

| Case | 결과 | 완화(수선점) | 패턴 | verified | 시간 |
|---|---|---|---|---|---|
| CX-WIN-001 | ✅PASS | N 최소 2 | ['coverage'] | True | 0.06s |
| CX-WIN-002 | ✅PASS | N 최소 2, N 최소 2 | ['coverage'] | True | 0.01s |
| CX-WIN-003 | ✅PASS | D 최소 2, D 최소 2, N 최소 2 | ['coverage'] | True | 0.05s |
| CX-WIN-004 | ⚪NOT-REPRODUCED | — | — | — | 충돌 미발생(feasible) |
| CX-WIN-005 | ⚪NOT-REPRODUCED | — | — | — | 충돌 미발생(feasible) |
| CX-WIN-006 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-WIN-007 | ✅PASS | D 최소 2, N 최소 2, N 최소 2 | ['coverage', 'weekend_off_only'] | True | 0.05s |
| CX-WIN-008 | ✅PASS | N 최소 2 | ['coverage'] | True | 0.02s |
| CX-WIN-009 | ✅PASS | N 최소 2 | ['coverage'] | True | 0.02s |
| CX-WIN-010 | ✅PASS | D 최소 2, D 최소 2, N 최소 2 | ['coverage'] | True | 0.04s |
| CX-SFT-011 | ✅PASS | N 최소 2 | ['coverage'] | True | 0.02s |
| CX-SFT-012 | ✅PASS | D 최소 2, D 최소 2, N 최소 2 | ['coverage'] | True | 0.11s |
| CX-SFT-013 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-SFT-014 | ✅PASS | N 최소 2 | ['coverage'] | True | 0.02s |
| CX-SFT-015 | ✅PASS | D 최소 2, N 최소 2, N 최소 2 | ['coverage', 'weekend_off_only'] | True | 0.05s |
| CX-NUR-016 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-NUR-017 | ✅PASS | n0 D불가, n0 D불가, n0 D불가 | ['allowed_shift_mask', 'coverage'] | True | 0.06s |
| CX-NUR-018 | ✅PASS | D 최소 2, N 최소 2, N 최소 2 | ['coverage', 'weekend_off_only'] | True | 0.05s |
| CX-NUR-019 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-NUR-020 | ✅PASS | D 최소 2, D 최소 2, N 최소 2 | ['coverage'] | True | 0.11s |
| CX-NUR-021 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage', 'team_min'] | True | 0.06s |
| CX-NUR-022 | ✅PASS | N 등급1 최소 1/일 | ['grade_min'] | True | 0.01s |
| CX-NUR-023 | ✅PASS | n0 D불가, n0 D불가, n0 D불가 | ['allowed_shift_mask', 'coverage'] | True | 0.07s |
| CX-NUR-024 | ⚪NOT-REPRODUCED | — | — | — | 충돌 미발생(feasible) |
| CX-NUR-025 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-COV-026 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage', 'team_min'] | True | 0.06s |
| CX-COV-027 | ✅PASS | n0 D불가, n0 D불가, n0 D불가 | ['allowed_shift_mask', 'coverage'] | True | 0.07s |
| CX-COV-028 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-COV-029 | ✅PASS | N 등급1 최소 1/일 | ['grade_min'] | True | 0.01s |
| CX-COV-030 | ✅PASS | D 최소 2, N 최소 2, N 최소 2 | ['coverage', 'weekend_off_only'] | True | 0.05s |
| CX-COV-031 | ⚪NOT-REPRODUCED | — | — | — | 충돌 미발생(feasible) |
| CX-COV-032 | ✅PASS | N 최소 2 | ['coverage'] | True | 0.02s |
| CX-COV-033 | ✅PASS | N 등급1 최소 1/일 | ['grade_min'] | True | 0.01s |
| CX-COV-034 | ✅PASS | n0 D불가, n0 D불가, n0 D불가 | ['allowed_shift_mask', 'coverage'] | True | 0.07s |
| CX-COV-035 | ✅PASS | D 최소 2, D 최소 2, N 최소 2 | ['coverage'] | True | 0.12s |
| CX-OVR-036 | ✅PASS | N 최소 2 | ['coverage'] | True | 0.02s |
| CX-OVR-037 | ✅PASS | n0 D불가, n0 D불가, n0 D불가 | ['allowed_shift_mask', 'coverage'] | True | 0.07s |
| CX-OVR-038 | ⚪NOT-REPRODUCED | — | — | — | 충돌 미발생(feasible) |
| CX-OVR-039 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-OVR-040 | ✅PASS | N 등급1 최소 1/일 | ['grade_min'] | True | 0.01s |
| CX-META-041 | ✅PASS | N 등급1 최소 1/일 | ['grade_min'] | True | 0.01s |
| CX-META-042 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-META-043 | ✅PASS | n0 D불가, n0 D불가, n0 D불가 | ['allowed_shift_mask', 'coverage'] | True | 0.07s |
| CX-META-044 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-META-045 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-MIX-046 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.02s |
| CX-MIX-047 | ✅PASS | D 최소 2, N 최소 2, N 최소 2 | ['coverage', 'weekend_off_only'] | True | 0.05s |
| CX-MIX-048 | ✅PASS | n0 D불가, n0 D불가, n0 D불가 | ['allowed_shift_mask', 'coverage'] | True | 0.07s |
| CX-MIX-049 | ✅PASS | N 최소 2, N 최소 2, N 최소 2 | ['coverage'] | True | 0.03s |
| CX-MIX-050 | ✅PASS | n0 D불가, n0 D불가, n0 D불가 | ['allowed_shift_mask', 'coverage'] | True | 0.07s |