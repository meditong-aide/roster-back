# Ontology Rule Granularity Spec (v1)

> 목적: 체크리스트 35개 규칙별로 evidence가 어느 단위까지 내려갈 수 있는지(scope)와 필수 필드를 확정한다.
>
> 위치: ontology/hypergraph 로드맵 3단계 중 **(1) Granularity** 단계의 산출물.
> 후속 단계인 causal_group_id 추가와 스키마 강제(governance seal)는 이 분류표가 안정된 뒤 진행.
>
> 선행 문서:
> - `docs/HARNESS_TO_ONTOLOGY_HYPERGRAPH_MAPPING.md`
> - `docs/CONSTRAINT_IMPACT_GRAPH_ACTIVE_BLUEPRINT.md`
> - `docs/ONTOLOGY_GROUNDED_CONSTRAINT_IMPACT_GRAPH_PLAN.md`
> - 룰 정의: `tools/harness/rules/checklist_core.yaml`

---

## 1. 결정 요약

본 spec은 다음 6개 결정에 따라 작성됨:

1. **D_*_MIN 등 cell-level 룰**: 1차 evidence는 cell만. nurse contributor는 별도 `enrichment.contributors[]`로 분리 (false attribution 방지).
2. **C_* 공정성 룰**: aggregate metric에 per-nurse delta 동봉.
3. **연속/회복 윈도우 룰**: `[start_day, end_day]` 전체 윈도우 기록.
4. **E_WANTED_APPLY**: per-wanted-entry 분해. status enum에 `head_rejected` 포함하여 수간호사 반려를 분모에서 제외.
5. **G_GRADE_CONSTRAINT**: grade-shortage 표현(`shortage_grade`) 사용. nurse contributor는 enrichment로 분리.
6. **scope 표기**: enum 방식 (조합 폭발 회피 / 검증 단순화).

---

## 2. Scope Enum

evidence가 어느 차원까지 결정 가능한지를 enum 1개로 표현한다.

| scope | required entity dims | 의미 |
|---|---|---|
| `fine` | nurse, day, shift | 위반 셀 단위. 가장 잘게 식별. |
| `nurse_window` | nurse, [start_day, end_day] | 연속/회복/주휴 등 nurse 시간축 구간. |
| `nurse_month` | nurse, year, month | 월간 누적 카운트(N cap, OFF days 등). |
| `cell` | day, shift | nurse 없는 커버리지 셀. |
| `cell_grade` | day, shift, grade | grade 분포 위반. |
| `pair` | preceptor_id, preceptee_id | 프리셉티 페어 산포. |
| `pair_day` | preceptor_id, preceptee_id, day, shift | 페어 동반 셀 단위. |
| `per_nurse` | nurse | day 없는 nurse 속성(role NULL 등). |
| `per_entry` | entry_id 기반 (nurse+day+requested_shift) | 원티드/매핑 같은 entry 단위. |
| `run` | run_id | 런 전체 aggregate. |
| `config` | run_id, config_key | 설정 정합성 위반. |

신규 차원 조합 필요 시 enum 값을 추가한다 (예: `nurse_week`).

---

## 3. Rule Classification Table

모든 룰은 `scope` 1개와 `required_evidence_fields` 집합을 가진다.

### Group A — Hard constraints

| rule_id | scope | required_evidence_fields |
|---|---|---|
| A_1N_SINGLE | fine | nurse_id, day, shift (N) |
| A_NOD | fine | nurse_id, day, shift_triplet[day-1, day, day+1] |
| A_NOE | fine | nurse_id, day, shift_triplet |
| A_EOD | fine | nurse_id, day, shift_triplet |
| A_2N_2OFF | nurse_window | nurse_id, start_day, end_day, block_pattern(=`NN`), recovery_window |
| A_3N_2OFF | nurse_window | nurse_id, start_day, end_day, block_pattern(=`NNN`), recovery_window |
| A_4N_MAX | nurse_window | nurse_id, start_day, end_day, run_length |
| A_MAX_CONSEQ_WORK | nurse_window | nurse_id, start_day, end_day, run_length, configured_max |
| A_MONTHLY_N_CAP | nurse_month | nurse_id, year, month, count, configured_cap |

