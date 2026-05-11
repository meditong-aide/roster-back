# 제약조건 총정리 (Hard/Soft) + Weight 적용 시점

이 문서는 현재 스케줄 엔진에서 사용하는 제약을 다음 관점으로 통합 정리한다.

1. **Hard 제약**: 반드시 만족해야 하는 제약(위반 시 해 불가 또는 사전 차단)
2. **Soft 제약**: 슬랙(완화변수) 또는 패널티/보너스 목적함수로 품질을 유도하는 제약
3. **Weight 적용 시점**: 어떤 단계에서 어떤 weight가 실제 objective에 들어가는지
4. **수식 형태**: CP-SAT 선형 제약/목적식 관점의 표현

---

## 0) 기호 정의 (문서 공통)

- $X_{n,d,s} \in \{0,1\}$: 간호사 $n$이 일자 $d$, 시프트 $s$에 배정되면 1
- $O$: OFF 시프트 인덱스
- $need_{d,s}$: 일자/시프트 필요 인원
- $slack \ge 0$: Soft 완화를 위한 슬랙 변수
- 목적함수는 코드상 `m.Maximize(sum(obj))` 또는 fallback에서 `m.Minimize(...)`

---

## 1) 파이프라인에서 제약/weight가 들어가는 시점

| 단계              | 주요 함수                                                      | 역할                            | Weight 투입 여부                 |
| --------------- | ---------------------------------------------------------- | ----------------------------- | ---------------------------- |
| 요청 오케스트레이션      | `app/services/roster_create_service.py::_run_cp_sat_basic` | 설정/전략 확정, 사전 진단, 엔진 호출        | 간접(설정 전달)                    |
| Precheck        | `precheck/*.py`, preflight alerts                          | 구조적 불가능 케이스 차단/경고             | 없음(검증 전용)                    |
| 설정 정규화          | `app/services/cp_sat_basic.py` (config 생성 경로)              | 기본값 포함 weight 키 확정            | **예 (기본값 주입)**               |
| 메인 모델 빌드        | `cp_sat_basic.py::_build_full_model`                       | hard 제약 + soft objective 항 생성 | **예**                        |
| 메인 objective 조립 | `cp_sat/objective_terms.py::build_main_objective_terms`    | 패널티/보너스 항 생성                  | **예 (핵심)**                   |
| Fallback Stage1 | `cp_sat/fallback_lex.py::build_model(stage=1)`             | 커버리지 부족 우선 최소화                | **예**                        |
| Fallback Stage2 | `cp_sat/fallback_lex.py::build_model(stage=2)`             | safety slack 최소화              | **예**                        |
| Fallback Stage3 | `fallback_lex.py` + `fallback_objectives.py`               | 품질/선호 재최적화                    | **예**                        |
| 사후 수리           | `repairs/grade_repair.py::grade_local_repair`              | 규칙 기반 국소 수리                   | objective weight 거의 없음(휴리스틱) |

---

## 2) Hard 제약 정리 (역할 + 수식)

### 2.1 사전 차단 Hard (Deterministic Precheck)

**소스**: `app/services/precheck/team_grade_precheck.py`

이 레이어의 역할은 “이 입력으로는 애초에 해가 존재 가능한가?”를 모델 빌드 전에 판단하는 것이다.

대표 reason code와 의미:

| Code                                                          | 역할(의미)                         |
| ------------------------------------------------------------- | ------------------------------ |
| `GLOBAL_DAY_CAPACITY_SHORTAGE`                                | 특정 일자의 총 가용 인원이 최소 필요 총량보다 작음  |
| `GLOBAL_SHIFT_ALLOWED_SHORTAGE`                               | 특정 시프트를 수행 가능한 인원 자체가 필요량보다 작음 |
| `CAPACITY_TOTAL_SHORTAGE`                                     | 월 전체 관점에서 총 공급량이 총 수요량보다 작음    |
| `TEAM_MIN_EXCEEDS_GLOBAL_NEED`                                | 팀별 최소 요구 합이 전역 need를 초과        |
| `TEAM_SIZE_INSUFFICIENT` / `TEAM_ACTIVE_MEMBERS_INSUFFICIENT` | 팀 최소치를 만족할 팀 인원이 절대 부족         |
| `TEAM_SHIFT_ALLOWED_SHORTAGE`                                 | 팀 내에서 특정 시프트 가능한 인원 부족         |
| `GRADE_MIN_SUM_EXCEEDS_NEED`                                  | grade 최소 요구 합이 need보다 큼        |
| `GRADE_MAX_SUM_BELOW_NEED`                                    | grade 최대 허용 합으로도 need 도달 불가    |
| `GRADE_MIN_AVAILABLE_SHORTAGE`                                | 특정 grade 최소치 충족 가능한 인원 부족      |
| `GRADE_ANTIPAIR_FORCES_SHORTAGE`                              | grade 교차 제한 때문에 실질 공급 부족       |
| `TEAM_GRADE_INTERSECT_SHORTAGE`                               | 팀 제약과 grade 제약 교차로 공급 부족       |
| `FIXED_ASSIGN_EXCEEDS_NEED`                                   | 고정 배정이 need 초과                 |
| `FIXED_ASSIGN_VIOLATES_ALLOWED`                               | 고정 배정이 개인 allowed shift 위반     |
| `FIXED_ASSIGN_BREAKS_TEAM_MIN`                                | 고정 배정 때문에 팀 최소 구조 붕괴           |
| `FIXED_OFF_EXCEEDS_SPAN`                                      | 고정 OFF가 유효 근무구간 대비 과도          |
| `MONTHLY_NIGHT_CAPACITY_SHORTAGE`                             | 월 야간 필요량 대비 야간 가능한 총공급 부족      |
| `MID_REQUIRED_MISSING` / `MID_DISABLED_BUT_USED`              | M 시프트 요구/설정 불일치                |

> 이 레이어는 objective weight를 쓰지 않는다. “불가능 판정”이 목적이다.

---

### 2.2 솔버 Hard (CP-SAT 내부 강제)

**주 소스**:
- `app/services/cp_sat_basic.py` (`_build_full_model`)
- `app/services/cp_sat/lookahead_constraints.py`
- (기준 참고) `app/services/cp_sat_basic_base.py`

#### (1) 1일 1배정
역할: 한 간호사는 하루에 정확히 1개 시프트만 갖는다.

$$
\sum_s X_{n,d,s} = 1
$$

#### (2) 고정 셀 강제
역할: 사용자가 고정한 셀은 반드시 해당 시프트로 배정.

예) `(n,d)`가 `s*`로 고정이면
$$
X_{n,d,s^*}=1,\quad X_{n,d,s\ne s^*}=0
$$

#### (3) 전이 금지 (예: N→D, E→D)
역할: 위험한 연속 패턴 금지.

$$
X_{n,d-1,N}+X_{n,d,D}\le 1
$$

옵션에 따라
$$
X_{n,d-1,E}+X_{n,d,D}\le 1
$$

#### (4) 연속근무 상한
역할: $K+1$일 창에서 최소 1일 OFF 보장.

$$
\sum_{t=0}^{K} X_{n,d+t,O} \ge 1
$$

#### (5) 월간/연속 야간 상한
역할: 야간 과부하 방지.

월 상한:
$$
\sum_d X_{n,d,N} \le N^{month}_{max}
$$

연속 야간 상한($L$):
$$
\sum_{t=0}^{L} X_{n,d+t,N} \le L
$$

#### (6) 2N→2O, 3N→2O 회복 규칙
역할: 연속 야간 후 회복 OFF 강제.

개념식(블록 종료 시):
$$
X_{n,d+1,O}+X_{n,d+2,O}=2
$$
(`2N` 또는 `3N` 조건 만족 시 enforce)

