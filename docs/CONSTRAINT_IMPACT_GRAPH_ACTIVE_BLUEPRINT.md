# Constraint Impact Graph 구현 청사진 (현재 dev branch 기준)

이 문서는 **현재 dev branch의 실제 서비스 로직**을 기준으로 Constraint Impact Graph / Delta Feasibility Simulation Layer를 어떻게 설계해야 하는지 정리한 구현 청사진이다.

## 진행 체크리스트 (현재 구현 기준)

- [x] **Semantics Snapshot Layer**
  - `roster_create_service.py`, `cp_sat_basic.py`, `constraints/*.py`에서 런타임 semantics를 `roster_system`에 attach
  - `services/constraint_impact/snapshot.py`, `snapshot_builders.py` 구현
- [x] **Assignment / Derived Atom Layer**
  - `services/constraint_impact/atoms.py` 구현
  - fixed / fixed_wanted / coverage 포함 여부 / preceptee coupling 반영
- [x] **Primitive Rule Layer (v1)**
  - `rule_primitives.py`, `rule_compiler.py`, `rule_masks.py` 구현
  - profile / weekend_off_only / personal rules / assignment-window rules를 primitive bundle로 compile
- [x] **Nurse State Machine Layer (v1)**
  - `services/constraint_impact/nurse_state_machine.py` 구현
  - 현재는 `consecutive_work`, `consecutive_nights`, `recovery_off_required_after_day` 추적
  - `2N/3N → 2OFF` 파생 atom 생성까지 구현
  - inbound/transfer carryover artifact(`issued/latest/blank`)를 첫 visible day 초기 상태에 주입
  - carryover 기반 first-day transition context(`previous_shift`, `transition_code`) 반영
  - carryover 기반 `fatigue_score`, `recovery_debt` seed 및 boundary day validation 반영
- [ ] **Typed Constraint Hypergraph Layer**
  - 아직 미구현
  - 다음 단계 구현 대상
- [x] **Delta Simulation Layer (v1)**
  - `services/constraint_impact/simulation.py` 구현
  - coverage / preceptee / team_min / grade / nurse-local 평가 구현
  - `/roster_create/generate` 응답에 inline `constraint_impact` payload 연결 완료
- [x] **실서비스 검증**
  - 2026-04 생성 실검증 수행
  - 생성 후 삭제(soft delete)로 원복 확인
- [ ] **Parity / Drift Harness**
  - simulation vs solver mismatch 자동 수집은 아직 미구현
- [ ] **State Carryover Artifact**
  - artifact 수집 및 state initialization 반영은 완료
  - 다만 carryover conflict validation / graph obligation node화는 아직 미구현

---

중요 원칙:

- 기준 경로는 **오직 active path**만 본다.
- 다음 모듈은 설계 기준에서 제외한다.
  - `cp_sat_adaptive.py`
  - `cp_sat_basic_base.py`
  - `cp_sat_basic_lagrangian.py`
  - `cp_sat_basic_legacy.py`
  - `cp_sat_main_v2.py`
  - `cp_sat_main_v3.py`
  - `sample_test`
- 현재 branch에서 실제 의미를 만드는 경로는 다음이다.
  - `app/services/roster_create_service.py::_run_cp_sat_basic`
  - `app/services/cp_sat_basic.py::generate_roster_cp_sat`
  - `app/services/cp_sat_basic.py::_build_full_model`
  - `app/services/cp_sat/feasibility_alerts.py`
  - `app/services/cp_sat/mid_feasibility.py`
  - `app/services/cp_sat/objective_terms.py`
  - `app/services/constraints/*.py`

---

## 1. 결론: 지금 당장 Graph부터 만들면 안 된다

현재 branch는 hard/soft 의미가 한 파일에 모여 있지 않다.

실제 의미는 여러 단계에서 조립된다.

