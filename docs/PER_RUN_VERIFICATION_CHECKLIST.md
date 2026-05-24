# 근무표 생성 검증 통합 체크리스트 (Per-Run)

> 생성: 2026-05-24
> 업데이트: 2026-05-24 (pull 후 신규/업데이트 docs 12건 반영 — 총 90+ 항목)
> 브랜치: feat/even-de-agent-qa-merge
> 목적: 근무표 generate 실행 시마다 자동/수동으로 돌릴 회귀 검증 항목 통합본
> 출처: 본 브랜치 전용 docs 40개에서 추출

## 사용법

1. 검증 대상 schedule_id 확보 (DB `schedules` 테이블에서 group_id + year + month로 최신 row)
2. 카테고리 A → B → C 순으로 검증 (HARD 우선 → SOFT → 정책 변경)
3. 카테고리 D~F는 PR/CI 게이트 단계에서 추가 적용
4. 각 항목 결과는 `tools/harness/reports/run-*/summary.json` 에 기록

## 출처 docs 매핑

| 약어 | 파일 |
|---|---|
| CT | docs/CONSTRAINT_TAXONOMY_AND_WRAP_TEST_PLAN.md |
| SC | docs/2026_06_SCHEDULE_CONFIG_CHANGES.md |
| HM | docs/HARNESS_CHECKLIST_COVERAGE_MATRIX.md |
| OC | docs/ONTOLOGY_HARNESS_CHECKLIST.md |
| TM | docs/CONSTRAINT_TESTCASE_MATRIX_SPEC.md |
| HD | docs/CONSTRAINT_HARD_CONFLICT_DIAGNOSIS_PLAN.md |
| NA | docs/NO_ASSIGNMENT_DIRECT_REASON_CASE_MATRIX.md |
| QA | docs/AGENT_QA_SCENARIOS_2026-05-18.md |
| RH | docs/ROSTER_GENERATION_HARNESS_SPEC.md |
| BP | docs/HARNESS_BRANCH_PROTECTION_OPERATIONS.md |

---

## A. HARD 제약 위반 (위반 0건 강제, 22항)

| ID | 항목 | 출처 | 검증 | 합격 |
|---|---|---|---|---|
| H-01 | 1N 단독 금지 | CT | 같은 nurse에 N 인접 0 없는 단독 N 탐색 | 0건 |
| H-02 | 2N→2OFF 회복 | CT | 2연속 N 후 2연속 OFF 미충족 | 0건 |
| H-03 | 3N→2OFF 회복 | SC | 3연속 N 후 2연속 OFF 미충족 | 0건 |
| H-04 | 4N 이상 연속 금지 | CT | N 연속 ≥ 4 발생 | 0건 (cfg.max_consecutive_nights=3) |
| H-05 | 연속근무 한계 | CT | D/E/N 연속 ≥ 6 | 0건 (cfg.max_consecutive_work_days=5) |
| H-06 | ND 전이 금지 | CT | N→D 인접 | 0건 |
| H-07 | NE 전이 금지 | CT | N→E 인접 | 0건 |
| H-08 | EOD(E→D) 금지 | CT, SC | E→D 인접 (banned_day_after_eve=True 시) | 0건 |
| H-09 | 월 야간 상한 | CT, SC | nurse별 월 N 합 | ≤ nurse_monthly_limit.n_max (ICU=7) |
| H-10 | 팀별 일일 최소 | CT | team×day×shift 배치 | ≥ team_min (soft=True 시 slack 허용) |
| H-11 | 등급별 일일 최소 | CT | grade×day×shift 배치 | ≥ grade_min (allow_soft_fallback=0 검증) |
| H-12 | 등급별 일일 최대 | CT | grade×day×shift 배치 | ≤ grade_max |
| H-13 | 일일 커버리지 | CT | day×shift 배치 | = need (under_count=0) |
| H-14 | E→D 금지 (활성 시) | SC | cfg.banned_day_after_eve=True | 위반 0 또는 비활성 |
| H-15 | 4O hard (활성 시) | SC | 4연속 OFF 발생 | 0건 (enforce_4o_hard=True 시) — 기본값 False |
| H-16 | max_same_shift 4연속 (활성 시) | SC | 동일 shift 4연속 | 0건 (max_same_shift=True 시) |
| H-17 | 월 OFF 최소 | CT | nurse별 월 OFF 합 | ≥ off_min |
| H-18 | 월 OFF 정확값 (o_exact) | CT | o_exact 설정 nurse | = o_exact |
| H-19 | N-only nurse 정책 | CT | N-only nurse의 D/E 할당 | 0건 |
| H-20 | Grade hard mode 강제 (`allow_soft_fallback=0`) | docs/GRADE_SOFT_FALLBACK_MIGRATION.md | `roster_grade_config.allow_soft_fallback=0` | 9B/ICU 모두 0 |
| H-21 | team_monthly_capacity 검증 (TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL) | docs/TEAM_GRADE_API_INTEGRATION_GUIDE.md | team_min_shift 저장 시 reason_code 0건 | 팀별 인력-OFF exact 정합 |
| H-22 | Precheck reason_code 카탈로그 18종 (A3+B4+C4+D1+E1+F4) | docs/TEAM_GRADE_INFEASIBILITY_PRECHECK.md, docs/FRONTEND_PRECHECK_INTEGRATION.md | precheck 실행 시 reason_code in 카탈로그 | 미정의 코드 0건 |