#### (7) Lookahead OFF cap
역할: 룩어헤드 구간에서 OFF가 수요를 깨지 않도록 상한.

$$
\sum_{n\in selectable(d)} X_{n,d,O} \le selectable\_cap_d
$$

---

### 2.3 조건부 Hard (설정에 따라 Soft 전환)

| 모듈 | Hard 모드 | Soft 모드 |
|---|---|---|
| `constraints/team_constraints.py` | `team_min_soft_fallback=False`이면 $\sum X \ge min$ 하드 | `True`이면 slack + 패널티 |
| `constraints/grade_constraints.py` | `allow_soft_fallback=False`이면 grade min/max 하드 | `True`이면 slack + `grade_penalty_weight` |
| `constraints/team_grade_handoff_constraints.py` | `team_handoff_soft_fallback=False`이면 하드 | `True`이면 slack + `team_handoff_penalty_weight` |

추가: `roster_create_service.py`는 특정 오류(`MAX_CAP_SHORTAGE`, `GRADE_MAX_SUM_BELOW_NEED`) 시 1회 재시도로 `allow_soft_fallback=True`를 강제하는 경로가 있다.

---

## 3) Soft 제약 정리 (역할 + 수식 + weight)

### 3.1 메인 objective (`objective_terms.py`)

| Soft 항목 | 역할 | 수식 형태(개념) | Weight |
|---|---|---|---|
| 커버리지 shortage 패널티 | 인원 부족 억제 | $-w\cdot shortage_{d,s}$ | `FALLBACK_COVERAGE_SHORT_WEIGHT` |
| 선호 점수 보상 | 개인 선호 반영 | $+score_{n,d,s}\cdot X_{n,d,s}$ | `PREFERENCE_SCORE_SCALE` |
| 야간전담 N 보너스 | N-only를 N에 유도 | $+w\cdot X_{n,d,N}$ | `N_ONLY_NIGHT_BONUS` |
| 추가 OFF 기피 | 불필요 OFF 억제 | $-w\cdot X_{n,d,O}$ | `extra_off_penalty_weight` |
| 월간 선호 보상 | 개인 월 선호 시프트 유도 | $+w_n\cdot X_{n,d,s^*}$ | `monthly_preference_weight` |
| 소프트 연속근무 상한 | 긴 연속근무 억제 | $-w\cdot miss_{n,d0}$, $miss\ge 1-\sum OFF$ | `soft_consecutive_work_penalty_weight` |
| 경력자 부족 패널티 | 숙련 인력 최소화 실패 억제 | $-w\cdot expShort_{d,s}$ | `EXPERIENCE_SHORT_PENALTY` |
| 주 2OFF 부족 패널티 | 주간 휴식 확보 | $-w\cdot weekSlack_{n,w}$ | `WEEK_OFF_SHORT_PENALTY` |
| KLD 분배 패널티 | D/E/N 분배 불균형 억제 | 편차 분해 변수를 다단 패널티 | `kld_*_weight` |
| MID 균등 분배 | M 편중 억제 | $-w\cdot(devLow+devHigh)$ | `mid_deviation_penalty_weight` |
| Night min-max 분배 | N 최대치/편차 억제 | $-w_1\cdot maxN - w_2\cdot dev$ | `night_minmax_*_weight` |
| NOD/NOE/EOD 패턴 억제 | 비선호 패턴 억제 | $-w\cdot pat$ | `NOD_NOE_PENALTY` |
| 고립 OFF 패널티 | OFF 고립 방지 | $-w\cdot iso$ | `ISOLATED_OFF_PENALTY` |
| 연속 OFF 보너스 | OFF 묶음 선호 | $+w\cdot seqOff$ | 내부상수 `SEQUENTIAL_OFF_BONUS` |
| 팀 밸런스 보너스 | 팀 내 시프트 정렬 유도 | $+w\cdot z$ (AND 보조변수) | `team_balance_weight` |
| 팀 최소치 soft 완화 | 팀 최소 미달 허용+패널티 | $sumX + slack \ge min$, obj에 $-w\cdot slack$ | `team_min_penalty_weight` |
| Grade soft 완화 | grade min/max 위반 완화 | min/max slack에 $-w\cdot slack$ | `grade_penalty_weight` |
| Team-grade handoff soft | 인계 제한 위반 완화 | 제약식에 slack 삽입 후 $-w\cdot slack$ | `team_handoff_penalty_weight` |
| shortage 우선순위 가중 | 시프트별 shortage 중요도 차등 | $-w_s\cdot shortage_{d,s}$ | `shift_requirement_priority` |
| oversupply 균등화 | 과잉배정의 시프트별 쏠림 완화 | $-w\cdot |ov_1-ov_2|$ | `oversupply_equalize_weight` |
| 3N 블록 선호(2N 억제) | 2N 블록 줄이고 3N 유도 | $-w\cdot is2N$ | `PREFER_3N_BLOCK_PENALTY` |
| per-nurse target 편차 | 개인별 목표량 정렬 | $-w\cdot |count-target|$ | `per_nurse_target_weight` |