1. `roster_create_service.py`
   - fixed wanted 수집
   - inbound nurse 확장
   - special fixed 반영
   - `initial_constraints` 병합
   - `run_preflight_feasibility_alerts()` 실행
   - `validate_mid_hard_feasibility()` 실행
   - grade max retry 시 `allow_soft_fallback=True` 강제

2. `cp_sat_basic.py`
   - join/leave 계산
   - fixed / fixed_wanted / fixed_type_by_cell 계산
   - off partition 계산
   - vacation vs structural OFF 구분
   - `n_forbid_n` 계산
   - preceptee follow / `preceptee_shift_count` 계산
   - coverage exclude 적용
   - min/max coverage, off cap, off_first 적용
   - 하드 규칙의 skip/bypass/enforce 분기 처리

3. `objective_terms.py`
   - team / grade / handoff 제약을 objective build 시점에 주입

즉, 이 branch에서는 `ConstraintGraph`보다 먼저 **현재 실행의 의미를 정규화하는 `SemanticsSnapshot`** 이 필요하다.

---

## 2. 최상위 아키텍처

추천 구조는 아래 5계층이다.

1. **Semantics Snapshot Layer**
2. **Assignment / Derived Atom Layer**
3. **Nurse State Machine Layer**
4. **Typed Constraint Hypergraph Layer**
5. **Delta Simulation Layer**

각 계층 역할:

### 2.1 Semantics Snapshot Layer
- 현재 실행 시점의 “실효 제약 의미”를 정규화
- source of truth 역할
- 그래프/시뮬레이션의 입력

### 2.2 Assignment / Derived Atom Layer
- `assign[n,d,s]` 원자
- fixed / fixed_wanted / forced / synced / coverage_excluded 같은 메타데이터 부착

### 2.3 Nurse State Machine Layer
- 간호사 단위 시간축 제약 표현
- 예: 1N 금지, 연속근무, 2N2O, 3N2O, off window, weekend only
- **다음 확장 우선순위**
  - `E→N`, `N→D`, `N→E` 같은 전이 금지/회복 전이 규칙을 transition object로 명시화
  - `NOD`, `NOE`, `EOD` 같은 패턴을 단순 soft penalty가 아니라 fatigue/event 시퀀스로 상태화
  - 개인 피로도(`fatigue_score`, `recovery_debt`)를 hard/soft 혼합 지표로 추가
  - `join/leave/blocked/assignment-window`를 단순 active range가 아니라 **state visibility / state continuity** 관점으로 분리
  - 특히 `병동이동/파견`은 target 병동에서 근무 state를 이어받아야 하므로 `transfer_state_carryover` 개념 추가
  - 개인별 quota / 요일 규칙은 state와 graph를 분리해서 모델링
    - 예: `N은 딱 2개`, `OFF 개수 11개`, `수요일은 D/O만`, `E는 주말만`
    - 이들은 nurse state에 누적 카운터/요일 마스크로 반영하고,
      최종 판정은 primitive personal rule node에서 수행
    - 내부 primitive는 `DisallowedShiftRule`, `RequiredShiftRule`, `ShiftCountBoundRule` 3종으로 고정
    - `weekend_off_only`도 별도 엔진 개념으로 두지 않고 이 primitive rule bundle로 컴파일
    - 앞으로 추가될 개인 규칙은 전부 “사용자 표현 → primitive 3종 compile” 경로만 타게 한다
    - 기간별 허용 shift 변경(예: `17일까지 교육, 18일부터 D 가능`)도 동일하게 day-scoped primitive rule bundle로 표현
  - 즉, 다음 버전 state machine은 단순 `consec_work` 누적기가 아니라 **shift-transition + fatigue accumulator**가 되어야 함

### 2.4 Typed Constraint Hypergraph Layer
- coverage, team, grade, handoff, preceptee sync 같이 여러 atom을 동시에 묶는 제약 표현

### 2.5 Delta Simulation Layer
- 사용자/에이전트 액션을 임시 반영
- 바뀐 atom과 연쇄 파생 atom만 재계산
- hard violation / risky constraint / causal chain 출력

