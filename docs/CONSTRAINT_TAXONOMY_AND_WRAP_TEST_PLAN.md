# Constraint Taxonomy & Wrap Test Plan (v1)

> 목적
>
> 1) 온톨로지/그래프/실제 solver output 정합성을 검증할 수 있도록 제약을 **원인-대상-환경 관점**으로 재분류한다.  
> 2) “모든 노드 나열”이 아니라 **대표 원인 묶음 + 해결법** 중심으로 해석 가능한 테스트 케이스 매트릭스를 정의한다.  
> 3) 현재 wrap 적용 상태를 명시하고, 누락/오차 여부를 재현 가능한 케이스로 점검한다.

---

## 0. Source of Truth

- Ontology family/mode: `app/services/semantics/ontology.yaml` (version: 2)
- Hard/Soft 구성 코드:
  - Hard 제약: `app/services/cp_sat_basic.py`, `app/services/cp_sat/fallback_lex.py`
  - Conflict core 추출: `app/services/cp_sat/hard_assumption.py`
  - Detector 기반 conflict: `app/services/precheck/conflict_detector.py`
  - Soft objective: `app/services/cp_sat/objective_terms.py`, `app/services/cp_sat/fallback_objectives.py`

---

## 1. Full Taxonomy (누락 없음)

아래는 ontology `constraints` 전체를 실무 해석 관점으로 재분류한 표다.

### 1.1 Coverage Feasibility (수요/구성 충족)

| Family | 핵심 질문(원인) | 대상 | 환경/범위 | 기본 성격 | soft_fallback mode | 비고 |
|---|---|---|---|---|---|---|
| `CoverageMin` | 일/시프트 최소 인원 요구를 채울 수 있는가 | day×shift | 월 전체/일별 수요 | Hard | 가능 | `NO_ASSIGNMENT` alias 포함 |
| `TeamMin` | 팀별 최소 인원 요구 충족 가능한가 | team×day×shift | 팀 구성/활성 인원 | Hard | 가능 | 후보 부족 시 skipped_by_capacity 가능 |
| `GradeMin` | 숙련도 최소 하한 충족 가능한가 | grade×day×shift | grade 분포 | Hard | 가능 | |
| `GradeMax` | 숙련도 최대 상한 때문에 수요가 막히는가 | grade×day×shift | grade 분포 | Hard | 가능 | |
| `TeamGradeHandoff` | 팀-숙련도 교차 handoff가 막는가 | team×grade×day×shift | 교차 제약 | Hard | 가능 | |

### 1.2 Temporal Pattern Hard (window/transition)

| Family | 핵심 질문(원인) | 대상 | 환경/범위 | 기본 성격 | soft_fallback mode | 비고 |
|---|---|---|---|---|---|---|
| `ConsecutiveWorkLimit` | 연속근무 상한 초과가 불가피한가 | nurse×day_window | 월내 + 경계 | Hard | 불가 | max_consecutive_work_days |
| `ConsecutiveNightLimit` | 연속 야간 상한 초과가 불가피한가 | nurse×day_window | 월내 + 경계 | Hard | 불가 | max_consecutive_nights |
| `NightRecovery` | 2N2O/3N2O 회복 OFF 강제가 커버리지와 충돌하는가 | nurse×day_window | 야간 블록 후 회복구간 | Hard | 불가 | bypassed_by_fixed 가능 |
| `OffWindow` | 경계/파생 OFF 강제 구간이 충돌하는가 | nurse×day_window | 전월 tail + 월초 | Hard | 불가 | 파생 제약(직접 disable 없음) |
| `BoundaryTransitionBan` | ND/ED/NE 금지 전이가 fixed/수요와 충돌하는가 | nurse×boundary_day | 월내 + 경계일 | Hard | 불가 | cross-month forbidden 변환 포함 |
| `NotOneNight` | 1N 금지와 고정/회복/N상한이 충돌하는가 | nurse×day | 인접일 패턴 | Hard | 불가 | |

### 1.3 Nurse Capacity/Eligibility Hard