추가(룩어헤드):

| 항목 | 역할 | 수식 | Weight |
|---|---|---|---|
| Lookahead OFF 분산 패널티 | 미래 OFF 쏠림 완화 | $-w\cdot lookaheadMaxOff$ | `lookahead_distribution_weight` |

---

## 4) Fallback의 Soft/Weight 동작 (핵심)

### Stage 1: 커버리지 최우선 최소화

소스: `cp_sat/fallback_lex.py`

목적식(개념):
$$
\min\; W_{short}\sum short + \sum over + W_{off}\sum off
$$

- $W_{short} =$ `FALLBACK_COVERAGE_SHORT_WEIGHT`
- $W_{off} =$ 내부 상수 `OFF_PENALTY=30`
- 관련 제어키: `soften_daily_coverage`, `coverage_soft_slack`, `coverage_soft_penalty_weight`

### Stage 2: safety slack 최소화

$$
\min\; \sum safety\_slacks
$$

중요 가중 슬랙:
- `isolated_off_slack_penalty` (기본 300000)
- `fallback_off_cap_bounded_slack_weight` (기본 10)
- 경로에 따라 OFF cap slack 내부가중(예: relax level 기반 가중)

### Stage 3: 품질 재최적화

소스: `cp_sat/fallback_objectives.py` + `fallback_lex.py`

역할: Stage1/2 결과를 고정/준고정한 상태에서 선호·분배·공정성 회복.

추가적으로 day smoothing 내부 패널티 사용:
- `-80 * excess`
- `-150 * range`
- `-60 * adjacent_diff`

---

## 5) Weight 기본값(자주 보는 키)

### 5.1 Config 기본값 주입(주로 `cp_sat_basic.py`)

- `extra_off_penalty_weight = 80`
- `soft_consecutive_work_penalty_weight = 180`
- `monthly_preference_weight = 60`
- `oversupply_equalize_weight = 120`
- `team_min_penalty_weight = 500`
- `team_handoff_penalty_weight = 80000`
- `coverage_soft_penalty_weight = 120000` (fallback 경로)
- `isolated_off_slack_penalty = 300000` (fallback stage2)
- `fallback_off_cap_bounded_slack_weight = 10`
- shadow coverage:
  - `shadow_coverage_lookback_days = 6`
  - `shadow_coverage_need_ratio = 0.6`
  - `shadow_coverage_penalty_weight = 6`

### 5.2 하드코딩 상수 (`cp_sat/hardcoded_weights.py`)

