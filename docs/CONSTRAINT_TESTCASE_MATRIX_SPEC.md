# Constraint Testcase Matrix Spec (Wrap-First)

> 목적: 현재 wrap 적용된 hard constraint를 중심으로, ontology/graph/output 정합성 테스트를 바로 실행 가능한 수준으로 표준화한다.

---

## 1. Scope

- 대상: `roster_create/generate` 기반 infeasible / fallback-infeasible 런
- 검증 축:
  1. payload (`infeasibility.conflict_cores`)
  2. graph_export (`ConflictCoreNode`, `BLOCKED_RUN`, `MEMBER_OF_CONFLICT`)
  3. ontology UI node detail (`/ontology/node/{id}`)

---

## 2. Assertions Contract

각 케이스는 아래 6개를 기본 검증한다.

1. `solver_status` 예상 범위 일치 (`fallback-infeasible` 또는 `primary-unsat` 등)
2. 기대 `pattern`이 top-level core 또는 member 경로에서 검출
3. 기대 `scope` (`nurse` / `multi_nurse` / `global`) 일치
4. `resolution_hints` 최소 1개 존재
5. graph에서 해당 core 노드가 `BLOCKED_RUN`으로 run 노드와 연결
6. graph에서 member 노드가 `MEMBER_OF_CONFLICT`로 core에 연결

---

## 3. Case Catalog (50 Cases, Complex-First)

> 원칙: 단일 충돌보다 다중(3~10+) 충돌을 우선한다.  
> 각 케이스는 반드시 **대표 원인 묶음(aggregate cause)** 과 **완화 레버(what to relax)** 를 갖는다.