### Group B — OFF / 휴무

| rule_id | scope | required_evidence_fields |
|---|---|---|
| B_OFF_NEAR_CONFIG | nurse_month | nurse_id, year, month, off_count, target_off, deviation |
| B_OFF_CAP_EXACT | nurse_month | nurse_id, year, month, off_count, expected_cap, exclusion_reason? |
| B_WEEKLY_OFF | nurse_window | nurse_id, week_start_day, week_end_day, weekly_off_missing |
| B_OFF_SWAP_RECOVERY | fine | nurse_id, day, original_shift(=O recovery), converted_shift |
| B_OFF_SWAP_N_ONLY | fine | nurse_id, day, original_shift, converted_shift |
| B_OFF_SWAP_FIXED | fine | nurse_id, day, original_shift, converted_shift, fixed_source |
| B_OFF_SWAP_JU | fine | nurse_id, day, original_shift, converted_shift |
| B_OFF_SWAP_TARGET_SINGLE | config | run_id, config_key(`target_shift`), offending_value |

### Group C — Fairness (per-nurse delta 동봉)

| rule_id | scope | required_evidence_fields |
|---|---|---|
| C_DEN_BALANCE | run | run_id, spread_ratio, per_nurse_delta[{nurse_id, D, E, N, deviation_from_median}] |
| C_TOTAL_BALANCE | run | run_id, total_work_spread, per_nurse_delta[{nurse_id, total_work_days, deviation_from_median}] |
| C_N_SKEW | run | run_id, n_skew_ratio, per_nurse_delta[{nurse_id, n_count, deviation_from_median}] |

> 참고: C_*는 metric 자체는 aggregate이지만, "누가 쏠렸는지" 답을 줄 수 있도록 per-nurse delta를 evidence에 함께 박는다.

### Group D — Coverage (cell만, contributor는 enrichment)

| rule_id | scope | required_evidence_fields |
|---|---|---|
| D_D_MIN | cell | day, shift(=D), need, assigned, shortage |
| D_E_MIN | cell | day, shift(=E), need, assigned, shortage |
| D_N_MIN | cell | day, shift(=N), need, assigned, shortage |
| D_M_MIN | cell | day, shift(=M), need, assigned, shortage |
| D_MAX_OVER | cell | day, shift, max, assigned, overflow |
| D_MAX_ENABLED_INTEGRITY | config | run_id, config_key(`max_enabled`), inconsistency_detail |

### Group E — Wanted / Fixed

| rule_id | scope | required_evidence_fields |
|---|---|---|
| E_WANTED_APPLY | per_entry | wanted_entries[{entry_id, nurse_id, day, requested_shift, status, reason_code?}] |
| E_FIXED_LOCK | fine | nurse_id, day, original_fixed_shift, observed_shift, fixed_source |
| E_BAN_N_BEFORE_FIXED_OFF | fine | nurse_id, day(=fixed_off_day-1), shift(=N), fixed_off_day |

#### E_WANTED_APPLY status enum

| status | 분모 포함? | 의미 |
|---|---|---|
| `approved_applied` | yes | 수간호사 승인 + solver 반영 (numerator) |
| `approved_unapplied` | yes | 수간호사 승인 + solver 미반영 (denominator only, ratio 깎임) |
| `head_rejected` | **no** | 수간호사 반려 — ratio 계산에서 제외 |

ratio 계산식:

```
applied = count(status == approved_applied)
denominator = count(status in {approved_applied, approved_unapplied})
apply_ratio = applied / denominator
```

이 결과로 `>= 0.90` 임계가 "수간호사가 승인한 원티드 중 90% 이상은 실제 반영"으로 정확히 해석된다.

#### Status derivation (현재 스키마 기준)