- `PREFERENCE_SCORE_SCALE = 100`
- `N_ONLY_NIGHT_BONUS = 500`
- `FALLBACK_COVERAGE_SHORT_WEIGHT = 100000`
- `FALLBACK_EXPERIENCE_SHORT_PENALTY = 100`
- `EXPERIENCE_SHORT_PENALTY = 200`
- `WEEK_OFF_SHORT_PENALTY = 300`
- `NIGHT_DEVIATION_PENALTY = 5000`
- `NOD_NOE_PENALTY = 300`
- `ISOLATED_OFF_PENALTY = 100`
- `PREFER_3N_BLOCK_PENALTY = 80`

---

## 6) 실무 해석 가이드

1. **Precheck hard**는 “애초에 불가능”을 차단한다 (weight 없음).
2. **Solver hard**는 모델이 반드시 지키는 제약이다.
3. **조건부 hard**(team/grade/handoff)는 설정값에 따라 soft로 완화될 수 있다.
4. 생성 실패 시 fallback에서 objective 우선순위가 바뀌며, stage별로 weight 의미가 달라진다.
5. 따라서 튜닝은 반드시 “메인 objective”와 “fallback stage objective”를 분리해서 봐야 한다.

---

## 7) 하드 제약 누락 점검 결과 (추가 반영)

아래 항목들은 기존 요약에서 빠지기 쉬운 하드/준하드 경계 항목으로, 코드 기준으로 추가 점검했다.

### 7.1 문서에 반드시 포함해야 하는 하드 체크(추가)

1. **`weekend_off_only` + 최소 OFF 충돌**
   - 근거: `app/services/cp_sat/feasibility_alerts.py`
   - 의미: 주말휴무자에게 요구되는 최소 OFF(`min_off_required`)가 실제 주말 슬롯 수보다 크면 불가능.

2. **M 시프트의 preceptee unit subset-sum 불가능**
   - 근거: `app/services/cp_sat/feasibility_alerts.py`
   - 의미: 프리셉터-프리셉티 묶음 단위 크기 합으로 남은 M 수요를 정확히 만들 수 없으면 불가능.

3. **강제 non-vacation OFF가 개인 max OFF 상한 초과**
   - 근거: `app/services/cp_sat/feasibility_alerts.py`
   - 의미: 구조적 OFF + 강제 OFF + 정책성 OFF 합이 개인 최대 OFF 허용량을 넘으면 불가능.

4. **M 하드 가능범위 체크(고정/금지 반영)**
   - 근거: `app/services/cp_sat/mid_feasibility.py`
   - 의미: 일자별 M 요구치가 `[min_possible, max_possible]` 범위를 벗어나면 즉시 불가능.

5. **저장 시점 팀 min 단순 모순 차단**
   - 근거: `app/services/precheck/team_min_shift_capacity_validator.py`
   - 의미:
     - `TEAM_EMPTY_BUT_MIN_SET`
     - `TEAM_ACTIVE_LT_DAY_MIN_SUM`

6. **고정값으로 전이금지 예외/충돌 경계**
   - 근거: `app/services/cp_sat_basic.py`
   - 의미: 일부 전이금지(N→D/E→D/N→E)는 fixed로 명시된 경우 면제 경로가 있어, 설정/DB 고정 상태에 따라 충돌 양상이 달라짐.

---

## 8) 하드 충돌 가능 상황 전체 리스트 (DB 상태/서비스 로직 포함)

아래는 운영 중 실제로 많이 발생하는 충돌을 **중심 기능별**로 묶은 체크리스트다.

### 8.1 중심 기능 A: `Team min`을 설정했을 때

#### A-1) 팀 인원 구조 자체 모순
- 상황: 팀에 배정된 인원 수가 0인데 `team_min_by_team`이 양수
- 결과: `TEAM_EMPTY_BUT_MIN_SET` 또는 `TEAM_SIZE_INSUFFICIENT`
- DB 상태 원인 예시:
  - 팀 생성 후 인력 배정 누락
  - 팀 이동 배치 반영 전 저장