| # | Case ID | 카테고리 | 겹치는 주요 제약(핵심) | 복합도 | 대표 원인 묶음(예상) | 우선 완화 레버 |
|---|---|---|---|---:|---|---|
| 1 | `CX-WIN-001` | Window Hard | TransitionBan + FixedWanted + CarryoverBoundary | 3 | 경계 전이 금지 묶음 | TransitionBan nurse-day 완화 |
| 2 | `CX-WIN-002` | Window Hard | NotOneNight + Fixed OFF neighbors + AllowedShiftMask | 3 | 1N 금지-인접일 봉쇄 묶음 | NotOneNight 임시 완화 |
| 3 | `CX-WIN-003` | Window Hard | ConsecutiveWorkLimit + OffWindow + OffCap | 3 | 연속근무 윈도우 OFF 불능 | max_consecutive_work_days 상향 |
| 4 | `CX-WIN-004` | Window Hard | NightRecovery(2N2O) + CoverageMin + TeamMin | 3 | 회복 OFF vs 커버리지 충돌 | 해당 day CoverageMin 완화 |
| 5 | `CX-WIN-005` | Window Hard | NightRecovery(3N2O) + GradeMin + TeamMin | 3 | 3N2O 회복으로 팀/grade 동시 부족 | TeamMin soft fallback |
| 6 | `CX-WIN-006` | Window Hard | ConsecutiveNightLimit + MonthlyNightCap + N demand high | 3 | N 상한 이중 캡 충돌 | MonthlyNightCap 상향 |
| 7 | `CX-WIN-007` | Window Hard | OffWindow + CarryoverBoundary + WeekendOffOnly | 3 | 월경계 OFF 강제-평일 OFF 금지 충돌 | WeekendOffOnly 비활성 |
| 8 | `CX-WIN-008` | Window Hard | TransitionBan + NotOneNight + NightRecovery | 3 | 전이금지/1N금지/회복 규칙 동시 잠금 | TransitionBan 일시 해제 |
| 9 | `CX-WIN-009` | Window Hard | TransitionBan + AllowedShiftMask + FixedWanted | 3 | 허용마스크로 대체불가 전이 충돌 | AllowedShiftMask 임시 완화 |
| 10 | `CX-WIN-010` | Window Hard | ConsecutiveWorkLimit + TeamMin + CoverageMin + FixedAssignment | 4 | 연속근무 창 봉쇄로 최소커버 실패 | FixedAssignment 일부 해제 |
| 11 | `CX-SFT-011` | Window Soft | NOD/NOE soft + hard TransitionBan + NotOneNight | 3 | hard 원인 우세, soft는 보조 | hard만 완화(soft 유지) |
| 12 | `CX-SFT-012` | Window Soft | SequentialOff soft + OffCap + NightRecovery | 3 | OFF 형상 soft가 hard 충돌 증폭 | OffCap 완화 |
| 13 | `CX-SFT-013` | Window Soft | N fairness soft + MonthlyNightCap + N-only 다수 | 3 | N 분산 요구 vs N 상한 충돌 | MonthlyNightCap 상향 |
| 14 | `CX-SFT-014` | Window Soft | NOD/NOE soft + CarryoverBoundary + TransitionBan | 3 | 경계 전이 hard 원인이 본질 | TransitionBan 완화 |
| 15 | `CX-SFT-015` | Window Soft | SequentialOff soft + WeekendOffOnly + OffWindow | 3 | OFF 제약 hard 묶음 | WeekendOffOnly 완화 |
| 16 | `CX-NUR-016` | Nurse Hard | MonthlyNightCap + OffCap + N_exact(1) + N-only | 4 | n_only_vs_caps 묶음 | N_exact 완화/MonthlyNightCap 상향 |
| 17 | `CX-NUR-017` | Nurse Hard | AllowedShiftMask + TeamMin + CoverageMin | 3 | 허용 shift 단절로 팀최소 불가 | AllowedShiftMask 완화 |
| 18 | `CX-NUR-018` | Nurse Hard | WeekendOffOnly + TeamMin + GradeMin | 3 | 주말휴무자로 평일 가용 붕괴 | WeekendOffOnly 완화 |
| 19 | `CX-NUR-019` | Nurse Hard | BanNightBeforeFixedOff + MonthlyNightCap + N demand | 3 | N 전일 금지로 N 캡 경직 | ban_night_before_fixed_off 비활성 |
| 20 | `CX-NUR-020` | Nurse Hard | OffCap + FixedWanted(OFF 다수) + CoverageMin | 3 | OFF cap 초과/커버리지 동시 | OffCap 완화 |
| 21 | `CX-NUR-021` | Nurse Hard | AssignmentWindow(join late) + TeamMin + CoverageMin | 3 | active days 감소로 최소충족 실패 | TeamMin 완화 |
| 22 | `CX-NUR-022` | Nurse Hard | AssignmentWindow(leave early) + GradeMin + TeamMin | 3 | 숙련 인력 active window 부족 | GradeMin soft fallback |
| 23 | `CX-NUR-023` | Nurse Hard | PrecepteeSync + AllowedShiftMask + TeamMin | 3 | 동기화로 배치 자유도 급감 | PrecepteeSync 기간 축소 |
| 24 | `CX-NUR-024` | Nurse Hard | PrecepteeSync + NightRecovery + NotOneNight | 3 | 페어 동기화와 야간 규칙 충돌 | PrecepteeSync 완화 |
| 25 | `CX-NUR-025` | Nurse Hard | CarryoverBoundary + MonthlyNightCap + ConsecutiveNightLimit | 3 | 전월 tail로 월초 N 윈도우 잠김 | MonthlyNightCap 조정 |
| 26 | `CX-COV-026` | Coverage Hard | CoverageMin + TeamMin + GradeMin | 3 | day/shift 최소 조건 과다 | TeamMin/GradeMin 단계 완화 |
| 27 | `CX-COV-027` | Coverage Hard | TeamMin + GradeMax + AllowedShiftMask | 3 | grade 상한 + 허용마스크로 팀최소 불가 | GradeMax soft fallback |
| 28 | `CX-COV-028` | Coverage Hard | CoverageMin + FixedAssignment + InitialForbidden | 3 | 고정/금지로 커버 셀 봉쇄 | FixedAssignment/Forbidden 재조정 |
| 29 | `CX-COV-029` | Coverage Hard | TeamGradeHandoff + TeamMin + GradeMin | 3 | 팀-숙련 교차 제약 과밀 | TeamGradeHandoff soft fallback |
| 30 | `CX-COV-030` | Coverage Hard | CoverageMin + WeekendOffOnly + AssignmentWindow | 3 | 평일 공급 급감 | WeekendOffOnly 완화 |
| 31 | `CX-COV-031` | Coverage Hard | CoverageMin + NightRecovery + ConsecutiveWorkLimit | 3 | 회복+연속근무 창으로 셀 미달 | CoverageMin 완화 |
| 32 | `CX-COV-032` | Coverage Hard | TeamMin + CarryoverBoundary + TransitionBan | 3 | 경계일 전이 제한으로 팀충족 실패 | TransitionBan 완화 |
| 33 | `CX-COV-033` | Coverage Hard | GradeMin + GradeMax + TeamMin + FixedWanted | 4 | grade 샌드위치 + 고정편향 | GradeMax 또는 TeamMin 완화 |
| 34 | `CX-COV-034` | Coverage Hard | CoverageMin + AllowedShiftMask + PrecepteeSync | 3 | 동기화+마스크로 대체 인력 부재 | AllowedShiftMask 완화 |
| 35 | `CX-COV-035` | Coverage Hard | TeamMin + OffCap + OffWindow + NightRecovery | 4 | OFF 계열 제약 누적으로 팀부족 | OffCap 완화 |
| 36 | `CX-OVR-036` | Override/Fixed | FixedWanted + BoundaryTransitionBan + NotOneNight | 3 | fixed override가 패턴 제약과 충돌 | fixed entry 조정 |
| 37 | `CX-OVR-037` | Override/Fixed | FixedWanted + AllowedShiftMask + MonthlyNightCap | 3 | 고정이 허용마스크/N cap와 충돌 | FixedWanted 수정 |
| 38 | `CX-OVR-038` | Override/Fixed | FixedWanted + BanNightBeforeFixedOff + NightRecovery | 3 | 고정 OFF 인접 N 금지/회복 충돌 | ban_night_before_fixed_off 완화 |
| 39 | `CX-OVR-039` | Override/Fixed | FixedAssignment + InitialForbidden + CoverageMin | 3 | 강제/금지 동시로 no-assignment | 고정 해제 우선 |
| 40 | `CX-OVR-040` | Override/Fixed | FixedWanted + TeamMin + GradeMin + TeamGradeHandoff | 4 | 고정 편향이 팀/숙련 구조 붕괴 | TeamMin soft fallback |
| 41 | `CX-META-041` | Precheck/Meta | GradeMin>Max + TeamMin config mismatch | 2 | ConfigIntegrity 차단 | grade/team config 수정 |
| 42 | `CX-META-042` | Precheck/Meta | Mid disabled but requirements include M | 2 | ConfigIntegrity(MID) 차단 | use_mid 또는 요구치 정정 |
| 43 | `CX-META-043` | Precheck/Meta | Allowed shift isolates nurse + N_exact strict | 2 | role isolation 경고 | allowed shift 확장 |
| 44 | `CX-META-044` | Precheck/Meta | Fixed assignments exceed daily need | 2 | fixed 과다 차단 | fixed 수량 축소 |
| 45 | `CX-META-045` | Precheck/Meta | Monthly limit min/max arithmetic contradiction | 2 | monthly limit 모순 | limit 재설정 |
| 46 | `CX-MIX-046` | 10+ 복합 | N-only 3명 + N_exact + MonthlyNightCap + OffCap + NotOneNight + TransitionBan + NightRecovery + TeamMin + CoverageMin + CarryoverBoundary | 11 | N-only cohort 충돌 + 커버리지 전역 붕괴 | N_exact 또는 MonthlyNightCap부터 완화 |
| 47 | `CX-MIX-047` | 10+ 복합 | WeekendOffOnly 다수 + AssignmentWindow + TeamMin + GradeMin + GradeMax + FixedWanted + OffWindow + ConsecutiveWorkLimit + CoverageMin + TeamGradeHandoff | 10 | 평일 공급붕괴 + 팀/grade 교차 충돌 | WeekendOffOnly/TeamMin 동시 완화 |
| 48 | `CX-MIX-048` | 10+ 복합 | PrecepteeSync + AllowedShiftMask + TransitionBan + NotOneNight + NightRecovery + ConsecutiveNightLimit + MonthlyNightCap + TeamMin + GradeMin + CoverageMin | 10 | 동기화-야간규칙-커버리지 3축 충돌 | PrecepteeSync/AllowedMask 완화 |
| 49 | `CX-MIX-049` | 10+ 복합 | FixedAssignment 대량 + InitialForbidden 다수 + FixedWanted + BanNightBeforeFixedOff + OffCap + OffWindow + TeamMin + GradeMax + CoverageMin + CarryoverBoundary | 10 | 고정/금지 과밀로 탐색공간 붕괴 | fixed/forbidden 해제 우선 |
| 50 | `CX-MIX-050` | 10+ 복합 | N-only + D-only 혼재 + AllowedShiftMask + TeamMin + GradeMin + GradeMax + TransitionBan + NotOneNight + NightRecovery + MonthlyNightCap + CoverageMin | 11 | role 분할로 shift cover 단절 | AllowedShiftMask/TeamMin 우선 완화 |