---

## B. SOFT 제약 / Fairness 분석 (13항)

| ID | 항목 | 출처 | 검증 | 합격 (권장) |
|---|---|---|---|---|
| S-01 | 야간 분포 KLD | HM | KLD(per-nurse N 분포) | < 0.15 |
| S-02 | 야간 range | SC | N max − N min | < 3 권장 (현 ICU=10, 정책상 허용) |
| S-03 | D/E 분포 편차 | HM | 표준편차 | < 평균 × 0.3 |
| S-04 | 총 근무일 균등 | HM | D+E+N max − min | ≤ 2 |
| S-05 | 연속 OFF 비중 | HM | 2일 이상 연속 OFF 비율 | ≥ 60% |
| S-06 | 주간 OFF 2회 충족 | CT | nurse별 주 OFF ≥ 2 | ≥ 80% nurse |
| S-07 | NOD/NOE soft penalty | CT | N-O-D / N-O-E 패턴 | < 5 occurrences |
| S-08 | 프리셉티 동기화 | HM | preceptor-preceptee 동일 shift | ≥ 70% |
| S-09 | 등급별 분포 | CT | grade 1/2/3 분포 비율 | 정책 목표값 ±5% |
| S-10 | N 블록 간격 (target) | SC, SOFT_CONSTRAINTS_WEIGHT_TIMELINE.md | N 블록 종료→다음 시작 gap, weight=50 | 평균 10일, ICU ±1일 비율 ≥ 39%, 9B ≥ 30% |
| S-11 | N 블록 간격 대칭 페널티 (target=10 ±1) | SOFT_CONSTRAINTS_WEIGHT_TIMELINE.md, SC §9 | 9~11일 비율 측정 | ICU ≥ 39%, 9B ≥ 30% |
| S-12 | 동일 shift 4연속 억제 (max_same_shift_soft) | SC §6, commit 3a62e1a | `max_same_shift_penalty_weight=10000` 적용 후 4연속 nurse 수 | ≤ 2명 / 28명 (peak 5+ 0건) |
| S-13 | team_min soft slack | SC §1, commit f2c3ff0 | `team_min_soft_fallback=True` + `team_min_penalty_weight=500` | 팀별 일일 min 미달 ≤ 3건 |

---

## C. 6월 정책 변경 검증 (12항)