| Family | 핵심 질문(원인) | 대상 | 환경/범위 | 기본 성격 | soft_fallback mode | 비고 |
|---|---|---|---|---|---|---|
| `MonthlyNightCap` | 월 N 상한이 역할/수요와 충돌하는가 | nurse×month | 월 누적 N | Hard | 불가 | |
| `OffCap` | 월 OFF cap/o_exact가 다른 제약과 충돌하는가 | nurse×month | 월 누적 OFF | Hard | 불가 | monthly limit 우선 |
| `AllowedShiftMask` | 허용되지 않은 shift mask로 대체가 막히는가 | nurse×day×shift | role/profile | Hard | 불가 | N-only/D-only 포함 |
| `WeekendOffOnly` | 평일 OFF 금지/주말 OFF 정책이 feasible한가 | nurse×weekday | 주중/주말 정책 | Hard | 불가 | |
| `BanNightBeforeFixedOff` | 휴가/공가 직전 N 금지가 feasible한가 | nurse×day | fixed off 인접 | Hard | 불가 | |

### 1.4 Coupling / Boundary / Override Hard

| Family | 핵심 질문(원인) | 대상 | 환경/범위 | 기본 성격 | soft_fallback mode | 비고 |
|---|---|---|---|---|---|---|
| `PrecepteeSync` | 프리셉터-프리셉티 동기화가 가능한가 | pair×day | 프리셉터 기간 | Hard | 불가 | |
| `AssignmentWindow` | join/leave/파견 윈도우 제약 충돌이 있는가 | nurse×period | 활성기간 | Hard | 불가 | |
| `CarryoverBoundary` | 전월 상태 carryover가 월초 제약과 충돌하는가 | nurse×boundary_day | 월경계 | Hard | 불가 | |
| `FixedWanted` | 확정 원티드가 우회/비우회 제약과 충돌하는가 | nurse×day×shift | 고정 셀 | Hard(override) | 불가 | 일부 family bypass, 일부 cannot_bypass |

### 1.5 Precheck / Meta Integrity

| Family | 핵심 질문(원인) | 대상 | 환경/범위 | 기본 성격 | soft_fallback mode | 비고 |
|---|---|---|---|---|---|---|
| `ConfigIntegrity` | 설정 정합성 자체가 깨졌는가 | config×group×month | solver 이전 | Precheck block | N/A | 예: grade min>max, allowed mask 단절 등 |

---

## 2. Soft Objective Taxonomy (feasible 내 최적화)

아래 항목은 기본적으로 **infeasible 원인**이 아니라, feasible 해 중 품질/선호 최적화 항이다.

| Soft Group | 예시(코드/지표) | 대상 | 목적 |
|---|---|---|---|
| Transition soft penalty | NOD/NOE (`NOD_NOE_PENALTY`) | nurse×day | 덜 선호되는 전이 감소 |
| OFF shape penalty | sequential off / weekly off 주변 OFF penalty | nurse×window | OFF 뭉침/낭비 최소화 |
| Night distribution fairness | N 편차/KLD/분산(`NIGHT_DEVIATION_PENALTY`, KLD terms) | nurse×month | 야간 분산 공정성 |
| Preference alignment | monthly preference / preference score | nurse×day×shift | 선호 반영도 향상 |

---

## 3. Hard Wrap Coverage (현재 구현 상태)

현재 CP-SAT assumption core 노출 기준(Primary/Fallback 동시 반영):

| Wrap Pattern | 상태 | 위치(요약) |
|---|---|---|
| `fixed_assignment` | ✅ 적용 | fixed 셀 강제 배정 |
| `initial_forbidden` / `forbidden_shift` | ✅ 적용 | 금지 셀/금지 shift |
| `transition_ban` | ✅ 적용 | ND/ED/NE 전이 금지 |
| `allowed_shift_mask` | ✅ 적용 | nurse role 기반 shift 금지 |
| `max_consecutive_work` | ✅ 적용 | K+1 창 최소 1 OFF |
| `off_window_requirement` | ✅ 적용 | 월경계 OFF 윈도우 강제 |
| `consecutive_night_cap` | ✅ 적용 | 연속 야간 상한 창 |

> 참고: `allowed_shift_mask`, `consecutive_night_cap`는 코어 minimal set 특성상 특정 런에서 top-level pattern 미노출 가능.  
> 이 경우 `members` 참여 여부까지 함께 검증해야 한다.

---

## 4. Test Matrix for Ontology↔Graph↔Output Consistency