---

## 3. 현재 branch에서 source of truth는 어디인가

### 3.1 절대 source of truth로 삼아야 하는 것

#### A. orchestration semantics
- 파일: `app/services/roster_create_service.py`
- 이유:
  - fixed_wanted가 선호인지 hard fixed인지 결정됨
  - inbound nurse 포함 여부가 결정됨
  - `initial_constraints`가 병합됨
  - preflight / mid feasibility가 solve 전에 수행됨
  - retry 시 grade soft fallback 강제 여부가 결정됨

#### B. model-build semantics
- 파일: `app/services/cp_sat_basic.py`
- 이유:
  - 실제 hard rule이 여기서 `m.Add(...)`로 enforce됨
  - fixed 우선 / forbidden skip / transition bypass / off cap 계산식 등이 여기 있음

#### C. objective-time injected constraints
- 파일: `app/services/cp_sat/objective_terms.py`
- 이유:
  - team / grade / handoff는 별도 'constraints phase'가 아니라 objective build 도중 삽입됨

### 3.2 보조 정보로만 써야 하는 것

#### `app/services/roster_system.py::_find_violations()`
- 부분 검증기임
- 현재 solver semantics 전체를 재현하지 않음
- canonical truth로 사용하면 안 됨

---

## 4. SemanticsSnapshot 설계

이 레이어가 1순위 구현 대상이다.

```python
from dataclasses import dataclass, field
from typing import Any, Literal

ConstraintMode = Literal[
    "enforced",
    "soft_fallback",
    "skipped_by_capacity",
    "bypassed_by_fixed",
    "inactive",
]

@dataclass
class SolveAttemptMeta:
    attempt_index: int
    label: str  # "primary", "grade_max_retry"
    grade_strategy: str
    forced_grade_soft_fallback: bool


@dataclass
class FixedCellFact:
    nurse_index: int
    day_index: int
    shift_code: str
    fixed_source: str  # weekly_off | special_fixed | fixed_wanted | manual | etc
    shift_type: str | None
    counts_to_coverage: bool


@dataclass
class PrecepteeFact:
    nurse_index: int
    preceptor_index: int | None
    follow_enabled: bool
    follow_days: set[int]
    counts_to_coverage: bool
    fixed_wanted_override_days: set[int] = field(default_factory=set)


@dataclass
class ConstraintModeFact:
    constraint_key: str
    mode: ConstraintMode
    source_file: str
    reason: str


@dataclass
class SemanticsSnapshot:
    year: int
    month: int
    attempt: SolveAttemptMeta

    # orchestration-time normalized inputs
    nurse_ids_in_scope: list[str]
    inbound_nurse_ids: list[str]
    fixed_cells: list[FixedCellFact]
    special_fixed_requests: list[dict[str, Any]]
    merged_initial_constraints: dict[str, Any]

    # derived runtime facts
    join: list[int]
    leave: list[int]
    active_days_by_nurse: dict[int, set[int]]
    blocked_by_nurse: dict[int, set[int]]
    fixed_wanted_cells: set[tuple[int, int]]
    fixed_type_by_cell: dict[tuple[int, int], str | None]
    coverage_exclude_cells: set[tuple[int, int]]

    vacation_off_cells: set[tuple[int, int]]
    structural_off_cells: set[tuple[int, int]]
    forced_off_cap_excluded: set[tuple[int, int]]
    off_exception_cells: set[tuple[int, int]]
    off_exception_vacation_cells: set[tuple[int, int]]
    weekend_days: set[int]

    n_forbid_n: set[int]
    preceptee_facts: list[PrecepteeFact]

    # config-derived semantics
    off_first: bool
    preceptee_on: bool
    preceptee_shift_count: bool
    team_min_soft_fallback: bool
    team_handoff_soft_fallback: bool
    grade_allow_soft_fallback: bool

    # external precheck outcomes
    preflight_alerts: list[str]
    mid_feasibility_error: str | None

    # per-family resolved modes
    constraint_modes: list[ConstraintModeFact]
```