### 3.1 카테고리 요약(50개)

- Window Hard: 10
- Window Soft 연계: 5
- Nurse Hard: 10
- Coverage Hard: 10
- Override/Fixed: 5
- Precheck/Meta: 5
- 10+ 복합 스트레스: 5

합계: **50 cases**

---

## 4. Data/Scenario Preparation Guidelines

1. **DB 직접 조작 금지**: 설정은 저장 endpoint로만 반영
2. 월 고정: 우선 `2026-05/06/07`
3. 원복 필수: 케이스 종료마다 reset endpoint 실행
4. 토큰 컨텍스트 고정: 동일 office/group에서 반복

---

## 5. Execution Template (per case)

1. pre-state 캡처 (`/auth/me`, config GET)
2. scenario 주입 (config save / wanted adjustment / daily-shift PUT)
3. `POST /roster_create/generate`
4. payload assertion
5. `tools/harness` 산출물/graph assertion
6. `/ontology/node/run:{run_id}` assertion
7. 원복 및 재검증

---

## 6. Known Caveats

- MUS minimal set 특성상 top-level pattern이 항상 목표 패턴으로 선택되지 않을 수 있음  
  → member/type/derivation 경로까지 함께 검증.
- 동일 원인의 nurse별 반복 노드는 “원인 종류 증가”가 아니라 “영향 범위 확장”일 수 있음.

---

## 7. Next Implementation Step

- `tests/`에 케이스 메타(예: JSON/YAML) 도입 후 parametrized e2e test 작성
- 우선 P0 4케이스 자동화 → PR gate 연결