| ID | 변경 항목 | 예상 결과 | 검증 | 비고 |
|---|---|---|---|---|
| C-01 | team_min_soft_fallback 전역 활성 | hard→soft 전환, slack 허용 | `cfg.team_min_soft_fallback=True` | 9B/ICU 적용 |
| C-02 | Grade hard 유지 (9B/ICU) | grade_min 위반 0건 강제 | `roster_grade_config.allow_soft_fallback=0` | 그룹별 |
| C-03 | ICU n_max=7 적용 | N≤7 (soft cap 시 초과 가능) | nurse별 N 분포 | 정책 변경 직후 초과 케이스 수용 가능 |
| C-04 | max_same_shift 4연속 억제 | 4연속 시프트 감소 | weight=10000 | 4연속 nurse ≤ 2명 권장 |
| C-05 | N-to-N gap soft (target=10) | 평균 gap 9~10일 | 블록 단위 측정 | ICU 39% 이상 target±1 |
| C-06 | AUTO-SOFT applied_relaxations | team_min soft 적용 추적 | response payload | `['team_min_hard_to_soft']` 또는 `['treatment:soft:team_min']` (ontology treatment 라벨, commit 9d6eb5c) |
| C-07 | 4O hard 해제 (기본 False) | 4연속 OFF 자연 발생 허용 | `enforce_4o_hard=False` | 자연 억제 11~13% |
| C-08 | team_min_soft_fallback 전역 True | hard→soft 자동 전환 (grade 유지) | `cfg.team_min_soft_fallback=True` | AUTO-SOFT 경로에서 team_min 우선 |
| C-09 | Grade hard 재강화 (9B/ICU) | `allow_soft_fallback=0` 양 그룹 | grade_min 위반 0건 또는 AUTO-SOFT 최대 1회 | 수동 soft 만 (UI 버튼) |
| C-10 | `enforce_4o_hard=False` 디폴트 해제 | 4O 자연 억제 11~13% | escape hatch: `ROSTER_DISABLE_4O_HARD` env | 4O 발생 ≤ 3명 (운영 수용) |
| C-11 | N-to-N gap soft (target=10, weight=50) | 평균 gap 9~10일, ±1 ≥ 39% | `n_to_n_interval_target=10` + `n_to_n_interval_max_window=15` | 월경계 미반영 (후속) |
| C-12 | `max_same_shift_penalty_weight=10000` | 4연속 회피 (hard 아님) | ICU/9B 4연속 인원 ≤ 2 | 트레이드오프 허용 |

---

## D. 온톨로지 / 메타 Invariant I1~I9 (9항)

| ID | Invariant | 검증 명령 | 합격 |
|---|---|---|---|
| I-01 | 모든 cause에 problem_template_ko 존재 | `pytest tests/test_ontology_consistency.py::test_I1_cause_templates` | 0 fail |
| I-02 | 모든 cause에 treatment ≥ 1 | `pytest ::test_I2_cause_treatments` | 0 fail |
| I-03 | treatment.applies_to_causes 유효 (dangling 없음) | `pytest ::test_I3_cause_refs` | 0 fail |
| I-04 | treatment에 rationale_ko + trade_off_ko | `pytest ::test_I4_treatment_ko` | 0 fail |
| I-05 | config_key 친화 라벨 등재 | `pytest ::test_I5_config_key_labels` | 0 fail |
| I-06 | direction (enable/disable) 라벨 | `pytest ::test_I6_direction_labels` | 0 fail |
| I-07 | constraint_family ↔ MUS token 매핑 | `pytest ::test_I7_family_mus_mapping` | 0 fail |
| I-08 | matrix 50-case factory 정합 | `pytest tests/test_matrix_full_50_cases.py` | 0 fail |
| I-09 | cause.category 알려진 도메인 | `pytest ::test_I9_known_categories` | 0 fail |

---

## E. UNSAT / 진단 검증 (12항)