`FixedWantedEntry` (`app/db/models.py:716`)에는 명시적 `status` 컬럼이 없으므로, 다음 휴리스틱으로 derive한다:

| is_applied | head_nurse_memo | derived status |
|---|---|---|
| True | (any) | `approved_applied` |
| False | NOT NULL | `head_rejected` |
| False | NULL | `approved_unapplied` |

**근거**: 결정 요인은 memo 내용이 아니라 "수간호사 개입의 흔적"(`is_applied=False AND memo 존재`)이다. memo가 반려 사유든 조정 코멘트든 의미적으로 동일하게 `head_rejected`로 본다.

#### source_type 동봉

evidence에는 `source_type` (`original | added | modified`)을 함께 박는다. 이유:
- `modified` + `approved_applied`: nurse 원본은 변형됐지만 entry 단위로는 적용된 케이스. evidence를 읽는 도구가 "원본이 어떻게 바뀌었는지" 추적 가능.
- `original_shift_id`도 함께 노출하여 변형 이력을 보존.

#### 향후 강화 (out-of-scope for v1)

휴리스틱 정확도가 부족하다고 판단되면 `FixedWantedEntry`에 명시적 `rejection_type: VARCHAR` (`head | solver | null`) 컬럼을 추가하여 (C)안으로 전환. governance seal 단계에서 재검토.

### Group F — Carryover (전월 연계)

| rule_id | scope | required_evidence_fields |
|---|---|---|
| F_PREV_TRANSITION | fine | nurse_id, day(=boundary day), prev_shift, curr_shift, transition_code |
| F_PREV_CONSEQ_WORK | nurse_window | nurse_id, start_day, end_day(boundary 포함), run_length, configured_max |
| F_PREV_N_RECOVERY | fine | nurse_id, day, expected_off, observed_shift |
| F_DROPPED_FILTER | config | run_id, dropped_ref_count, offending_records |

### Group G — 특수 케이스

| rule_id | scope | required_evidence_fields |
|---|---|---|
| G_PRECEPTEE_SYNC | pair_day | preceptor_id, preceptee_id, day, preceptor_shift, preceptee_shift, mismatch_flag |
| G_PRECEPTEE_PAIR_SPREAD | pair | preceptor_id, preceptee_id, concentration_ratio, per_shift_counts |
| G_PRECEPTEE_MAPPING | per_entry | mapping_entries[{entry_id, preceptor_id, preceptee_id, invalid_reason}] |
| G_ROLE_NULL | per_nurse | nurse_id, active_in_period |
| G_GRADE_CONSTRAINT | cell_grade | day, shift, grade_min, grade_max, grade_assigned_breakdown, shortage_grade[], overflow_grade[] |

### Group H — 시스템 안정성

| rule_id | scope | required_evidence_fields |
|---|---|---|
| H_NO_INFEASIBLE | run | run_id, solver_status, reason_codes[] |
| H_FALLBACK_OK | run | run_id, fallback_error_count, error_details[] |
| H_RUNTIME | run | run_id, solve_time_ms_p95, attempt_count |
| H_OFF_SWAP_LOG | run | run_id, offswap_trace_missing_count |

---

## 4. Enrichment Layer (Optional)

특정 scope의 evidence에는 옵션 enrichment 블록이 부착될 수 있다. 1차 evidence를 절대 바꾸지 않으며, 분석 도구만 활용한다.

### 4.1 Coverage cell enrichment

```json
{
  "evidence": {"day": 24, "shift": "E", "need": 3, "assigned": 1, "shortage": 2},
  "enrichment": {
    "contributors": [
      {"nurse_id": "n12", "reason_codes": ["OFF_WINDOW"], "detail": {...}},
      {"nurse_id": "n34", "reason_codes": ["FIXED_WANTED_OTHER_SHIFT", "OFF_WINDOW"], "detail": {...}},
      {"nurse_id": "n56", "reason_codes": ["INACTIVE_DAY"], "detail": {...}}
    ]
  }
}
```