#### A-2) 팀 인원수 < 일일 팀 최소합
- 상황: 팀원 모두가 매일 일해도 D/E/N(/M) 최소합을 못 채움
- 결과: `TEAM_ACTIVE_LT_DAY_MIN_SUM`, `TEAM_MIN_EXCEEDS_GLOBAL_NEED`
- DB 상태 원인 예시:
  - 휴직/퇴사 예정 인원이 팀에 포함되어 실근무 가능자 과대평가
  - role `etc` 제외 정책으로 실가용 인원 급감

#### A-3) 팀 내부 allowed shift 부족
- 상황: 팀원은 있는데 특정 시프트 가능한 팀원이 부족
- 결과: `TEAM_SHIFT_ALLOWED_SHORTAGE`
- DB 상태 원인 예시:
  - 야간전담/N금지 프로필 집중
  - 개인 forbidden/initial constraints 누적

#### A-4) 팀 min vs 고정배정(fixed/fixed_wanted) 충돌
- 상황: 고정근무가 팀 최소 구조를 깨거나 회복 OFF 슬롯을 점유
- 결과: `FIXED_ASSIGN_BREAKS_TEAM_MIN`, solver infeasible
- DB 상태 원인 예시:
  - 원티드 확정으로 특정일 팀원 다수가 동일 시프트/비OFF로 고정
  - 회복 OFF가 필요한 날짜에 non-OFF 고정값 존재

#### A-5) 팀 min vs 휴가/공가/강제OFF 충돌
- 상황: 팀 내 다수가 동일일 휴가/공가/forced_off
- 결과: 팀 최소치 미충족(사전 또는 solver 단계)
- DB 상태 원인 예시:
  - 휴가 승인 일괄 반영
  - 근무 외 고정(교육/출장성 OFF) 동시 다발

#### A-6) 팀 min vs 입퇴사 구간(join/leave) 충돌
- 상황: 월초/월말 특정 구간에서 active member 급감
- 결과: 해당 기간 팀 최소치 불가능
- DB 상태 원인 예시:
  - 중도 입사/퇴사 인력 다수
  - 월경계 오프윈도우와 겹침

---

### 8.2 중심 기능 B: `Grade 제약(min/max)`을 켰을 때

#### B-1) grade min 총합이 need 초과
- 결과: `GRADE_MIN_SUM_EXCEEDS_NEED`
- 전형 원인: 각 grade 최소치가 과도하게 큼

#### B-2) grade max 총합으로도 need 미달
- 결과: `GRADE_MAX_SUM_BELOW_NEED`, 런타임 `MAX_CAP_SHORTAGE`
- 전형 원인: 고grade cap이 너무 낮거나 저grade만 다수

#### B-3) grade 최소치 충족 인원 부족
- 결과: `GRADE_MIN_AVAILABLE_SHORTAGE`
- DB 상태 원인:
  - 해당 grade 인력 휴가 집중
  - 입퇴사/block으로 해당일 가용성 축소

#### B-4) 팀 min과 grade max 교차충돌
- 결과: `TEAM_GRADE_INTERSECT_SHORTAGE`
- 의미: 팀 단위 최소치와 grade 상한을 동시에 만족할 교집합 인력이 없음

#### B-5) 고정배정이 grade 구조를 잠가버림
- 결과: `FIXED_ASSIGN_EXCEEDS_NEED` 또는 grade shortage
- DB 상태 원인:
  - 원티드/수동고정이 특정 grade에 편중

---

### 8.3 중심 기능 C: `M(use_mid)` / preceptee 연동

#### C-1) M 사용 비활성인데 M 키 사용
- 결과: `MID_DISABLED_BUT_USED`

#### C-2) M 요구치가 일별 가능한 범위 초과
- 결과: `M 하드 제약 불가능` (mid feasibility)
- 수식 관점: $req_M(d) > fixed_M(d)+variable\_capacity_M(d)$