### 4.1 이 snapshot에 반드시 들어가야 하는 이유

이 branch에서는 다음 정보가 전부 실제 의미를 바꾼다.

- `fixed_wanted_use_yn`
- inbound nurse inclusion
- `initial_constraints` 병합 결과
- `preflight_alerts`
- `mid_feasibility` 결과와 M 수요 보정
- `preceptee_on`
- `preceptee_shift_count`
- `coverage_exclude_cells`
- `off_first`
- grade retry의 `allow_soft_fallback=True`

이 중 하나라도 snapshot에서 빠지면, 나중에 delta simulation이 실제 solver와 어긋날 가능성이 높다.

---

## 5. 현재 branch에서 hard를 표현할 때 꼭 분리해야 하는 것

### 5.1 configured hard vs enforced hard

예시:
- `team_constraints.py`
  - `team_min_soft_fallback=False`여도
  - active team size < min이면 **hard constraint를 추가하지 않고 skip**함

따라서 constraint graph에서 단순히

```text
TeamMin = hard
```

라고 넣으면 안 된다.

반드시 다음처럼 표현해야 한다.

```text
TeamMin(team=T1, day=26, shift=D)
- configured_mode = hard
- effective_mode = skipped_by_capacity
- reason = active_members(0) < min_required(1)
```

### 5.2 bypassable hard vs non-bypassable hard

현재 branch 기준 예시:

#### bypass 가능
- `N→D`, `E→D`, `N→E`
  - 해당 패턴이 **fixed**로 명시되면 제약 면제
- `initial_forbidden`
  - fixed가 있으면 skip
- weekend-off-only 일부
  - fixed / off_window / prev_month tail 조건에서 skip 가능

#### bypass 불가
- K+1 연속근무 OFF 제약
  - 코드 주석상 fixed_wanted 포함 우회 불가

이 차이는 snapshot에 다음처럼 저장해야 한다.

```python
ConstraintModeFact(
    constraint_key="consecutive_work_window:nurse=12:day=24-30",
    mode="enforced",
    source_file="cp_sat_basic.py",
    reason="fixed_wanted 포함 우회 불가 정책"
)
```

---

## 6. Assignment Atom 설계 (현재 branch 맞춤)

```python
@dataclass
class AssignmentAtom:
    nurse_index: int
    day_index: int
    shift_code: str
    source: str  # solver | fixed_wanted | weekly_off | special_fixed | agent_action | synced
    is_fixed: bool
    is_forced: bool
    counts_to_coverage: bool
    override_reason: str | None
    coupled_unit_id: str | None  # preceptor/preceptee unit
```

### 6.1 이 branch에서 필요한 필드

#### `source`
- `roster_create_service.py`에서 이미 fixed source가 갈린다.
- 예:
  - `fixed_wanted`
  - weekly off
  - special fixed

#### `counts_to_coverage`
- `preceptee_shift_count=False`일 수 있음
- `coverage_exclude_cells`도 있음
- 따라서 atom 단위 coverage 포함 여부가 필요함

#### `coupled_unit_id`
- preceptee follow가 hard equality로 들어감
- 단일 nurse atom으로만 보면 영향도 계산이 깨짐

---

## 7. Nurse State Machine 설계 (현재 branch 맞춤)

State machine은 간호사별 시간축 hard를 담당한다.

핵심 이벤트:

- `assign D/E/N/O/M`
- `fixed non-O`
- `fixed O`
- `forced O`
- `blocked day`
- `follow preceptor`
- `cross-month n_tail`
- `cross-month off_tail`

상태 예시:

```python
@dataclass
class NurseDayState:
    nurse_index: int
    day_index: int
    assigned_shift: str | None
    consecutive_work: int
    consecutive_nights: int
    recovery_off_required: int
    weekend_only_active: bool
    n_forbidden: bool
    off_cap_used: int
```