## 4.1 공통 검증 항목 (모든 케이스)

각 케이스마다 다음을 검사한다.

1. Solver 결과(`success/fallback/unsat`)와 conflict payload 존재 여부 일치
2. `infeasibility.conflict_cores[*].pattern`이 기대 패밀리와 정합
3. `members[*].type/node_id`가 family semantics와 정합
4. ontology graph의 `ConflictCoreNode`/`BLOCKED_RUN`/`MEMBER_OF_CONFLICT` 연결 정합
5. 대표 원인 집계(`affected_count`, `per_nurse_cores`)가 과/중복 없이 해석 가능

## 4.2 카테고리별 케이스

| Case ID | 카테고리 | 주요 family | 기대 결과 |
|---|---|---|---|
| `T-COV-01` | Coverage 단일 hard | CoverageMin | `cpsat_mus:coverage_min` 또는 동등 root 노출 |
| `T-COV-02` | Coverage 교차 hard | TeamMin + GradeMax | multi-family core + 해결 힌트 2종 이상 |
| `T-WIN-01` | Window hard 단일 | BoundaryTransitionBan | `transition_ban` 노출 |
| `T-WIN-02` | Window hard 복합 | NotOneNight + NightRecovery + OffWindow | 복합 코어/집계 코어 노출 |
| `T-NUR-01` | Nurse hard 단일 | MonthlyNightCap | nurse scope 코어 노출 |
| `T-NUR-02` | Nurse eligibility | AllowedShiftMask + FixedWanted | `allowed_shift_mask` 또는 관련 금지 코어 노출 |
| `T-NUR-03` | N-only 충돌 | AllowedShiftMask + OffCap + MonthlyNightCap + n_exact | `n_only_vs_caps`/유사 결론 노출 |
| `T-COUP-01` | Coupling | PrecepteeSync | pair/day 충돌 신호 노출 |
| `T-BOUND-01` | Carryover boundary | CarryoverBoundary + TransitionBan | 월경계 충돌 신호 노출 |
| `T-META-01` | Precheck block | ConfigIntegrity | solver 진입 전 reason_code 차단 |
| `T-SOFT-01` | Soft only | NOD/NOE/SequentialOff/N fairness | infeasible 없이 score/penalty 변화만 |

## 4.3 현재 wrap 중심 우선 실행 케이스

| Priority | Case ID | 목적 |
|---|---|---|
| P0 | `T-WIN-01` | `transition_ban` 회귀 방지 |
| P0 | `T-NUR-02` | `allowed_shift_mask` 노출 확인 |
| P0 | `T-NUR-03` | N-only 복합 충돌 해석 검증 |
| P0 | `T-WIN-02` | `max_consecutive_work`/`off_window_requirement` 검증 |
| P1 | `T-NUR-01` | `consecutive_night_cap` 멤버 참여 검증 |

---

## 5. “대표 원인 묶음” 해석 규칙 (UI/Agent 공통)

노드 과다를 줄이기 위해 해석 시 아래 순서를 강제한다.

1. `scope=multi_nurse` + `affected_count` 큰 코어를 대표 원인으로 우선
2. 동일 `pattern`의 nurse별 코어는 cohort로 묶어 1건으로 요약
3. `data_quality` 계열 코어는 “증폭 요인”으로 분리 표시 (직접 UNSAT 원인과 분리)
4. 마지막에 전역 신호(`infeasibility:no_assignment`)를 결과 요약으로 배치

---

## 6. Gaps / Next

- `allowed_shift_mask`, `consecutive_night_cap`은 top-level pattern 미노출이 가능하므로 member-level 검증 자동화 필요
- run-node detail에서 “대표 원인 카드 + 해결 액션 TOP N” 자동 집계 표시 추가 필요
- 테스트 구현 시 `case_id → expected pattern/member/source`를 고정 스냅샷으로 관리 권장

---

## 7. Related Docs

- `docs/ONTOLOGY_RULE_GRANULARITY_SPEC.md`
- `docs/HARNESS_TO_ONTOLOGY_HYPERGRAPH_MAPPING.md`
- `docs/HARNESS_CHECKLIST_COVERAGE_MATRIX.md`
- `docs/INFEASIBLE_DIAGNOSTICS_FRONT_BACK_ARCHITECTURE.md`