#### C-3) preceptee unit 조합 불가능(subset-sum 실패)
- 결과: preflight alert
- DB 상태 원인:
  - 프리셉터-프리셉티 묶음 크기가 수요와 맞지 않음
  - unit 일부가 fixed/forbidden/forced_off로 비활성

#### C-4) M 고정값이 다른 hard 규칙과 상충
- 결과: 전이/회복/팀 min과 결합 시 infeasible 가능

---

### 8.4 중심 기능 D: 야간/회복 규칙(2N2O, 3N2O, N 관련 금지)

#### D-1) N 수요를 채우면 회복 OFF가 과도하게 필요
- 결과: OFF cap/근무충족과 충돌
- 전형 패턴: `2N→2O`, `3N→2O` + 높은 N need 동시 활성

#### D-2) 회복 OFF 슬롯에 fixed_wanted non-OFF 존재
- 결과: 해당 N 블록 자체 금지 또는 infeasible
- 근거: `cp_sat_basic.py`의 recovery 슬롯 검사 로직

#### D-3) 월간 N cap 부족
- 결과: `MONTHLY_NIGHT_CAPACITY_SHORTAGE`
- 전형 원인: N 가능 인력 적고 수요가 큼

#### D-4) N 금지 프로필/초기금지 누적으로 N 실가용 급감
- 결과: 전역/시프트 capacity shortage

---

### 8.5 중심 기능 E: OFF 정책(주말휴무/최소OFF/최대OFF)

#### E-1) weekend_only + min_off_required > weekend_slots
- 결과: preflight hard alert

#### E-2) forced_nonvac_off > max_off_allowed
- 결과: preflight hard alert
- DB 상태 원인:
  - 휴가 제외 OFF가 과도
  - 주말휴무자 정책 + 구조적 OFF + forced_off 중첩

#### E-3) off_window_constraints와 고정근무 충돌
- 결과: 특정 구간 OFF 확보 실패로 infeasible 위험

---

### 8.6 중심 기능 F: fixed/fixed_wanted/원티드 확정

#### F-1) 고정값이 need 초과
- 결과: `FIXED_ASSIGN_EXCEEDS_NEED`

#### F-2) 고정값이 개인 allowed shift 위반
- 결과: `FIXED_ASSIGN_VIOLATES_ALLOWED`

#### F-3) 고정 OFF가 span 대비 과다
- 결과: `FIXED_OFF_EXCEEDS_SPAN`

#### F-4) 고정값이 전이/회복/팀min/grade를 동시에 잠금
- 결과: 복합 infeasible
- DB 상태 원인:
  - 원티드 승인/일괄확정 이후 수동고정 누적

---

### 8.7 중심 기능 G: 전역 수요-공급 충돌

#### G-1) 일별 총공급 부족
- 결과: `GLOBAL_DAY_CAPACITY_SHORTAGE`

#### G-2) 시프트별 공급 부족
- 결과: `GLOBAL_SHIFT_ALLOWED_SHORTAGE`

#### G-3) 월 총량 부족
- 결과: `CAPACITY_TOTAL_SHORTAGE`

원인군(대부분 DB 상태 기반):
- 휴가/공가 집중
- 동일일 forced_off 다수
- 입퇴사 경계로 active day 감소
- 팀/grade/profile 제약으로 실질 가용 인력 축소

---

### 8.8 충돌을 빨리 찾는 운영 체크 순서(권장)

1. **고정값 충돌 먼저**: fixed/fixed_wanted, 원티드 확정, 휴가/공가 반영 상태
2. **전역 수요-공급**: day/shift/month capacity
3. **Team min 단독 모순**: 팀 인원/팀 최소합/팀별 allowed
4. **Grade min/max 교차**: team-grade intersect 포함
5. **M/preceptee 단위 조합**: subset-sum, fixed M 경계
6. **야간 회복 규칙**: 2N2O/3N2O와 OFF cap 동시 점검

이 순서를 따르면, 실제로는 soft weight 조정 전에 하드 불가능 원인을 더 빠르게 분리할 수 있다.