| ID | 항목 | 검증 | 합격 |
|---|---|---|---|
| E-01 | NO_ASSIGNMENT 원인 분해 | `fix_plan.no_assignment_breakdown` | capacity / eligibility / fixed / carryover 명시 |
| E-02 | 직접 reason 우선 | solver+validator 결과 | direct reason 있으면 `reason_source=direct` |
| E-03 | conflict core 정합 | `GET /ontology/conflict_summary` | `missing_in_graph = []` |
| E-04 | precheck block 신호 | 500 + reason_code | ConfigIntegrity / GradeMinMaxMismatch 명확 |
| E-05 | MUS(Minimal Unsat Set) | `infeasibility.conflict_cores[*].pattern` | 예상 family 일치 |
| E-06 | applied_relaxations 추적 | response payload | 완화 항목 명시 |
| E-07 | relax_level 진행도 | `infeasibility.relax_level` | 0 (최적) ~ N |
| E-08 | solver_status | response | OPTIMAL / FALLBACK / INFEASIBLE 명확 |
| E-09 | INFEASIBLE_DIAGNOSTICS 3단계 | docs/INFEASIBLE_DIAGNOSTICS_FRONT_BACK_ARCHITECTURE.md | `response.diagnostics.precheck/primary_cp_sat/fallback` 구조 | 3단계 모두 존재 |
| E-10 | Precheck deterministic reason_code 18종 | docs/FRONTEND_PRECHECK_INTEGRATION.md, docs/TEAM_GRADE_INFEASIBILITY_PRECHECK.md | A(3)+B(4)+C(4)+D(1)+E(1)+F(4) 카탈로그 | 미정의 코드 0건 |
| E-11 | `applied_relaxations` ontology treatment 라벨 | docs/INFEASIBILITY_FRONTEND_GUIDE.md §3 | `treatment:soft:*` 라벨 동시 출력 | UI에서 treatment 표기 가능 |
| E-12 | team_auto_assign greedy + 2-opt swap | docs/2026_06_SCHEDULE_CONFIG_CHANGES.md §10, commit 1020142 | algorithm 결과 balance | G1 ≥ 1명/팀, 쏠림 < 10% |

---

## F. 운영 게이트 (PR/CI 차단 조건, 7항)

| ID | 항목 | 검증 | 차단 기준 |
|---|---|---|---|
| F-01 | blocking_fail_count | harness summary.json | = 0 |
| F-02 | blocking_skipped_count (--strict) | harness --strict | = 0 |
| F-03 | graph_consistency 정합 | summary.graph_consistency | missing_in_graph = [] + extra_in_graph = [] |
| F-04 | 매핑 정합 | rules_total vs mapped_rules_count | unmapped 최소화 |
| F-05 | CI workflow 성공 | `.github/workflows/harness-dev-gate.yml` | exit 0 |
| F-06 | 아티팩트 생성 | `tools/harness/reports/run-*/` | run_result.json + summary.json + triage.md + graph_export.json |
| F-07 | Branch protection rule | GitHub Settings → Branches | `harness-dev-gate` required (dev/main) |

---

## G. 하네스 42규칙 커버리지 (그룹별)

| Group | 완전 | 부분 | 미커버 | 항목 (요약) |
|---|---|---|---|---|
| A (Hard) | 9 | 0 | 0 | 1N / 2N→2OFF / 3N→2OFF / 4N_MAX / MAX_CONSEQ_WORK / NOD / NOE / EOD / MONTHLY_N_CAP |
| B (OFF) | 3 | 8 | 0 | OFF_SWAP_* 4종 + WEEKLY_OFF 1종 (실측 필요) |
| C (Fairness) | 0 | 3 | 0 | DEN_BALANCE / TOTAL_BALANCE / N_SKEW (정책식 확정 필요) |
| D (Grade) | 6 | 0 | 0 | D/E/N/M MIN / MAX_OVER / MAX_INTEGRITY |
| E (Fixed) | 2 | 1 | 0 | FIXED_LOCK / BAN_N_BEFORE_OFF + WANTED_APPLY |
| F (Carryover) | 3 | 1 | 0 | PREV_TRANSITION / PREV_CONSEQ / PREV_N_RECOVERY + DROPPED_FILTER |
| G (Preceptee) | 4 | 1 | 0 | SYNC / MAPPING / ROLE_NULL / GRADE + PAIR_SPREAD |
| H (System) | 1 | 0 | 0 | NO_INFEASIBLE + RUNTIME(p95) / FALLBACK_OK / OFF_SWAP_LOG (완전화 필요) |

---

## H. Agent QA 회귀 critical (5항)

| ID | 시나리오 | 사고 유형 | 합격 |
|---|---|---|---|
| QA-B1 | max_nig 변경 거절 | 운영 정책 위반 | "고정되어 있어 변경할 수 없습니다" + 대안 |
| QA-E1/E5 | group_id 필터 누락 | PHI 유출 | ctx.group_id 소속만 응답 |
| QA-D5 | 삭제 재확인 누락 | 데이터 손실 | 명시적 재확인 발화 |
| QA-K1~K4 | 권한 분리 실패 | 권한 우회 | permission_denied 또는 정책 거절 |
| QA-L4 | raw error 노출 | UX 저하 | "없습니다" 자연어 |