**금지 규칙**:
- enrichment.contributors는 "이 nurse가 미달의 원인" 이라고 해석하지 않는다.
- 표현/문구는 "이날 이 shift에 *가용하지 않았던* nurse들과 그 이유" 로 고정.
- false attribution 방지를 위해 agent/UI는 enrichment를 보여줄 때 반드시 위 규약을 따른다.

### 4.3 reason_codes Enum

`enrichment.contributors[].reason_codes`는 **list** — 한 nurse에 여러 사유가 동시 적용되면 모두 기록한다(정보 손실 방지). UPPER_SNAKE_CASE는 기존 precheck reason_code 컨벤션과 정렬.

#### v1 범위 (cheap derive — 즉시 채움)

**A. 시간축 사전 제외 (cell-level pre-determined)** — `cp_sat_basic.py`의 기존 셀 집합에서 직접 derive

| code | source |
|---|---|
| `OFF_WINDOW` | `off_window_constraints` |
| `FORCED_OFF` | `forced_off` (법규/전월 꼬리 등) |
| `WEEKLY_OFF_REQUIRED` | 주휴 의무일 |
| `COVERAGE_EXCLUDED` | `coverage_exclude_cells` (파견 등) |
| `INITIAL_FORBIDDEN` | `initial_forbidden` 셀 |
| `INACTIVE_DAY` | join/leave 외 활성 아님 |

**D. Fixed/Wanted override** — `FixedWantedEntry` + special_fixed 직접 조회

| code | source |
|---|---|
| `FIXED_WANTED_OTHER_SHIFT` | 그날 fixed_wanted가 다른 shift |
| `FIXED_OTHER_SHIFT` | special_fixed/manual fixed가 다른 shift |

**E. Coupling**

| code | source |
|---|---|
| `PRECEPTEE_COUPLED` | preceptor의 다른 shift에 coupling됨 |

#### v1 out-of-scope (deferred — Open Item #4 후 합류)

다음 카테고리는 nurse_state_machine event log(§8.1) 및 coverage 그래프 derivation이 안정된 뒤 합류한다.

**B. State-derived 차단** — `TRANSITION_BAN`, `CONSECUTIVE_WORK_BLOCK`, `NIGHT_RECOVERY_BLOCK`, `MONTHLY_N_CAP_REACHED`, `WEEKEND_ONLY_BLOCK`

**C. 커버리지 그래프 차단 (다른 셀 깨짐 위험)** — `TEAM_MIN_BLOCKED`, `GRADE_MIN_BLOCKED`, `GRADE_MAX_BLOCKED`

#### 미분류 케이스

위 v1 코드로 어느 것에도 해당하지 않는 경우 nurse를 `enrichment.contributors`에서 **생략**한다. 강제로 `UNKNOWN` 같은 코드를 박지 않는다(false signal 방지). 누락이 분석상 문제 되면 B/C 단계 합류 시점에 채워진다.

### 4.2 Grade shortage enrichment

```json
{
  "evidence": {"day": 24, "shift": "E", "grade_min": {"g1": 1},
               "grade_assigned": {"g1": 0, "g2": 2}, "shortage_grade": ["g1"]},
  "enrichment": {
    "contributors": [
      {"nurse_id": "n78", "grade": "g1", "reason_codes": ["OFF_WINDOW"]}
    ]
  }
}
```

---

## 5. Derived / Aggregate Fields

evidence에서 직접 계산 가능한 값은 별도 저장하지 않고 derived view로 둔다.

| derived | source |
|---|---|
| `wanted.apply_ratio` | E_WANTED_APPLY의 status 분포 |
| `fairness.den_spread_ratio` | C_DEN_BALANCE의 per_nurse_delta |
| `coverage.under_count_*` | D_*_MIN의 shortage 합계 |

`metric` 필드는 dashboard/quick-glance 용으로 유지되지만, 진실 소스는 per-entry/per-cell evidence다.

---

## 6. Common Envelope

모든 violation evidence는 공통 envelope을 따른다.