### 7.1 여기서 계산해야 하는 대표 체인

예:

```text
B 24=N, 25=N
→ consecutive_nights = 2
→ 2N2O recovery required
→ 26,27 OFF forced or block-end constraint activated
→ B가 26일 coverage / team / grade 후보군에서 빠질 수 있음
```

즉 state machine은 단순 위반 체크가 아니라 **forced consequence 생성기**여야 한다.

---

## 8. Typed Constraint Hypergraph 설계

현재 branch에서는 다음 제약군이 graph node로 적합하다.

### 8.1 Coverage 계열
- `CoverageMin(day, shift)`
- `CoverageMax(day, shift)`
- `MSoftCoverage(day)`
- `LookaheadOffCap(day)`

### 8.2 Team / Grade / Handoff 계열
- `TeamMin(team, day, shift)`
- `GradeMin(grade, day, shift)`
- `GradeMax(grade, day, shift)`
- `TeamGradeHandoff(team, rule, day, transition)`

### 8.3 Nurse-local hard 계열
- `ConsecutiveWorkWindow(nurse, start_day)`
- `TransitionBan(nurse, day, N->D / E->D / N->E)`
- `NotOneNight(nurse, day)`
- `Recovery2N2O(nurse, end_day)`
- `Recovery3N2O(nurse, end_day)`
- `MonthlyNightCap(nurse)`
- `MonthlyOffMin(nurse)`
- `MonthlyOffMax(nurse)`
- `WeekendOnly(nurse, day)`
- `CrossMonth4O(nurse, head_day)`

### 8.4 Coupling 계열
- `PrecepteeSync(preceptor, preceptee, day)`
- `PrecepteeCoverageInclusion(preceptee, day)`

### 8.5 Override / exception 계열
- `FixedPriority(nurse, day)`
- `InitialForbiddenBypass(nurse, day)`
- `TransitionBypassByFixed(nurse, day)`
- `RecoveryBlockByFixedWanted(nurse, day)`

---

## 9. Delta Simulation을 어떻게 붙일 것인가

현재 branch에서는 full recompute보다 **affected-scope replay**가 맞다.

### 9.1 입력

```python
@dataclass
class SimulationAction:
    action_id: str
    type: str  # assign_shift | add_off | remove_fixed | change_team_min ...
    assignments: list[dict]
    source: str  # user | agent | system
```

### 9.2 단계

1. 현재 `SemanticsSnapshot` 로드
2. action을 atom 변경으로 변환
3. affected nurse/day 범위 계산
4. preceptee/preceptor coupling 확장
5. nurse state machine 국소 재계산
6. 파생 forced atom 생성
7. 관련 hyperedge만 재평가
8. `pressure/slack` 변화 계산
9. 결과 반환

### 9.3 출력

```python
@dataclass
class SimulationResult:
    action_id: str
    valid_under_current_semantics: bool
    triggered_forced_atoms: list[AssignmentAtom]
    violated_constraints: list[str]
    risky_constraints: list[str]
    causal_chain: list[str]
    notes: list[str]
```

---

## 10. 현재 branch에서 꼭 first-class로 다뤄야 하는 tricky semantics

### 10.1 fixed_wanted는 전역 무적이 아니다

현재 branch 기준:

- fixed_wanted는 hard fixed source일 수 있음
- coverage에 포함될 수 있음
- profile hard 일부를 우회할 수 있음
- 하지만 모든 hard를 무시하지는 않음
- 연속근무 K+1 OFF는 우회 불가
- recovery OFF 슬롯에 non-O fixed_wanted가 있으면
  - 2N 블록 종료 금지
  - 3N 블록 자체 금지

즉 representation은 다음처럼 constraint-family별 guard여야 한다.

```python
@dataclass
class OverridePolicy:
    override_type: str  # fixed_wanted
    bypasses_transition_ban: bool
    bypasses_initial_forbidden: bool
    bypasses_profile_shift_limit: bool
    bypasses_consecutive_work: bool
    blocks_recovery_slot: bool
    counts_to_coverage: bool
```