---

## I. 실행 스크립트 샘플

### I-1. 전체 하네스 실행 (CI 게이트와 동등)
```bash
python tools/harness/runner.py \
  --base-url http://127.0.0.1:8000 \
  --token "<JWT>" \
  --year 2026 --month 6 \
  --strategy COMBINED \
  --repeats 5 \
  --strict
```

### I-2. 온톨로지 일관성
```bash
pytest tests/test_ontology_consistency.py -v
pytest tests/test_friendly_labels.py -v
pytest tests/test_matrix_full_50_cases.py -v
```

### I-3. 라이브 audit
```bash
curl -X GET http://127.0.0.1:8000/ontology/audit
```

### I-4. HARD 위반 0건 SQL (1N 예시)
```sql
-- ⚠️ 월경계 false positive 발생함: 당월 첫날(1일)의 LAG가 NULL → 단독 N으로 오판
-- 정확한 cross-month 검사: 전월 latest schedule의 마지막 6일을 병합한 후 평가

DECLARE @first_day DATE = '2026-06-01';
DECLARE @prev_sid VARCHAR(50);
SELECT TOP 1 @prev_sid = schedule_id FROM schedules
  WHERE group_id = :gid AND [year]=2026 AND [month]=5
  ORDER BY created_at DESC;

WITH all_days AS (
  SELECT nurse_id, work_date, shift_id
  FROM schedule_entries WHERE schedule_id = :sid
  UNION ALL
  SELECT nurse_id, work_date, shift_id
  FROM schedule_entries
  WHERE schedule_id = @prev_sid AND work_date >= DATEADD(DAY,-6,@first_day)
), seq AS (
  SELECT nurse_id, work_date, shift_id,
    LAG(shift_id) OVER (PARTITION BY nurse_id ORDER BY work_date) prev_s,
    LEAD(shift_id) OVER (PARTITION BY nurse_id ORDER BY work_date) next_s
  FROM all_days
)
SELECT COUNT(*) FROM seq
WHERE shift_id='N' AND work_date >= @first_day
  AND (prev_s IS NULL OR prev_s<>'N') AND (next_s IS NULL OR next_s<>'N');
-- 합격: 0
-- 실측 사례 (9B 2026-06): 같은 검증에서 cross-month 미반영 시 1건(이서연 6/1) false positive 발생, 5/31 N 반영 후 0건
```

### I-5. ND/NE/EOD SQL
```sql
SELECT COUNT(*) FROM schedule_entries a
JOIN schedule_entries b ON b.schedule_id=a.schedule_id AND b.nurse_id=a.nurse_id
  AND b.work_date = DATEADD(DAY,1,a.work_date)
WHERE a.schedule_id = :sid AND (
  (a.shift_id='N' AND b.shift_id IN ('D','E'))
  OR (a.shift_id='E' AND b.shift_id='D')
);
-- 합격: 0
```

### I-6. 일별 coverage SQL
```sql
SELECT work_date,
  SUM(CASE WHEN shift_id='D' THEN 1 ELSE 0 END) D_cnt,
  SUM(CASE WHEN shift_id='E' THEN 1 ELSE 0 END) E_cnt,
  SUM(CASE WHEN shift_id='N' THEN 1 ELSE 0 END) N_cnt
FROM schedule_entries WHERE schedule_id = :sid
GROUP BY work_date ORDER BY work_date;
-- 합격: 모든 row가 daily_shift need와 일치
```

---

## J. PR 승인 전 최종 게이트

- [ ] A (HARD 19항): 모두 0건
- [ ] B (SOFT 10항): 합격 기준 충족 (정책상 예외는 C 카테고리에서 정합 확인)
- [ ] C (정책 7항): 변경 의도가 결과에 반영됨
- [ ] D (Invariant 9항): pytest 통과
- [ ] E (UNSAT/진단 8항): reason/conflict 정합
- [ ] F (운영 게이트 7항): CI 성공 + 아티팩트 생성
- [ ] G (42규칙): 회귀 0
- [ ] H (Agent QA 5항): 모두 통과
- [ ] 결과 기록: `triage.md` + `summary.json`

