# Ontology Hypergraph Design — Causal Diagnosis of CP-SAT Infeasibility

**Date:** 2026-05-15
**Status:** Design (no code change yet)
**Replaces / supersedes (after implementation):** flat `REASON_CODE_TO_CONSTRAINT` mapping in `app/services/semantics/ontology_attach.py`, ad-hoc reason-code labels in `roster_create_service.py` (`DAY_ZERO_COVERAGE`, `NO_ASSIGNMENT` fallback path).

**Companion documents:**
- `/tmp/ontology_audit_track_a.md` — full audit of current pipeline (facts)
- `/tmp/ontology_audit_track_b.md` — literature survey (29 references)

---

## 0. 한 줄 요약

현재 `reason_code → constraint_family` 평면 매핑을, **(cause set) ──treatment──▶ (symptom set)** + **(treatment) ──verified-by──▶ (evidence)** 를 표현하는 **directed hypergraph** 로 교체한다. cause 와 symptom 을 어휘 차원에서 분리하고, treatment 는 단일/번들 모두 1급 노드로 두며, treatment 적용 후 re-solve 결과(witness solution + violation delta)를 evidence 노드로 첨부해 "정말 풀렸는지" 를 유저가 직접 확인할 수 있게 한다.

---

## 1. Why — 현 구조의 결함 (Track A 요약)

1. **증상-원인 동어반복** : `DAY_ZERO_COVERAGE → CoverageMin`, `NO_ASSIGNMENT → CoverageMin` 처럼 "커버리지 부족이라 커버리지 제약 위반". 진짜 원인(grade_max 과도, night cap 부족, fixed 과다 등)이 가려짐.
2. **오분류 2건 확정** : `N_CAPACITY_SHORTAGE → BoundaryTransitionBan`, `MONTHLY_NIGHT_CAPACITY_SHORTAGE → NightRecovery`. 야간 용량은 전이금지/회복과 무관.
3. **NO_ASSIGNMENT 4축 분해 코드 외톨이** : `_infer_no_assignment_direct_reasons()` 가 `NO_ASSIGNMENT_{CAPACITY|ELIGIBILITY|FIXED|CARRYOVER}` 로 분해하는데, 이 4 개가 `REASON_CODE_TO_CONSTRAINT` 와 `ontology.yaml` 어디에도 없음. 진단은 풍부한데 ontology 가 못 받음.
4. **하나의 cause, 여러 reason_code 산재** : `grade_max 빡빡` 이 `GRADE_MAX_SUM_BELOW_NEED / GRADE_ANTIPAIR_FORCES_SHORTAGE / GRADE_HARD_PROBE / MAX_CAP_SHORTAGE / TEAM_GRADE_INTERSECT_SHORTAGE` 로 흩어짐.
5. **Treatment 미정의 family** : `OffCap, AllowedShiftMask, NotOneNight, PrecepteeSync, CarryoverBoundary, FixedWanted` 등 6 개에 `constraint_impact/control.py` action 없음. fix_plan axis 만 hint 로 존재.
6. **Treatment bundle 부재** : `TEAM_MIN_VS_GRADE_MAX_INTERSECTION` 같은 시나리오는 두 제약 동시 완화가 필요한데 묶음 단위 표현 없음.
7. **Verified-resolution 부재** : `_force_grade_max_soft_fallback=True` retry 후 성공 여부는 코드에만 있고 ontology 노드 없음. 유저에게 "정말 풀렸다" 를 보여줄 객체 없음.
8. **병렬 분류 4 개 통합 안 됨** : `causal_layer` (policy/data/personal/structural), `failure_stage` (S0–S4), `structural_diagnosis.primary_causes`, `tier` (T0–T3) 가 각자 따로 놀고 있음.
9. **Evidence-rich but ontology-blind** : ConflictCore 의 `derivation[]` 단계별 산술 증명, `resolution_hints[]`, `pool_snapshot.shortages` 가 모두 runtime dict 에 갇혀 있고 ontology 스키마에 못 들어감.

---

## 2. 학술 근거 (Track B 요약 — 무엇으로 바꾸나)

설계의 형식 의미론(formal semantics)을 받쳐주는 5 개 기둥:

| # | 문헌 | 우리 설계 어디에 |
|---|---|---|
| 1 | **OCUS** (Gamba·Bogaerts·Guns 2021/2023) + XCP-explain NRP 튜토리얼 | runtime unsat core → cost-ranked treatment 선택. cause set 추출과 treatment 비용함수의 직접 알고리즘 |
| 2 | **Reiter HS-tree** (1987) | hyperedge `(cause set) ──treatment──▶ (symptom set)` 의 형식 의미론. treatment = MUS 들의 minimal hitting set |
| 3 | **ATMS** (de Kleer 1986) | treatment **bundle** 의 형식 의미론. nogood = cause set tail, label = MSS, context switching = "이 번들이 적용된다면" 시뮬레이션 |
| 4 | **De Causmaecker NRP α\|β\|γ 분류** (2011) + Burke (2004) | CauseNode 의 도메인 어휘. hard/soft 의 1 차 attribute 근거 |
| 5 | **Corrective Explanations** (O'Callaghan 2005) + **Inverse CP** (Korikov·Beck 2021) | verified-resolution 정의 : "treatment 적용 후 다항시간 검증 가능한 witness solution + violation delta" |

추가 참고: Knowledge Hypergraph for Fault Diagnosis (2023) 가 구조적으로 가장 유사한 사례 — 우리는 "fault" 자리에 "infeasibility" 가 들어감.

---

## 3. Target Hypergraph Schema

### 3.1 노드 종류 (5+1)

| 노드 | 의미 | 우리 코드 대응 (Track A) |
|---|---|---|
| **CauseNode** | 인과 그래프의 입력 잎. "실제로 일어난 잘못된 사실". reason_code 중 cause 분류된 것이 1:1 매핑 | precheck/_build_infeasible_diagnosis 가 만들어내는 cause-type reason_code |
| **SymptomNode** | 인과 그래프의 출력 잎. solver 관찰 결과로만 보이는 신호. 절대 단독 노출 안 됨 (cause 와 연결될 때만 의미). | `NO_ASSIGNMENT, DAY_ZERO_COVERAGE, NURSE_BLOCKED_DAYS, GRADE_HARD_PROBE, MAX_CAP_SHORTAGE` 등 |
| **TreatmentNode** | 적용 가능한 단위 조작. atomic. | `constraint_impact/control.py` 의 action 1 개, `_force_*_soft_fallback`, `daily_shift_requirements[code]` 조정 등 |
| **TreatmentBundle** | 여러 TreatmentNode 의 묶음. 묶음 자체가 1 급 객체 (ATMS context 의 등가물). 순서·동시성 명시 가능 | `applied_relaxations[]` 가 길어질 때 묶음으로 표현 |
| **EvidenceNode** | 한 번의 re-solve 결과로 "이 treatment 가 이 cause set 을 정말 풀었는가" 를 증명. | `applied_relaxations`, `validation_error is None`, 위반 카운트 delta, witness `schedule_id` |
| **ConstraintInstanceNode** (보조) | CP-SAT 모델 안의 구체 hard/soft 제약 1 개. assumption literal 1 개에 대응. | `HardAssumptionRegistry.entries[]`, 각 `add_hard()` 호출의 name |

각 노드의 공통 속성:
- `id` (kebab-case stable)
- `label` (사람 읽는 이름)
- `category` (예: `coverage`, `team`, `grade`, `transition`, `recovery`, `fixed`, `eligibility`, `carryover`, `config_integrity`)
- `is_hard` (Burke 2004 의 hard/soft) — CauseNode 와 ConstraintInstanceNode 에 필수
- `tier` (T0–T3) — CauseNode 만
- `causal_layer` (policy / data / personal / structural / meta) — CauseNode 만, 기존 hard_assumption.py 의 layer 와 통합

### 3.2 하이퍼엣지 종류 (4)

모두 directed hyperedge. tail 과 head 가 모두 set.

| Edge | tail (입력) | head (출력) | 의미 | 우리 코드 대응 |
|---|---|---|---|---|
| **CAUSAL** | `{CauseNode+}` | `{SymptomNode+}` | "이 cause 들이 동시에 성립하면 이 symptom 들이 관찰된다" | `_infer_no_assignment_direct_reasons` 의 분해 규칙 + ontology.yaml `conflict_scenarios` |
| **TREATMENT** | `{CauseNode+}` | `{TreatmentNode+ \| TreatmentBundle}` | "이 cause set 을 풀려면 이 treatment(들) 가 필요하다" (Reiter hitting set) | `constraint_impact/control.py` action ↔ family, fix_plan axis_actions |
| **EVIDENCE** | `{TreatmentNode \| TreatmentBundle}` | `{EvidenceNode}` | "이 treatment 를 적용한 결과가 이 evidence 이다" (Corrective Explanation) | retry 결과 + applied_relaxations + violation delta |
| **AGGREGATION** | `{CauseNode+}` | `CauseGroupNode` | "여러 cause 가 같은 인과 cluster 에 속한다" (e.g. `causal_group_id`) | 기존 `causal_group_id` 가 이미 partial 구현 |

### 3.3 Bundle 의 형식 의미론 (ATMS 인용)

`TreatmentBundle` = (`atomic_treatments[]`, `application_order`, `simultaneity: "parallel" | "sequential"`).
- bundle 이 정의된 이유는 "동시에 풀어야만 풀리는" cause set 표현 — ATMS 의 multi-cause/multi-effect 에서 nogood 1 개가 두 개 이상의 assumption flip 으로만 제거되는 경우와 동형.
- bundle 비용 = `Σ cost(t_i) + bundle_overhead` (overhead 는 작전상 추가 마찰 — 예: 두 모듈 변경 협의 비용).
- bundle 의 검증은 단일 evidence 노드로 — bundle 통째로 re-solve 한 결과 하나만 따라간다.

### 3.4 Evidence 의 형식 (Corrective + Inverse CP)

EvidenceNode 필수 속성:
- `status: FEASIBLE | INFEASIBLE | PARTIAL`
- `witness_schedule_id: str | null` — 진짜 풀린 근무표 ID (Corrective Explanation 의 witness solution)
- `delta_applied: dict[constraint_id, delta]` — 어떤 제약을 얼마나 풀었는지 vector (Inverse CP 의 δ)
- `violation_delta: dict[symptom_id, {before, after}]` — 증상별 위반 수 변화. 전 → 후 둘 다 기록
- `proof_type: RE_SOLVE | DRAT | CERTIFICATE` — 우리는 일단 RE_SOLVE 만 지원 (재실행 결과), DRAT 는 향후
- `verified: bool` — `validation_error is None and status == FEASIBLE` 일 때만 true
- `timestamp`, `run_id`

핵심 원칙: **EvidenceNode 가 verified=true 이고 cause-set 의 모든 cause 가 violation_delta 에서 0 으로 떨어졌을 때만 "해결됨" 으로 표시**. partial 은 partial 로.

---

## 4. 현 reason_code 의 Cause/Symptom 분류 (확정안)

Track A audit 의 잠정 분류를 학술 분류(α|β|γ + hard/soft)와 교차검증해서 **확정**.

### 4.1 CauseNode (실제 원인 — reason_code 로 노출 OK)

| reason_code (기존) | CauseNode id (신) | category | causal_layer | tier | is_hard |
|---|---|---|---|---|---|
| `CAPACITY_TOTAL_SHORTAGE` | `cause:capacity:monthly_total_shortage` | capacity | policy | T0 | true |
| `MONTHLY_NIGHT_CAPACITY_SHORTAGE` | `cause:capacity:monthly_night_shortage` | capacity (night) | policy | T0 | true |
| `N_CAPACITY_SHORTAGE` | `cause:capacity:daily_night_shortage` | capacity (night) | policy | T0 | true |
| `GLOBAL_DAY_CAPACITY_SHORTAGE` | `cause:capacity:daily_total_shortage` | capacity | policy | T0 | true |
| `GLOBAL_SHIFT_ALLOWED_SHORTAGE` | `cause:eligibility:shift_eligible_shortage` | eligibility | data | T1 | true |
| `MID_REQUIRED_MISSING` | `cause:config:mid_required_missing` | config | meta | T0 | true |
| `MID_DISABLED_BUT_USED` | `cause:config:mid_disabled_but_used` | config | meta | T0 | true |
| `ALLOWED_SHIFTS_ISOLATES_NURSE` | `cause:eligibility:nurse_isolated` | eligibility | data | T0 | true |
| `FIXED_ASSIGN_EXCEEDS_NEED` | `cause:fixed:over_demand` | fixed | personal | T1 | true |
| `FIXED_ASSIGN_VIOLATES_ALLOWED` | `cause:fixed:violates_eligibility` | fixed | personal | T0 | true |
| `FIXED_OFF_EXCEEDS_SPAN` | `cause:fixed:off_exceeds_span` | fixed | personal | T1 | true |
| `GRADE_MIN_EXCEEDS_MAX` | `cause:config:grade_min_gt_max` | config | meta | T0 | true |
| `GRADE_MIN_SUM_EXCEEDS_NEED` | `cause:grade:min_sum_over_need` | grade | policy | T2 | true |
| `GRADE_MAX_SUM_BELOW_NEED` | `cause:grade:max_sum_below_need` | grade | policy | T2 | false (soft 가능) |
| `GRADE_ANTIPAIR_FORCES_SHORTAGE` | `cause:grade:antipair_forces_shortage` | grade | policy | T2 | false |
| `TEAM_MIN_EXCEEDS_GLOBAL_NEED` | `cause:team:min_over_need` | team | policy | T2 | true |
| `TEAM_SIZE_INSUFFICIENT` | `cause:team:size_insufficient` | team | data | T0 | true |

### 4.2 SymptomNode (관찰 신호 — reason_code 로 단독 노출 금지, evidence 와 함께만)

| 기존 reason_code | SymptomNode id (신) | 신호 출처 |
|---|---|---|
| `NO_ASSIGNMENT` | `symptom:solve:no_assignment` | `_validate_generated_roster` work_cells==0 |
| `DAY_ZERO_COVERAGE` | `symptom:solve:day_zero_coverage` | per-day actual==0 |
| `NURSE_BLOCKED_DAYS` | `symptom:solve:nurse_fully_blocked` | `blocked_by_nurse` |
| `GRADE_HARD_PROBE` | `symptom:probe:grade_hard_block` | `_probe_first_grade_hard_blocker` |
| `MAX_CAP_SHORTAGE` | `symptom:probe:grade_max_cap_short` | grade max probe |
| `TEAM_ACTIVE_MEMBERS_INSUFFICIENT` | `symptom:precheck:team_active_short` | team active 합산 결과 |
| `TEAM_SHIFT_ALLOWED_SHORTAGE` | `symptom:precheck:team_shift_eligible_short` | team x allowed_shifts 합산 |
| `GRADE_MIN_AVAILABLE_SHORTAGE` | `symptom:precheck:grade_min_avail_short` | grade 별 available 합산 |
| `TEAM_GRADE_INTERSECT_SHORTAGE` | `symptom:precheck:team_grade_intersect_short` | team∩grade |
| `FIXED_ASSIGN_BREAKS_TEAM_MIN` | `symptom:precheck:fixed_breaks_team_min` | fixed → team min 잔여 |

### 4.3 분해 코드 4 종 (NO_ASSIGNMENT direct reasons) — CAUSAL hyperedge 의 tail-cluster id 로 등록

`NO_ASSIGNMENT_CAPACITY`, `_ELIGIBILITY`, `_FIXED`, `_CARRYOVER` 는 단독 cause 가 아니라 **CAUSAL hyperedge** 의 4 종 패턴이다:
- `CAUSAL: {cause:capacity:*} → {symptom:solve:no_assignment}` (NO_ASSIGNMENT_CAPACITY 패턴)
- `CAUSAL: {cause:eligibility:*} → {symptom:solve:no_assignment}` (NO_ASSIGNMENT_ELIGIBILITY 패턴)
- `CAUSAL: {cause:fixed:*} → {symptom:solve:no_assignment}` (NO_ASSIGNMENT_FIXED 패턴)
- `CAUSAL: {cause:carryover:*} → {symptom:solve:no_assignment}` (NO_ASSIGNMENT_CARRYOVER 패턴)

즉 분해 코드는 더 이상 reason_code 가 아니라 **hyperedge pattern id**.

### 4.4 폐기되는 라벨

- `DAY_ZERO_COVERAGE` reason_code 자체는 **삭제**. day-zero 발견 로직은 보존하되 trigger 로만 사용해 4축 probe 를 강제 실행 (Phase 1 의 핵심).
- `REASON_CODE_TO_CONSTRAINT` dict 폐기. 대신 `cause` 와 `symptom` 어휘를 ontology.yaml 에 분리 등재.

---

## 5. TreatmentNode 카탈로그

학술 분류 (Felfernig DOC, AutoCO δ-perturbation) 를 적용한 신규 분류 + 현 `constraint_impact/control.py` action 매핑.

| TreatmentNode id | action_type | parameter | 코드 위치 | 어떤 cause 를 푸는가 |
|---|---|---|---|---|
| `treatment:soft:grade_max` | force_soft_mode | `_force_grade_max_soft_fallback=True` | control.py:303 + roster_create_service.py:5668 | `cause:grade:max_*` |
| `treatment:soft:grade_min` | force_soft_mode | `_force_grade_min_soft_fallback=True` | control.py:303 | `cause:grade:min_sum_over_need` |
| `treatment:soft:team_min` | force_soft_mode | `team_min_soft_fallback=True` | control.py:276 | `cause:team:min_*` |
| `treatment:soft:handoff` | force_soft_mode | `team_handoff_soft_fallback=True` | control.py:321 | `cause:grade:*` (간접) |
| `treatment:disable:team_min` | disable_module | `team_min_by_team={}` | control.py:273 | `cause:team:size_insufficient` (drastic) |
| `treatment:disable:transition_ban_*` | disable_module | `ban_n_to_d/e_to_d/n_to_e=False` | control.py:328 | `cause:capacity:daily_night_shortage` (간접) |
| `treatment:disable:night_recovery` | disable_module | `two_offs_after_*=False` | control.py:346 | `cause:capacity:monthly_night_shortage` (간접) |
| `treatment:threshold:coverage_min` | set_threshold | `daily_shift_requirements[code] -= δ` | control.py:377 | `cause:capacity:daily_total_shortage` |
| `treatment:threshold:consecutive_work` | set_threshold | `max_consecutive_work_days += δ` | control.py:337 | (rare) |
| `treatment:threshold:monthly_night_cap` | set_threshold | `max_night_shifts_per_month += δ` | control.py:354 | `cause:capacity:monthly_night_shortage` |
| `treatment:threshold:team_min` | set_threshold | `team_min_by_team[t][s] -= δ` | control.py:279 | `cause:team:min_over_need` |
| `treatment:scope:team_min_remove` | narrow_scope | delete team key | control.py:291 | `cause:team:min_over_need` |
| `treatment:data:fix_config` | data_correction_required | (manual; UI hint only) | fix_plan T0 | 모든 `cause:config:*`, `cause:eligibility:nurse_isolated`, `cause:fixed:violates_eligibility` |
| `treatment:data:reduce_fixed` | data_correction_required | reduce fixed_cells | fix_plan T0 | `cause:fixed:over_demand`, `cause:fixed:off_exceeds_span` |

### 5.1 미정의 family 의 treatment (신규 정의 필요)

| Family | 신규 TreatmentNode 제안 |
|---|---|
| OffCap | `treatment:threshold:off_cap` (set off_days += δ) |
| AllowedShiftMask | `treatment:data:relax_allowed_shifts` (master data; manual) |
| NotOneNight | `treatment:disable:not_one_night` |
| PrecepteeSync | `treatment:disable:preceptee_sync` |
| CarryoverBoundary | `treatment:disable:carryover_recovery_guard` |
| FixedWanted | `treatment:data:cancel_wanted` (manual; UI) |

### 5.2 Treatment Bundle 예시

| Bundle id | atomic | 적용 시나리오 |
|---|---|---|
| `bundle:grade_pair_soft` | `treatment:soft:grade_max + treatment:soft:grade_min` | grade max 와 min 이 동시에 빡빡 |
| `bundle:team_grade_intersect` | `treatment:soft:team_min + treatment:soft:grade_max` | TEAM_MIN_VS_GRADE_MAX_INTERSECTION scenario |
| `bundle:capacity_relax_pair` | `treatment:threshold:coverage_min + treatment:threshold:monthly_night_cap` | monthly night + daily total 둘 다 부족 |
| `bundle:fixed_full_audit` | `treatment:data:reduce_fixed + treatment:data:cancel_wanted` | fixed/wanted 누적이 over_demand 일 때 manual cleanup |

---

## 6. Hypergraph Traversal — Runtime Pipeline

(XCP-explain + Reiter HS-tree + OCUS 패턴)

```
CP-SAT INFEASIBLE
  │
  ├─[1] sufficient_assumptions_for_infeasibility() / HardAssumptionRegistry.extract_conflict_cores()
  │        → raw_cores: ConflictCore[] (이미 구현됨)
  │
  ├─[2] map raw_cores → CauseNode set (causal_layer 기반 grouping)
  │        ConstraintInstanceNode (assumption literal) → CauseNode (category)
  │
  ├─[3] AGGREGATION hyperedge 적용: 같은 causal_group 의 CauseNode 묶기
  │        (기존 causal_group_id 재활용)
  │
  ├─[4] CAUSAL hyperedge 탐색: cause cluster 가 tail 인 edge 모두 찾아 head SymptomNode 도출
  │        (관찰된 symptom 과 교차검증해 인과 일관성 체크)
  │
  ├─[5] TREATMENT hyperedge 탐색: cause cluster 가 tail 인 edge 의 head TreatmentNode/Bundle 후보 수집
  │        (Reiter hitting set + DOC priority)
  │
  ├─[6] OCUS-style cost ranking: bundle 별 비용 계산 (relaxation_priority, tier, hard/soft, overhead)
  │        가장 저비용 treatment 1 위 추천 + 차선 2~3 개 노출
  │
  ├─[7] (optional) auto-apply 가 허용된 treatment 면 즉시 적용 → re-solve
  │        현재는 grade_hard_to_soft 만 auto. 나머지는 사용자 승인 후
  │
  └─[8] EVIDENCE 생성:
        re-solve 결과 + applied_relaxations + violation_delta(before/after)
        EVIDENCE hyperedge: TreatmentBundle → EvidenceNode
        verified = (validation_error is None) AND (all cause violation→0)
```

---

## 7. 데이터 스키마 (YAML 초안)

기존 `app/services/semantics/ontology.yaml` v3 를 v4 로 올리고 다음 섹션 추가/재배치:

```yaml
version: 4

# 기존 constraint_families 는 ConstraintInstance 카탈로그로 retain (CP-SAT add_hard 와 1:1).
constraint_instances:
  - id: CoverageMin
    parent: CoverageConstraint
    is_hard: true
    causal_layer: policy
    tier: T2
    cp_sat_pattern: "CoverageMin:day_{d}:shift_{code}"
  # ... 19 families 동일하게 이전

# 신설: causes
causes:
  - id: cause:capacity:monthly_total_shortage
    label: "월 총 근무 슬롯이 공급 상한 초과"
    category: capacity
    causal_layer: policy
    tier: T0
    is_hard: true
    aliases: [CAPACITY_TOTAL_SHORTAGE]
    detection_source: precheck.capacity_total_check
    explanation_template: "월 총 필요 슬롯 {required} 가 공급 상한 {capacity} 를 초과 ({surplus} 부족)"
    evidence_required:
      - required: int
      - capacity: int
      - nurse_count: int
      - off_days: int
  # ... 모든 cause 동일 스키마

# 신설: symptoms
symptoms:
  - id: symptom:solve:no_assignment
    label: "실근무 배정 0건"
    category: solve_result
    detection_source: validate.work_cells_zero
    aliases: [NO_ASSIGNMENT]
  # ...

# 신설: treatments
treatments:
  - id: treatment:soft:grade_max
    action_type: force_soft_mode
    target_constraint: GradeMax
    parameter: { config_key: _force_grade_max_soft_fallback, value: true }
    cost: 2
    auto_applicable: true
    implementation: roster_create_service.py:5668
  # ...

# 신설: treatment bundles
treatment_bundles:
  - id: bundle:team_grade_intersect
    atomic_treatments:
      - treatment:soft:team_min
      - treatment:soft:grade_max
    simultaneity: parallel
    overhead: 1
    applicable_scenario: TEAM_MIN_VS_GRADE_MAX_INTERSECTION

# 신설: hyperedges
hyperedges:
  causal:
    - id: edge:causal:capacity_no_assignment
      tail: [cause:capacity:monthly_total_shortage, cause:capacity:daily_night_shortage]
      head: [symptom:solve:no_assignment]
      condition: "any tail cause active"

  treatment:
    - id: edge:treatment:capacity_relax
      tail: [cause:capacity:daily_total_shortage]
      head: [treatment:threshold:coverage_min]
      priority: 1
      direction_hint: decrease

    - id: edge:treatment:grade_intersect
      tail: [cause:grade:max_sum_below_need, cause:team:min_over_need]
      head: [bundle:team_grade_intersect]
      priority: 1

  evidence:
    # 동적으로 생성 (runtime). 정적 yaml 에는 패턴만 등록.
    - id_pattern: "edge:evidence:{run_id}"
      tail: TreatmentBundle
      head: EvidenceNode
      verification_method: re_solve_then_validate
```

---

## 8. Backend Schema 변화 (페이로드)

기존 `build_unrecoverable_payload` 결과의 핵심 필드를 다음과 같이 재구성:

```jsonc
{
  "infeasibility": {
    "diagnosis_id": "uuid",
    "causes": [                                // ← reason_code 자리. CauseNode 만.
      {
        "cause_id": "cause:capacity:monthly_night_shortage",
        "label": "월간 N 수요가 N 상한 용량 초과",
        "tier": "T0",
        "causal_layer": "policy",
        "evidence": { "n_required": 84, "n_capacity": 60, ... }
      }
    ],
    "observed_symptoms": [                     // ← 증상은 별도 필드. cause 와 분리.
      { "symptom_id": "symptom:solve:no_assignment", "signal": { ... } }
    ],
    "causal_edges": [                          // ← cause→symptom 인과 명시
      { "edge_id": "edge:causal:capacity_no_assignment",
        "tail": ["cause:capacity:monthly_night_shortage"],
        "head": ["symptom:solve:no_assignment"] }
    ],
    "treatments": [                            // ← treatment 후보 ranked
      {
        "treatment_id": "bundle:capacity_relax_pair",
        "kind": "bundle",
        "atomic": ["treatment:threshold:monthly_night_cap",
                   "treatment:threshold:coverage_min"],
        "cost": 4,
        "applicable_to": ["cause:capacity:monthly_night_shortage",
                          "cause:capacity:daily_total_shortage"],
        "auto_applicable": false,
        "rationale_ko": "야간 용량 상한 +1, daily 야간 요구치 -1 동시 적용"
      }
    ],
    "applied_treatment": null,                 // ← 자동/수동 적용된 treatment (있으면)
    "evidence": null                           // ← apply 후 채워짐
  }
}
```

`evidence` 가 채워지면:
```jsonc
"evidence": {
  "evidence_id": "uuid",
  "applied_treatment_id": "bundle:capacity_relax_pair",
  "status": "FEASIBLE",
  "witness_schedule_id": "sched_2026_05_123",
  "delta_applied": { "max_night_shifts_per_month": +1, "daily_N_demand": -1 },
  "violation_delta": {
    "symptom:solve:no_assignment":     { "before": 1, "after": 0 },
    "cause:capacity:monthly_night_shortage": { "before": 1, "after": 0 }
  },
  "proof_type": "RE_SOLVE",
  "verified": true,
  "timestamp": "2026-05-15T..."
}
```

`UNDIAGNOSED` 가 필요한 경우 (cause probe 4축 모두 침묵):
```jsonc
"causes": [{ "cause_id": "cause:undiagnosed", "tier": "T0",
             "evidence": { "raw_validator_evidence": {...}, "raw_conflict_cores": [...] }}],
"observed_symptoms": [...],
"treatments": []    // 자동 후보 없음. UI 는 raw evidence 노출.
```

---

## 9. 코드 변경 영향 (위치별)

| 파일 | 변경 종류 | 핵심 내용 |
|---|---|---|
| `app/services/semantics/ontology.yaml` | major | v3→v4, `constraint_instances/causes/symptoms/treatments/treatment_bundles/hyperedges` 5 섹션 신설 |
| `app/services/semantics/ontology.py` | major | `OntologyCause/OntologySymptom/OntologyTreatment/OntologyTreatmentBundle/OntologyHyperedge` dataclass 신설, loader 확장 |
| `app/services/semantics/ontology_attach.py` | replace | `REASON_CODE_TO_CONSTRAINT` 폐기 → `attach_cause_ontology(...)` / `attach_symptom_ontology(...)` 분리. cause-only 어휘. |
| `app/services/roster_create_service.py` | 핵심 | (a) DAY_ZERO_COVERAGE fallback 라벨 throw 제거 (L3877-3926) — day-zero 발견은 trigger 로만, (b) cause 미진단 시 `cause:undiagnosed` + evidence dump, (c) `_extract_unrecoverable_violated_constraints` 가 cause/symptom 분리 반환 |
| `app/services/precheck/build_unrecoverable_payload.py` | major | payload 새 스키마 (8 절) 로 재구성 |
| `app/services/constraint_impact/control.py` | minor | `apply_treatment(treatment_id)` 엔트리 추가. action 분기 통일 |
| `app/services/precheck/fix_plan.py` | minor | fix_plan 의 axis 출력을 TreatmentNode/Bundle 출력으로 어댑트 (호환 레이어) |
| `app/routers/ontology.py` | minor | 새 노드 타입 (Cause/Symptom/Treatment/Bundle/Evidence) 렌더링 추가. 프론트 영향 무시 OK |
| `tests/test_semantics_ontology.py` | rewrite | DAY_ZERO_COVERAGE 어설션 제거, 새 cause/symptom/treatment 어설션 추가 |
| `docs/INFEASIBLE_DIAGNOSTICS_FRONT_BACK_ARCHITECTURE.md` | minor | 라벨 카탈로그 갱신 |

---

## 10. Migration Phases (안전한 단계적 적용)

### Phase 1 — 거짓 라벨 제거 + UNDIAGNOSED 도입 (가장 작은 단위, 즉시 실행 가능)
- `roster_create_service.py:3877-3926` 의 DAY_ZERO_COVERAGE 라벨 throw 제거. day-zero 감지 로직은 보존하되 cause probe 호출만 수행.
- cause probe 4 축이 모두 침묵하면 `[reason_code=UNDIAGNOSED]` + evidence dump 로 반환.
- `routers/roster_create.py:57` 및 `roster_create_service.py:3386` 의 DAY_ZERO_COVERAGE 키워드 매칭 제거.
- `ontology_attach.py:52` 의 `"DAY_ZERO_COVERAGE": "CoverageMin"` 라인 삭제.
- `tests/test_semantics_ontology.py:23-31` UNDIAGNOSED 테스트로 교체.
- 위 표 #6 의 `MONTHLY_NIGHT_CAPACITY_SHORTAGE → NightRecovery` 와 `N_CAPACITY_SHORTAGE → BoundaryTransitionBan` 잘못된 매핑도 같이 제거.
**기대 효과**: 거짓 라벨 사라짐. 단, 새 cause/treatment 그래프는 아직 안 생김. 페이로드 스키마 변화 0.

### Phase 2 — Cause/Symptom 어휘 분리 + ontology.yaml v4 도입
- ontology.yaml v4 작성 (causes/symptoms/treatments 섹션 신설, hyperedges 는 비워둠).
- `ontology.py` loader 확장.
- `ontology_attach.py` 를 `attach_cause_ontology/attach_symptom_ontology` 로 분리. 기존 REASON_CODE_TO_CONSTRAINT 폐기.
- 모든 reason_code 발생 지점이 cause 인지 symptom 인지 라벨 부여.
- payload 에 `causes[]` / `observed_symptoms[]` 분리 등재 (treatments[] 는 아직 비어 있음).
- 호환 레이어: 기존 `violated_constraints[].reason_code` 는 deprecated 로 유지, 동시에 새 필드 채움. (1 릴리즈 후 제거)

### Phase 3 — Hyperedge (CAUSAL + TREATMENT) 도입
- `hyperedges.causal` 정의 — `_infer_no_assignment_direct_reasons` 의 4 축 규칙을 정적 edge 로 등재.
- `hyperedges.treatment` 정의 — 표 5.0 + 5.1 + 5.2 의 TreatmentNode/Bundle 전수.
- `apply_treatment(treatment_id)` API 신설. 기존 `constraint_impact/control.py` action 을 백엔드로.
- payload 에 `treatments[]` 채움. ranked.

### Phase 4 — Evidence + Verified Resolution
- `apply_treatment` 적용 후 re-solve → `EvidenceNode` 생성.
- `violation_delta` 계산. before/after 비교.
- payload 에 `evidence` 채움. UI 가 before/after 근무표 diff 노출 가능.
- 기존 `_force_grade_max_soft_fallback` retry 경로를 새 Evidence pipeline 으로 포팅.

### Phase 5 — OCUS Cost Ranking + Bundle Auto-resolve
- treatment ranking 을 OCUS cost function 으로 격상 (현재는 단순 priority).
- 사전 정의된 bundle scenarios 가 auto 적용 가능하면 즉시 적용 후 evidence 첨부.
- conflict_cores 의 derivation chain 을 ontology graph 의 cause node 속성으로 흡수.

### Phase 6 (선택) — Certifying Solver / DRAT
- 학술 단계의 verified-resolution 강화. 우선순위 낮음. 운영에서는 RE_SOLVE 만으로 충분.

---

## 11. Open Questions (사용자 확정 필요)

1. **Sentinel 이름**: 미진단 케이스의 reason_code 는 `UNDIAGNOSED` / `CAUSE_NOT_IDENTIFIED` / `cause:undiagnosed` 중 어느 것이 좋은가? (위에서는 `cause:undiagnosed` 가정)
2. **호환 레이어 유지 기간**: Phase 2 의 deprecated `violated_constraints[].reason_code` 필드를 몇 릴리즈 유지할지? (제안: 1 릴리즈)
3. **Bundle auto-apply 정책**: 현재는 `grade_hard_to_soft` 만 auto. 새 bundle 들 (예: `bundle:team_grade_intersect`) 도 auto 로 갈지, 항상 사용자 승인 후로 갈지?
4. **OffCap/AllowedShiftMask/NotOneNight/PrecepteeSync/CarryoverBoundary/FixedWanted 의 신규 treatment 정의** (5.1) 를 Phase 3 안에 포함할지, Phase 5 로 미룰지?
5. **OWL DL pre-solver filter** (Track B §2.5) 도입은 본 설계 범위에 포함할까? (제안: 별도 RFC 로 분리)
6. **conflict_cores derivation chain 활용**: Phase 5 에 cause node 속성으로 흡수 vs 별도 evidence 첨부로 유지?

---

## 12. 한 페이지 정리

```
┌──────────────────────────────────────────────────────────────────┐
│         Directed Hypergraph for CP-SAT Infeasibility             │
│                                                                  │
│   CauseNode ──┐                                                  │
│   CauseNode ──┤───[CAUSAL hyperedge]──▶ SymptomNode              │
│   CauseNode ──┘                                                  │
│       │                                                          │
│       │                                                          │
│       └──[TREATMENT hyperedge]──▶ TreatmentNode | TreatmentBundle│
│                                              │                   │
│                                              │                   │
│                                              └─[EVIDENCE edge]──▶│
│                                                          EvidenceNode
│                                                          { witness,│
│                                                            delta,  │
│                                                            verified}│
└──────────────────────────────────────────────────────────────────┘

핵심 규칙:
  · reason_code 로 노출되는 것은 오직 CauseNode (또는 cause:undiagnosed).
  · SymptomNode 는 cause 와 함께 있을 때만 의미를 가짐.
  · TreatmentNode/Bundle 은 1 급 객체. 비용·우선순위·자동가능여부 명시.
  · EvidenceNode 가 verified=true 이고 모든 cause violation==0 일 때만 "해결".
```