```json
{
  "rule_id": "D_D_MIN",
  "scope": "cell",
  "run_id": "...",
  "evidence": { /* scope별 required fields */ },
  "enrichment": { /* optional */ },
  "ontology": {
    "constraint_id": "CoverageMin",
    "group": "CoverageConstraint",
    "mode": "enforced"
  }
}
```

- `scope` 필드는 enum 값을 그대로 노출(검증/필터링 용도).
- `ontology` 블록은 기존 attach 규약(`ONTOLOGY_GROUNDED_CONSTRAINT_IMPACT_GRAPH_PLAN.md §6.3`) 그대로 따른다.

---

## 7. 다음 단계 (out-of-scope for v1)

본 spec이 정착한 뒤 후속 단계로 다음을 다룬다:

1. **causal_group_id 도입** — 동일 위반에 기여한 원인 집합을 묶는 additive 메타. 본 v1의 evidence shape를 깨지 않고 추가 가능.
2. **Schema enforcement (governance seal)** — rule별 required_evidence_fields를 강제. 누락 시 FAIL 또는 strict warning. v1의 분류표가 reference schema가 됨.

---

## 8. Window Source (nurse_window 룰)

`nurse_window` scope 룰의 `[start_day, end_day]` 추출 ownership은 다음과 같이 분기한다.

| rule_id | window source | rationale |
|---|---|---|
| A_2N_2OFF | NurseStateMachine event log | n_run_start/end + recovery_required event |
| A_3N_2OFF | NurseStateMachine event log | 동일 패턴 (NNN) |
| A_4N_MAX | NurseStateMachine event log | n_run_overflow event 시 run boundary |
| A_MAX_CONSEQ_WORK | NurseStateMachine event log | work_run_overflow event 시 run boundary |
| B_WEEKLY_OFF | calendar detector | state machine 무관, site week 정의(월~일/일~토/ISO)는 컨벤션으로 고정 |
| F_PREV_CONSEQ_WORK | NurseStateMachine + carryover artifact | cross-month boundary는 `inbound/transfer carryover artifact` 초기 상태 주입(`CONSTRAINT_IMPACT_GRAPH_ACTIVE_BLUEPRINT.md §2.3`)으로 자연 처리. day 인덱스 표기는 §11 참조 |

### 8.1 NurseStateMachine event log 확장

`app/services/constraint_impact/nurse_state_machine.py`에 다음 event 타입을 추가한다.

```python
@dataclass
class NurseTimelineEvent:
    nurse_index: int
    event_type: str
    # n_run_start | n_run_end | n_run_overflow
    # work_run_start | work_run_overflow
    # recovery_required | recovery_satisfied | recovery_violated
    day_index: int
    metadata: dict  # run_length, recovery_window, etc.
```

window detector는 이 event log를 후처리로 스캔하여 `[start_day, end_day]`를 산출한다.

### 8.2 침습성 원칙

- `cp_sat_basic.py`는 건드리지 않는다. constraint application point에 attach 코드를 박지 않는다.
- 의미 정렬은 parity test로 검증한다(아래 §8.3).

### 8.3 Parity Test

각 nurse_window 룰별로:
1. evidence의 `[start_day, end_day]` 추출
2. 해당 구간의 assignment를 독립 detector로 재검사
3. 동일 위반이 재현되어야 함
4. mismatch 시 detector 또는 state machine 버그로 분류

---

## 9. Open Items (remaining)

- [x] ~~각 `nurse_window` 룰의 window 정의~~ → §8에서 확정
- [x] ~~`head_rejected` 상태 노출 경로~~ → §3 Group E의 status derivation 휴리스틱으로 확정
- [x] ~~`enrichment.contributors`의 `reason_code` enum~~ → §4.3에서 v1 enum(A/D/E 카테고리 11개) 확정, B/C는 deferred
- [ ] cross-month boundary가 포함된 `F_PREV_*` 룰의 `day` 인덱스 표기 규약 (음수 day vs 별도 `prev_month_day` 필드).