총 항목: **70개** (A19 + B10 + C7 + D9 + E8 + F7 + G(매트릭스) + H5)

---

## K. 미해결 알려진 이슈 (참고)

| 항목 | 영향 | 추적 |
|---|---|---|
| `_run_cp_sat_basic` ConstraintImpact NameError (`_assignments` 등 3변수 미정의) | metadata attach만 실패, 솔버 결과 정상 | roster_create_service.py line 2862-2882 |
| ICU n_max=7 정책 변경 직후 일부 nurse N=13 케이스 | 의도된 soft cap 초과 | C-03 항목으로 정합성 검증 |
| 50-case 자동 회귀 스위트 미자동화 | P0 케이스 (T-WIN-01/02, T-NUR-02/03) 수동 실행 | CONSTRAINT_TESTCASE_MATRIX_SPEC.md |

---

## L. 운영팀 그룹별 요구사항 (2026-05-24 정리)

### L-1. 시화 중환자실1 (group_id=`10135857f9f9`, office=시화병원)

| ID | 요구사항 | 분류 |
|---|---|---|
| L1-01 | 나이트 전체 균일 — 1인당 월 N ≤ 7 (전원 동일) | HARD |
| L1-02 | 그레이드 1,2만 필수 (3/4 등급 최소 인원 비요구) | HARD |
| L1-03 | OFF/연차 처리 균등 (off_swap 균등 분배) | SOFT |

### L-2. 시화 9병동 (group_id=`101358ddf07b` 메인 + 9A/9B/CCR/NA·LP)

| ID | 요구사항 | 분류 |
|---|---|---|
| L2-01 | 나이트 — 전담자에서 줄이더라도 N은 균등하게 분배 | SOFT (N range 최소화) |
| L2-02 | 그레이드 1,2만 필수 (3 등급 최소 인원 비요구) | HARD |
| L2-03 | 장기오프(원티드 OFF 길게) 이후 OFF/연차 처리 균등 | SOFT (추후 작업) |
| L2-04 | shift는 DDDEE처럼 묶음 형태로 끊어줄 것 (동일 shift 연속 선호) | SOFT |
| L2-05 | 동일 근무 최대 4회 연속 | HARD (max_same_shift=4) |
| L2-06 | 30일 근무자 (월 OFF=0) alert | 검증/경고 |

### L-3. 현재 DB 정책 vs 요구사항 갭 분석

#### 시화 중환자실1 (config_id=1324 / grade config_id=10)

| 요구 ID | DB 컬럼 / 값 | 정합 |
|---|---|---|
| L1-01 | `nurse_monthly_limits.n_max=7` (27명 균일) | ✅ DB 일치, 단 schedule v119 결과 nurse 271772 N=13 → solver soft cap 동작 의심 |
| L1-02 | `roster_grade_config.constraints_json` = `{D:{1:1,2:1,3:0,4:0}, E:{...}, N:{...}}` + `allow_soft_fallback=0` | ✅ 완전 일치 |
| L1-03 | `roster_config.off_first=True`, `off_swap_enabled=True`, `off_days=9.0` | ✅ |

#### 시화 9병동-9B (config_id=1257 / grade config_id=11)

| 요구 ID | DB 컬럼 / 값 | 정합 |
|---|---|---|
| L2-01 | `max_nig_per_month=15` (그룹 단위), `nurse_monthly_limits` 2026-06 0건 | ⚠️ 개인별 N 균등 제약 없음 |
| L2-02 | `constraints_json` = `{D:{1:1,2:0,3:0}, E:{...}, N:{...}}` | ❌ **1등급만 필수, 2등급 누락** |
| L2-03 | 정책 미반영 | 추후 작업 명시 |
| L2-04 | 컬럼 없음 (DDDEE 묶음 선호 패널티 없음) | ❌ 미반영 |
| L2-05 | 컬럼 없음 (`max_same_shift` 컬럼 미존재) | ❌ 미반영 (`2026_06_SCHEDULE_CONFIG_CHANGES.md`에서 weight=10000 언급, DB 컬럼 추가 또는 config_dict 주입 필요) |
| L2-06 | 검증 로직 없음 | ❌ alert 미구현 |