### 10.2 preceptee는 atom이 아니라 coupled unit이다

현재 branch 기준:

- `preceptee_on=True`면 equality hard 제약이 생김
- `preceptee_shift_count=False`면 coverage에서는 제외될 수 있음
- 프리셉티 fixed wanted는 따로 map으로 보호됨

즉 simulation에서 nurse 한 명만 바꾼다고 보면 안 되고,
**unit 단위 영향도 계산**이 필요하다.

### 10.3 `off_first`는 OFF cap 해석 자체를 바꾼다

현재 branch에서는 `off_first=True`일 때

- `off_days` 명세를 사실상 직접 hard min으로 보지 않고
- min coverage 기반 잔여 OFF 회수 구조로 해석한다.

즉 OFF 관련 graph pressure는 `off_first` 여부에 따라 완전히 달라져야 한다.

### 10.4 retry lineage가 필요하다

현재 branch에서는 grade max 계열 실패 시 retry로

```text
allow_soft_fallback=True
```

가 강제될 수 있다.

따라서 simulation도 다음을 구분해야 한다.

- primary semantics에서 invalid
- retry semantics에서는 valid

이 구분이 없으면 agent가 잘못된 reject를 하게 된다.

---

## 11. 구현 순서 (권장)

### Phase 1 — Snapshot 정규화
구현 대상:
- `app/services/constraint_impact/snapshot.py`
- `app/services/constraint_impact/build_snapshot.py`

목표:
- 현재 active path의 의미를 구조화된 snapshot으로 추출

필수 입력 소스:
- `roster_create_service.py`
- `cp_sat_basic.py`
- `feasibility_alerts.py`
- `mid_feasibility.py`
- `objective_terms.py`

### Phase 2 — Atom / State model
구현 대상:
- `atoms.py`
- `nurse_state_machine.py`

목표:
- nurse-local time-sequence와 forced consequence 계산

### Phase 3 — Hypergraph
구현 대상:
- `constraint_nodes.py`
- `graph_builder.py`

목표:
- coverage/team/grade/handoff/local hard를 node/edge로 정규화

### Phase 4 — Delta Simulation
구현 대상:
- `simulate_action.py`
- `impact_report.py`

목표:
- 특정 action의 local hard impact 분석

### Phase 5 — Parity Test
구현 대상:
- fixture 기반 simulation vs actual solve 비교

비교해야 하는 것:
- fixed_wanted 있는 경우
- preceptee follow on/off
- preceptee_shift_count on/off
- off_first on/off
- weekend_off_only
- 2N2O / 3N2O
- grade retry fallback path

---

## 12. 이 설계에서 하지 말아야 할 것

1. `roster_system._find_violations()`를 canonical checker로 쓰기
2. fixed_wanted를 단일 “무적 override”로 모델링하기
3. team/grade/handoff를 단일 constraints 단계에서만 생기는 것으로 가정하기
4. retry path를 무시하기
5. preflight alerts와 solver semantics를 분리된 세계로 취급하기
6. inactive solver variants를 끌어와 설계를 혼탁하게 만들기

---

## 13. 최종 권고

현재 branch에서 가장 맞는 전략은 다음이다.

> **ConstraintGraph를 먼저 만들지 말고, 현재 런타임 의미를 보존하는 SemanticsSnapshot을 먼저 구현하라.**

그 다음에만

- atom
- state machine
- typed hypergraph
- delta simulation

순서로 올라가야 한다.

이 순서를 지키면,

- 현재 `cp_sat_basic`의 복잡한 skip/bypass 로직을 잃지 않고
- `fixed_wanted`, `preceptee`, `off_first`, `grade retry` 같은 branch 특이 semantics를 보존하면서
- 에이전트가 액션 전에 **명확하고 설명 가능한 hard impact 판단**을 할 수 있다.