#### 시화 9병동 기타 하위 그룹 (9A/CCR/NA·LP)

| 그룹 | 2N→2OFF | 3N→2OFF | grade config | 비고 |
|---|---|---|---|---|
| 9A | **False** ⚠️ | True | 없음 | 운영팀 요구와 불일치 가능 |
| CCR | **False** ⚠️ | True | 없음 | 동일 |
| NA/LP | **False** ⚠️ | True | 없음 | 동일 |

### L-4. 검증 시 추가 체크 항목 (운영팀 요구 → 자동 SQL)

| ID | 항목 | SQL / 검증 |
|---|---|---|
| L-A1 | 시화 중환자실 N ≤ 7 / nurse (HARD) | per-nurse N 합 GROUP BY nurse_id 후 MAX(N) ≤ 7 |
| L-A2 | 시화 9병동 동일 shift 4연속 위반 | 연속 4회 동일 shift_id 탐색 → COUNT 0 |
| L-A3 | 시화 9병동 N range (균등) | per-nurse N max−min, 권장 ≤ 3 |
| L-A4 | 시화 9병동 30일 근무자 alert | nurse별 OFF 합 = 0 인 row 탐색 → 비어있어야 함 |
| L-A5 | 그레이드 1,2 필수 충족 (양 그룹) | day×shift에서 grade in (1,2) 인 nurse ≥ 1 |
| L-A6 | DDDEE 묶음 선호 측정 | 동일 shift 2~4 연속 빈도 분포, 모니터링 (HARD 아님) |

### L-5. 즉시 조치 필요 항목 (정합성 회복)

1. **시화 9병동 grade config 2등급 추가** — 운영팀 요구 "1,2 필수" 명시. 현재 9B만 grade config 있고 1등급만 설정. 9A/CCR/NA/LP는 grade config 자체 부재 → 사용자 결정 후 update
2. **시화 9병동 동일 shift 4회 한도 정책 도입** — DB 컬럼 추가 or config_dict 주입
3. **30일 근무자 alert 검증 추가** — solver 후 validator에 1줄 추가
4. **시화 중환자실 N=13 케이스** — n_max=7 hard였는데 N=13 발생 원인 진단 (soft fallback 코드 경로 확인 필요)
5. **9A/CCR/NA/LP 2N→2OFF False** — 운영팀과 정책 정합 재확인 (정책 의도인지 누락인지)

---

## M. Constraint Impact Graph 메타 검증 (4항, 2026-05-22 신규)

| ID | 항목 | 출처 | 검증 |
|---|---|---|---|
| M-01 | SemanticsSnapshot 5계층 구조 | docs/CONSTRAINT_IMPACT_GRAPH_ACTIVE_BLUEPRINT.md + SCHEMA_AND_API.md | `snapshot_builders.py` → constraint_impact 응답 payload 존재 |
| M-02 | AssignmentAtom 메타 (fixed / fixed_wanted / coverage_excluded 등) | docs/CONSTRAINT_IMPACT_GRAPH_SCHEMA_AND_API.md §2.4~3.2 | 각 atom에 source + metadata 부착 확인 |
| M-03 | carryover artifact 기반 first-day transition | docs/CONSTRAINT_IMPACT_GRAPH_SCHEMA_AND_API.md §3.2 | issued/latest/blank 상태 반영, `recovery_debt` seed 계산 |
| M-04 | graph_consistency missing/extra 정합 (F-03 보강) | 본 docs | `summary.graph_consistency.missing_in_graph = []` + `extra_in_graph = []` |

> **연관 이슈**: `_run_cp_sat_basic` ConstraintImpact NameError (K 섹션) 가 해결되어야 M-01~M-03 메타가 실제 attach됨. 현재는 try-except로 silently 빈 값.

---

## N. 6월 cfg 키 구체값 (실제 적용 검증용, 8항)

| cfg 키 | 값 | 출처 | 검증 SQL/명령 |
|---|---|---|---|
| `team_min_soft_fallback` | True | SC §1 | `SELECT team_min_soft_fallback FROM roster_config WHERE config_id=:cid` |
| `team_min_penalty_weight` | 500 | SC §1 | (코드 상수, 또는 cfg JSON) |
| `max_same_shift` | True | SC §6 | `SELECT max_same_shift FROM roster_config WHERE config_id=:cid` |
| `max_same_shift_penalty_weight` | 10000 | SC §6.4 | (코드 상수) |
| `n_to_n_interval_target` | 10 | SC §9.2, commit 5e7c665 | (cfg JSON) |
| `n_to_n_interval_penalty_weight` | 50 | SC §9.2 | (코드 상수) |
| `n_to_n_interval_max_window` | 15 | SC §9.2 | (cfg JSON) |
| `enforce_4o_hard` | False | SC §8, commit 30c0c75 | env `ROSTER_DISABLE_4O_HARD` override 가능 |

### N-1. 시화 두 그룹 cfg 현재 적용 상태 (2026-05-24 기준)

| key | 시화 중환자실1 (config_id=1324) | 시화 9B (config_id=1257) |
|---|---|---|
| `max_nig_per_month` | 0 (그룹 default 비활성, nurse_monthly_limits.n_max=7 우선) | 15 (개인별 nml 없음) |
| `max_conseq_work` | 5 | 5 |
| `two_offs_after_two_nig` | True | True |
| `two_offs_after_three_nig` | True | True |
| `banned_day_after_eve` | True (E→D 금지) | True |
| `nod_noe` | True | True |
| `not_one_night` | True | True |
| `off_first` | True | False |
| `off_swap_enabled` | True | False |
| `use_mid` | False | False |
| `off_days` | 9.0 | 11.0 |
| `grade_strategy` | BASE | BASE |

> **갭**: 9B의 `off_first/off_swap_enabled` 모두 False — 운영팀의 "OFF/연차 균등" 요구(L2-03)와 정합성 검토 필요.
> **갭**: 9병동 4개 하위 그룹 중 9B 외에는 `two_offs_after_two_nig=False` — L 섹션에서 이미 표기.

---

## O. 최근 commit 기반 신규 feature 검증 (8건, 2026-05-22~24)

| ID | commit | 검증 항목 |
|---|---|---|
| O-01 | 1020142 `team_auto_assign greedy + 2-opt` | E-12 적용 |
| O-02 | 9d6eb5c `AUTO-SOFT ontology treatment 라벨` | C-06, E-11 적용 |
| O-03 | 5e7c665 `N→N gap soft target 10일` | S-10, S-11, C-11 적용 |
| O-04 | 30c0c75 `4O hard 디폴트 해제` | C-07, C-10, N 표 적용 |
| O-05 | c8557bb `AUTO-SOFT 재시도 — grade hard 유지, team_min hard→soft` | C-08, C-09 적용 |
| O-06 | 3a62e1a `max_same_shift soft 4연속 패널티` | S-12, C-12 적용 |
| O-07 | f2c3ff0 `team_min 디폴트 soft + 6월 운영 변경 문서화` | C-08, S-13 적용 |
| O-08 | 1eac653 `advanced_inference 솔버 시간 토글` | RosterRequest 인자 검증 (frontend payload 확인) |

---

## P. 총 항목 수 (업데이트)

| 카테고리 | 이전 | 신규 | 합계 |
|---|---|---|---|
| A. HARD | 19 | 3 (H-20~22) | 22 |
| B. SOFT/Fairness | 10 | 3 (S-11~13) | 13 |
| C. 6월 정책 | 7 | 5 (C-08~12) | 12 |
| D. Invariant | 9 | 0 | 9 |
| E. UNSAT/진단 | 8 | 4 (E-09~12) | 12 |
| F. 운영 게이트 | 7 | 0 | 7 |
| G. 42규칙 매트릭스 | (매트릭스) | 0 | (매트릭스) |
| H. Agent QA | 5 | 0 | 5 |
| L. 그룹별 요구 | 9 (시화 ICU 3 + 9병동 6) | 0 | 9 |
| M. Impact Graph (신규) | 0 | 4 | 4 |
| N. cfg 키 구체값 (신규) | 0 | 8 | 8 |
| O. commit 기반 feature (신규) | 0 | 8 | 8 |
| **합계** | **74** | **35** | **109+** |
