# Constraint Impact Graph 스키마 / API 설계안 (현재 active path 기준)

이 문서는 `CONSTRAINT_IMPACT_GRAPH_ACTIVE_BLUEPRINT.md`를 실제 구현으로 내리기 위한
**구체적인 Python 스키마 / 모듈 구조 / 함수 시그니처 / 추출 포인트**를 정의한다.

## 진행 상태 체크

- [x] `types.py`
- [x] `snapshot.py`
- [x] `snapshot_builders.py`
- [x] `atoms.py`
- [x] `rule_primitives.py`
- [x] `rule_compiler.py`
- [x] `rule_masks.py`
- [x] `nurse_state_machine.py` (v1)
- [ ] `graph_nodes.py`
- [ ] `graph_builder.py`
- [x] `simulation.py` (v1)
- [ ] `pressure.py`
- [ ] `explain.py`

### 현재 v1 구현 범위

- [x] 현재 roster 기준 `SemanticsSnapshot` 생성
- [x] 현재 roster 기준 `AssignmentAtom` 생성
- [x] 액션 delta 반영 (`SimulationAction`)
- [x] builtin/personal primitive rule compile
- [x] assignment-window primitive rule compile
- [x] primitive rule mask generation
- [x] `coverage` 평가
- [x] `preceptee_sync` 평가
- [x] `team_min` 평가
- [x] `grade(min 성격)` 평가
- [x] nurse-local `consecutive_work` 평가
- [x] nurse-local `consecutive_night` 평가
- [x] nurse-local `monthly_night_cap` 평가
- [x] `2N/3N 이후 recovery OFF` 파생 atom 생성
- [x] `issued/latest/blank` carryover artifact 수집
- [x] carryover artifact 기반 nurse state initialization
- [x] carryover artifact 기반 boundary transition ban 평가(`N->D`, `E->D`, `N->E`)
- [x] carryover artifact 기반 `recovery_debt` / `fatigue_score` seed
- [x] boundary first-day `recovery_debt` validation
- [x] fatigue risk warning state 생성
- [ ] typed hypergraph 기반 causal edge 추적
- [ ] assignment boundary `carries_state` obligation artifact
- [ ] override policy node화
- [ ] full solver parity harness

---

대상 경로는 다음 active path만 기준으로 한다.

- `app/services/roster_create_service.py::generate_roster_service`
- `app/services/roster_create_service.py::_run_cp_sat_basic`
- `app/services/cp_sat_basic.py::generate_roster_cp_sat`
- `app/services/cp_sat_basic.py::_build_full_model`
- `app/services/cp_sat/objective_terms.py::build_main_objective_terms`
- `app/services/cp_sat/feasibility_alerts.py::run_preflight_feasibility_alerts`
- `app/services/cp_sat/mid_feasibility.py::validate_mid_hard_feasibility`

---

## 1. 구현 위치(권장 패키지 구조)

신규 패키지:

```text
app/services/constraint_impact/
├── __init__.py
├── types.py
├── snapshot.py
├── snapshot_builders.py
├── atoms.py
├── nurse_state_machine.py
├── graph_nodes.py
├── graph_builder.py
├── simulation.py
├── pressure.py
└── explain.py
```

### 파일 역할

- `types.py`
  - 공통 Enum / Literal / small DTO
- `snapshot.py`
  - `SemanticsSnapshot` 계열 dataclass
- `snapshot_builders.py`
  - active path 입력으로 snapshot 추출
- `atoms.py`
  - `AssignmentAtom`, `DerivedAtom`
- `nurse_state_machine.py`
  - nurse-local hard sequence 계산
- `graph_nodes.py`
  - constraint node / edge DTO
- `graph_builder.py`
  - snapshot → typed hypergraph 생성
- `simulation.py`
  - delta simulation main entry
- `pressure.py`
  - slack/pressure 계산
- `explain.py`
  - causal chain / agent-facing explanation 생성

---

## 2. 타입 정의 (`types.py`)

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


ConstraintFamily = Literal[
    "coverage",
    "team_min",
    "grade_min",
    "grade_max",
    "handoff",
    "preceptee_sync",
    "transition_ban",
    "consecutive_work",
    "consecutive_night",
    "recovery_2n2o",
    "recovery_3n2o",
    "monthly_night_cap",
    "monthly_off_min",
    "monthly_off_max",
    "weekend_only",
    "cross_month_4off",
    "initial_forbidden",
    "off_window",
]


ConstraintMode = Literal[
    "enforced",
    "soft_fallback",
    "skipped_by_capacity",
    "bypassed_by_fixed",
    "inactive",
]


AtomSource = Literal[
    "solver",
    "fixed_wanted",
    "special_fixed",
    "weekly_off",
    "manual_fixed",
    "agent_action",
    "forced_by_rule",
    "preceptee_sync",
]


SolveAttemptLabel = Literal[
    "primary",
    "grade_max_retry",
]


SimulationSeverity = Literal[
    "ok",
    "warning",
    "hard_violation",
]
```

---

## 3. Snapshot 스키마 (`snapshot.py`)

### 3.1 Solve attempt 메타

```python
@dataclass(slots=True)
class SolveAttemptMeta:
    attempt_index: int
    label: SolveAttemptLabel
    grade_strategy: str
    forced_grade_soft_fallback: bool
    config_flags: dict[str, Any] = field(default_factory=dict)
```

`config_flags`에는 최소 아래 값이 들어가야 한다.

- `preceptee_on`
- `preceptee_shift_count`
- `use_mid`
- `off_first`
- `weekend_off_only_enable`
- `team_min_soft_fallback`
- `team_handoff_soft_fallback`
- `grade_allow_soft_fallback`
- `two_offs_after_two_nig`
- `two_offs_after_three_nig`
- `not_one_night`
- `ban_n_to_d`
- `ban_e_to_d`
- `ban_n_to_e`

### 3.2 고정 셀 / 고정 원티드

```python
@dataclass(slots=True)
class FixedCellFact:
    nurse_index: int
    nurse_id: str
    day_index: int
    shift_code_raw: str
    shift_code_main: str
    shift_type: str | None
    fixed_source: str
    counts_to_coverage: bool
```

`counts_to_coverage`는 아래 규칙으로 확정해야 한다.

- OFF 계열: `False`
- working fixed: 기본 `True`
- preceptee + `preceptee_shift_count=False`: `False` 가능
- `coverage_exclude_cells`에 포함되면 `False`

### 3.3 preceptee 사실

```python
@dataclass(slots=True)
class PrecepteeFact:
    nurse_index: int
    nurse_id: str
    preceptor_index: int | None
    preceptor_id: str | None
    follow_enabled: bool
    follow_days: set[int]
    full_month_default_follow: bool
    counts_to_coverage: bool
    fixed_wanted_override_days: set[int] = field(default_factory=set)
```

### 3.4 precheck 출력

```python
@dataclass(slots=True)
class PreflightAlertFact:
    source: Literal["feasibility_alerts", "mid_feasibility"]
    severity: Literal["warning", "blocking"]
    code: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
```

`feasibility_alerts.py`는 지금 문자열 alert만 반환하므로, v1에서는

- `code="preflight_alert"`
- `message=<원문>`

으로 저장하고,
나중에 정규 파서 추가하는 식이 현실적이다.

### 3.5 constraint mode fact

```python
@dataclass(slots=True)
class ConstraintModeFact:
    family: ConstraintFamily
    key: str
    configured_mode: str
    effective_mode: ConstraintMode
    source_file: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
```

예:

```python
ConstraintModeFact(
    family="team_min",
    key="team_min:T2:day=25:shift=D",
    configured_mode="hard",
    effective_mode="skipped_by_capacity",
    source_file="app/services/constraints/team_constraints.py",
    reason="active team members < min_t in hard mode",
    evidence={"active": 0, "min_t": 1},
)
```

### 3.6 메인 snapshot

```python
@dataclass(slots=True)
class SemanticsSnapshot:
    year: int
    month: int
    attempt: SolveAttemptMeta

    # orchestration input
    nurse_ids_in_scope: list[str]
    inbound_nurse_ids: list[str]
    fixed_cells: list[FixedCellFact]
    special_fixed_requests: list[dict[str, Any]]
    merged_initial_constraints: dict[str, Any]

    # runtime-derived facts
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

    preflight_alerts: list[PreflightAlertFact]
    mid_feasibility_error: str | None
    constraint_modes: list[ConstraintModeFact]
```

---

## 4. Atom 스키마 (`atoms.py`)

```python
@dataclass(slots=True)
class AssignmentAtom:
    atom_id: str
    nurse_index: int
    nurse_id: str
    day_index: int
    shift_code: str
    source: AtomSource
    is_fixed: bool
    is_forced: bool
    counts_to_coverage: bool
    override_reason: str | None
    coupled_unit_id: str | None
    created_by_action_id: str | None = None


@dataclass(slots=True)
class DerivedAtom:
    atom_id: str
    family: str  # forced_off | blocked_end_of_2n | blocked_end_of_3n | synced_from_preceptor
    nurse_index: int
    day_index: int
    shift_code: str
    caused_by: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
```

### 4.1 `atom_id` 규칙

```text
assign:{nurse_id}:{YYYY-MM-DD}:{shift}
```

### 4.2 `coupled_unit_id`

- 일반 nurse: `None`
- preceptor/preceptee unit:
  - `preceptor_unit:{preceptor_id}`

---

## 5. Nurse state machine API (`nurse_state_machine.py`)

```python
@dataclass(slots=True)
class NurseDayState:
    nurse_index: int
    day_index: int
    assigned_shift: str | None
    consecutive_work: int
    consecutive_nights: int
    recovery_off_required_after_day: int
    weekend_only_active: bool
    n_forbidden: bool
    off_count_nonvac_so_far: int
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NurseStateTransition:
    nurse_index: int
    from_day: int
    to_day: int
    trigger: str
    produced_atoms: list[DerivedAtom] = field(default_factory=list)
    blocked_constraints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
```

핵심 함수:

```python
def build_nurse_day_states(
    *,
    snapshot: SemanticsSnapshot,
    atoms_by_nurse_day: dict[tuple[int, int], AssignmentAtom],
) -> dict[int, list[NurseDayState]]:
    ...


def simulate_nurse_local_delta(
    *,
    snapshot: SemanticsSnapshot,
    nurse_index: int,
    changed_days: set[int],
    atoms_by_nurse_day: dict[tuple[int, int], AssignmentAtom],
) -> tuple[list[NurseStateTransition], list[DerivedAtom]]:
    ...
```

### 5.1 v1에서 상태기계가 반드시 다뤄야 하는 규칙

- `ban_n_to_d`
- `ban_e_to_d`
- `ban_n_to_e`
- `not_one_night`
- `two_offs_after_two_nig`
- `two_offs_after_three_nig`
- `max_consecutive_work_days`
- `max_consecutive_nights`
- `month night cap`
- `weekend_off_only`
- `cross_month_4off`
- `off_window_constraints`

### 5.2 v2로 확장할 규칙 (Graph 이전에 state machine 강화)

- `E→N`, `N→D`, `N→E` transition object화
- `NOD`, `NOE`, `EOD` 패턴 event화
- `isolated_off`를 fatigue/recovery 품질 상태로 승격
- `prefer_3n_block`를 단순 objective 항이 아니라 연속 N 블록 품질 지표로 승격
- 개인 피로도 누적값 추가

예시 확장 필드:

```python
@dataclass(slots=True)
class NurseDayState:
    nurse_index: int
    day_index: int
    assigned_shift: str | None
    consecutive_work: int
    consecutive_nights: int
    recovery_off_required_after_day: int
    weekend_only_active: bool
    n_forbidden: bool
    off_count_nonvac_so_far: int

    # v2 extension
    previous_shift: str | None
    transition_code: str | None      # E->N, N->D, N->E, N->O, O->N ...
    fatigue_score: float
    recovery_debt: float
    pattern_events: list[str]

    # assignment / visibility continuity
    state_visibility: str            # active | blocked | pre_join | post_leave | transferred_out | inbound_only
    assignment_owner_group: str | None
    assignment_window_type: str | None   # home | inbound | outbound | transfer
    transfer_state_carryover: bool
```

권장 누적 방식:

- `fatigue_score += transition_weight[prev_shift, curr_shift]`
- `fatigue_score += block_weight(consecutive_work, consecutive_nights)`
- `fatigue_score -= recovery_credit(OFF, vacation, weekend_off)`
- `recovery_debt += forced_recovery_need - granted_recovery_off`

즉 v2 state machine은
단순 `consecutive_work` 추적이 아니라,
**transition matrix + block length + recovery budget**를 같이 누적하는 구조가 적합하다.

### 5.3 추가로 반드시 고려할 state 축

#### A. 퇴사 / 휴직
- 현재 active path에서는 `join/leave`로 물리 월 범위를 잘라낸다.
- 따라서 state machine에서는 `post_leave` 상태를 따로 복잡하게 유지하기보다,
  **state visibility 차단**으로 다루는 것이 맞다.
- 단, 월중 퇴사 직전의 꼬리 상태는 다음 constraint 평가에 영향 주므로
  `last_visible_state`는 남겨야 한다.

#### B. 부서이동 / 파견
- 이건 단순 blocked가 아니다.
- 현재 코드베이스는 inbound nurse를 target group 생성에 포함하고,
  source schedule의 shift를 target schedule로 복사하는 로직도 가진다.
- 즉 target 병동에서 state를 새로 시작하는 것이 아니라,
  **source 쪽의 직전 shift / 연속근무 / 연속 N / recovery debt를 이어받아야 한다.**

그래서 state machine에 아래 개념이 필요하다.

- `assignment_window_type`
  - `home`
  - `inbound`
  - `outbound`
  - `transfer`
- `transfer_state_carryover`
  - target 병동에서 이전 병동의 마지막 visible state를 이어받는지 여부
- `assignment_owner_group`
  - 해당 day의 소유 병동

#### C. blocked day와 invisible day 구분
- 지금 `blocked_by_nurse`는 active-day 바깥으로 잘라내는 데 쓰이지만,
  state 모델에서는 아래를 나눠야 한다.
  - 제약상 계산 제외되는 날
  - ownership만 다른 날
  - 실제 근무가 source 병동에 존재하는 날

즉 `blocked`와 `not-visible-here`는 같은 게 아니다.

#### D. 개인별 quota / 요일 허용 규칙

다음 류의 규칙은 앞으로 충분히 들어올 수 있다.

- `N은 딱 2개만`
- `OFF는 정확히 11개`
- `Evening(E)은 주말에만`
- `수요일은 D, O만`

이런 규칙은 **전부 nurse-local이긴 하지만, 전부 state 하나로 끝내면 안 된다.**

권장 분리:

1. **State machine이 갖는 것**
   - 누적 count
     - `shift_count_by_code`
     - `off_count_nonvac`
   - 요일 축 정보
     - `weekday`
     - `is_weekend`
   - 금일 허용 shift 후보 마스크
     - `allowed_today_by_profile`
     - `allowed_today_by_calendar_rule`

2. **Constraint node가 판정하는 것**
   - `ExactShiftCount(nurse, shift, target)`
   - `MinMaxShiftCount(nurse, shift, min, max)`
   - `AllowedWeekdayShift(nurse, weekday, allowed_set)`
   - `WeekendOnlyShift(nurse, shift)`

즉 구현 철학은:

- **State**: “현재까지 몇 번 했는가 / 오늘 어떤 캘린더 문맥인가”
- **Node**: “그 누적/문맥이 제약을 위반했는가”

예시 확장 필드:

```python
@dataclass(slots=True)
class NurseDayState:
    ...
    shift_count_by_code: dict[str, int]
    weekday_index: int
    is_weekend: bool
    allowed_today_by_calendar_rule: set[str]
```

예시 personal node:

```python
ConstraintNode(
    family="personal_shift_rule",
    node_id="weekday_rule:nurse=445872:wed:D|O",
    operator="subset",
    target={"D", "O"},
    ...
)
```

### 5.4 요일 규칙 표현의 통일 원칙

`주말 전용` 같은 별도 개념을 남기기보다,
내부 canonical form은 **요일 × 시프트 금지 규칙(`DisallowedShift`) 하나로 통일**하는 것이 가장 좋다.

권장 내부 모델:

```python
@dataclass(slots=True)
class DisallowedWeekdayShiftRule:
    nurse_id: str
    weekdays: set[int]          # 0=Mon ... 6=Sun
    shift_codes: set[str]       # {"D", "E", "N", "O", "M"}
    source: str                 # profile | personal_rule | transfer_overlay | generated
    reason: str | None = None
```

예시:

1. `수요일은 D,O만`

```python
DisallowedWeekdayShiftRule(
    nurse_id="445872",
    weekdays={2},
    shift_codes={"E", "N", "M"},
    source="personal_rule",
)
```

2. `Evening은 주말에만`

```python
DisallowedWeekdayShiftRule(
    nurse_id="445872",
    weekdays={0,1,2,3,4},
    shift_codes={"E"},
    source="personal_rule",
)
```

즉 네가 말한

`disallowedshift(n, mon, ['E']) ... disallowedshift(n, fri, ['E'])`

는 방향이 맞고,
실제로는 weekday들을 set으로 묶어 한 rule로 저장하는 것이 더 낫다.

### 5.5 "주말"을 완전히 없애도 되나?

#### A. 대부분의 shift 허용/금지 규칙은 가능

- `E는 주말만`
- `수요일은 D,O만`
- `월/화는 N 금지`
- `금요일은 M 금지`

이런 건 전부 `DisallowedWeekdayShiftRule`로 커버 가능하다.

#### B. 하지만 현재 `weekend_off_only`는 단일 DisallowedShift 하나로는 끝나지 않는다

현재 active code의 `weekend_off_only`는 단순히

`weekday에는 O 금지`

만 하는 게 아니라,

- 주말 OFF 강제
- off bounds 계산 시 weekend slot 수를 사용
- feasibility_alerts에서 `min_off_required > weekend_slots` 체크

까지 포함한다.

즉 현재 의미는

```text
1) weekday에는 O 금지
2) weekend에는 O 강제
3) OFF 개수/상한 계산도 weekend slot 기반으로 재해석
```

이 때문에 `weekend_off_only`를 CPSAT 엔진 안의 별도 special-case로 남기기보다,
동일 rule framework 안에서 **여러 개의 정규화된 rule/fact bundle** 로 컴파일하는 것이 낫다.

예:

- `DisallowedWeekdayShiftRule(n, weekdays={0,1,2,3,4}, shift_codes={"O"})`
- `RequiredWeekdayShiftRule(n, weekdays={5,6}, shift_codes={"O"})` 또는 동등 hard fact
- `ShiftCountBoundRule(n, shift="O", ...)` 재계산 fact

즉 `주말`은 내부 primitive가 아니라 `weekdays={5,6}` 수준의 syntax sugar로 낮추되,
현재 `weekend_off_only` 전체 의미는 **금지 + 강제 + 수량경계** 로 분해해서 같은 제약 체계 안에 둬야 한다.

중요:

- `weekend_off_only`를 엔진 내부 if-branch로 따로 유지하면
  - 개인별 `shift count`
  - 개인별 `weekday shift`
  - 개인별 `required shift`
  와 제약 표현 체계가 분리되어 결합도가 올라간다.
- 반대로 `weekend_off_only`를 **rule bundle** 로 컴파일하면,
  새로 추가할 개인 규칙들(`N은 2개`, `수요일 D/O만`, `E는 주말만`)과
  동일한 파이프라인으로 처리할 수 있다.

권장 구조:

1. 사용자/설정 입력 레벨
   - `weekend_off_only`
   - `monthly_shift_count_rules`
   - `weekday_shift_rules`

2. 내부 compile 레벨
   - `DisallowedWeekdayShiftRule[]`
   - `RequiredWeekdayShiftRule[]`
   - `ShiftCountBoundRule[]`

3. CPSAT build 레벨
   - 금지 마스크 적용
   - required shift 적용
   - count bound 적용

즉 핵심은:

> `weekend_off_only`도 “별도 엔진 규칙”이 아니라 “여러 primitive rule로 컴파일되는 입력 규칙”으로 취급하는 것

이렇게 해야 개인별 월 shift 개수 설정, 특정 요일 shift 규칙과 완전히 같은 프레임에서 다룰 수 있다.

---

### 5.6 CP-SAT 조건식으로도 바꿀 수 있나?

가능하다. 오히려 관리가 더 쉬워질 수 있다.

#### 현재 방식

- `is_weekend_off`
- `ban_n_to_d`
- profile allowed shift
- weekly_off_map

등이 여러 곳에서 분기적으로 하드코딩되어 있다.

#### 추천 변환 방식

1. 사전 컴파일 단계에서 day별 rule을 펼친다.
2. `(nurse, day)`마다 disallowed shift 집합을 만든다.
3. CP-SAT에는 공통 형태로 넣는다.

공통형:

```python
for s_idx in disallowed_shift_indices[n][d]:
    m.Add(X(n, d, s_idx) == 0)
```

즉 solver에는 결국

**"그 날 금지된 시프트는 0"**

의 형태로 동일하게 내려간다.

### 5.7 추천 구현 철학

1. 외부 DSL / API 표현
   - 가능하면 `disallowed_weekday_shift` 중심으로 설계
   - `allowed_*`를 받더라도 complement 계산 후 내부에서 `disallowed`로 정규화

2. 내부 정규 표현
   - 전부 `disallowed_shift_indices[n][d]` 로 컴파일

3. 예외/보강
   - `weekend_off_only`, `weekly_off`, `off bounds`는
     별도 왕국으로 분리하지 않고,
     같은 rule framework 안의 추가 node/fact로 분해
   - 단, 구현 시에도 가능하면 `weekend_off_only` 전용 if/else를 최소화하고
     rule compiler가 primitive rule bundle로 변환한 결과만 CPSAT builder가 소비하게 한다

즉,

- **내부 표현은 DisallowedShift 하나로 통일하는 게 맞다**
- **solver 반영도 공통식으로 통일 가능하다**
- 단, `weekend_off_only` 전체 의미는 금지 규칙 하나가 아니라 추가 constraint fact가 더 필요하다

---

### 5.8 Primitive-only Rule DSL (최종 권장안)

이제부터 engine/storage/canonical layer는 아래 3개 primitive만 사용한다.

```python
@dataclass(slots=True)
class DisallowedShiftRule:
    nurse_id: str
    day_indices: set[int]            # 0-based physical day index set
    shift_codes: set[str]            # {"D", "E", "N", "O", "M"}
    source: str                      # profile | personal_rule | transfer_overlay | generated
    reason: str | None = None


@dataclass(slots=True)
class RequiredShiftRule:
    nurse_id: str
    day_indices: set[int]
    shift_codes: set[str]            # 허용 후보가 아니라 "반드시 이 집합 중 하나여야 함"
    min_required: int = 1            # 보통 1
    source: str = "generated"
    reason: str | None = None


@dataclass(slots=True)
class ShiftCountBoundRule:
    nurse_id: str
    shift_code: str
    min_count: int | None = None
    max_count: int | None = None
    exact_count: int | None = None   # exact 설정 시 min/max보다 우선
    scope_day_indices: set[int] | None = None   # None이면 해당 월 전체 active visible day
    source: str = "generated"
    reason: str | None = None
```

설계 원칙:

- `weekday`, `weekend`, `monthly quota`, `special profile` 같은 상위 의미는 전부 compiler 앞단의 해석 문제다.
- canonical 저장은 오직 위 3종만 허용한다.
- Graph / State / CPSAT builder는 이 3종만 이해하면 된다.

### 5.9 User/LLM 표현 → Primitive compile 예시

#### A. `수요일은 D,O만`

```python
DisallowedShiftRule(
    nurse_id="445872",
    day_indices={all_wednesdays_of_month...},
    shift_codes={"E", "N", "M"},
    source="personal_rule",
    reason="수요일은 D/O만",
)
```

#### B. `E는 주말에만`

```python
DisallowedShiftRule(
    nurse_id="445872",
    day_indices={all_mon_to_fri_of_month...},
    shift_codes={"E"},
    source="personal_rule",
    reason="E는 주말에만",
)
```

#### C. `N은 딱 2개`

```python
ShiftCountBoundRule(
    nurse_id="445872",
    shift_code="N",
    exact_count=2,
    source="personal_rule",
    reason="N은 정확히 2개",
)
```

#### D. `OFF는 11~13개`

```python
ShiftCountBoundRule(
    nurse_id="445872",
    shift_code="O",
    min_count=11,
    max_count=13,
    source="personal_rule",
    reason="OFF 범위",
)
```

#### E. 기존 `weekend_off_only`

```python
DisallowedShiftRule(
    nurse_id="445872",
    day_indices={all_weekdays_of_month...},
    shift_codes={"O"},
    source="generated",
    reason="weekend_off_only: weekday O 금지",
)

RequiredShiftRule(
    nurse_id="445872",
    day_indices={all_weekends_of_month...},
    shift_codes={"O"},
    min_required=1,
    source="generated",
    reason="weekend_off_only: weekend O 요구",
)

ShiftCountBoundRule(
    nurse_id="445872",
    shift_code="O",
    min_count=<computed_min>,
    max_count=<computed_max>,
    source="generated",
    reason="weekend_off_only: off bounds 재계산",
)
```

즉 `weekend_off_only`도 더 이상 별도 엔진 규칙이 아니라,
primitive 3종으로 compile된 결과물일 뿐이다.

#### F. `17일까지는 교육, 18일부터는 D 가능`

이 케이스는 primitive 체계로 아주 자연스럽게 표현 가능하다.

예를 들어 교육기간 중에는 `D/E/N/M` 금지, 이후에는 `E/N/M` 금지라고 하면:

```python
DisallowedShiftRule(
    nurse_id="445872",
    day_indices={0,1,2,...,16},
    shift_codes={"D", "E", "N", "M"},
    source="generated",
    reason="교육기간: 근무 금지",
)

DisallowedShiftRule(
    nurse_id="445872",
    day_indices={17,18,19,...,29},
    shift_codes={"E", "N", "M"},
    source="generated",
    reason="교육 종료 후 D/O만 허용",
)
```

즉 핵심은:

- `기간별 상태 변화`를 별도 특수개념으로 만들기보다
- **서로 다른 day scope를 가진 primitive rule 조각들**로 쪼개는 것

이 패턴은 아래에 모두 적용 가능하다.

- 프리셉티 초반 교육기간
- 기간 파견근무
- 특정 날짜 이후 단독 근무 가능
- 특정 날짜 전까지 N 금지

#### G. `기간 파견근무 + shift 가능범위 변경` 결합

이것도 가능하다.

예:

- 1~17일: source 병동 교육, target 병동에서는 근무 불가
- 18~30일: target 병동 D/O만 가능

그러면 target 병동 관점에서는:

```python
DisallowedShiftRule(
    nurse_id="445872",
    day_indices={0,1,2,...,16},
    shift_codes={"D", "E", "N", "M", "O"},
    source="transfer_overlay",
    reason="target 병동 관점: inbound 시작 전 invisible",
)

DisallowedShiftRule(
    nurse_id="445872",
    day_indices={17,18,19,...,29},
    shift_codes={"E", "N", "M"},
    source="transfer_overlay",
    reason="target 병동 관점: 기간파견 중 D/O만 허용",
)
```

여기서 첫 rule은 사실상 “이 기간에는 target 병동에서 어떤 shift도 가질 수 없음”을 뜻한다.

### 5.9.1 중요한 경계: rule만으로 충분한 것 vs state carryover가 필요한 것

이런 period-scoped shift availability는 **rule layer만으로 충분히 표현 가능**하다.

하지만 아래는 여전히 state layer가 필요하다.

- 17일까지 연속근무가 4일 누적되었고
- 18일부터 target 병동에서 이어서 근무할 때
- `consecutive_work`, `consecutive_nights`, `recovery_debt`, `fatigue_score`
  를 계속 이어받아야 하는 문제

즉 정리하면:

- **"어떤 shift가 가능한가"** → primitive rules로 표현
- **"이전 기간의 근무 상태가 이어지는가"** → state carryover로 표현

둘은 같이 필요하다.

### 5.10 Compiler 파이프라인

권장 모듈:

```text
constraint_impact/
  rule_primitives.py
  rule_compiler.py
  rule_masks.py
```

권장 함수:

```python
def compile_personal_rules(
    *,
    snapshot: SemanticsSnapshot,
    user_rules: list[dict],
) -> tuple[
    list[DisallowedShiftRule],
    list[RequiredShiftRule],
    list[ShiftCountBoundRule],
]:
    ...


def compile_builtin_rules(
    *,
    snapshot: SemanticsSnapshot,
) -> tuple[
    list[DisallowedShiftRule],
    list[RequiredShiftRule],
    list[ShiftCountBoundRule],
]:
    ...


def merge_rule_bundles(
    *,
    bundles: list[tuple[list[DisallowedShiftRule], list[RequiredShiftRule], list[ShiftCountBoundRule]]],
) -> tuple[list[DisallowedShiftRule], list[RequiredShiftRule], list[ShiftCountBoundRule]]:
    ...
```

단계:

1. profile / fixed / assignment overlay / special config 읽기
2. user/LLM 표현(rule intent) 읽기
3. 전부 primitive 3종으로 compile
4. nurse/day 단위로 펼쳐서 mask/index 생성
5. CPSAT builder / state machine / graph builder가 공통 사용

### 5.11 CPSAT builder 연결 방식

최종 compile 결과는 아래 3개 구조로 다시 압축하는 것이 좋다.

```python
disallowed_shift_indices: dict[tuple[int, int], set[int]]
required_shift_indices: dict[tuple[int, int], tuple[set[int], int]]
shift_count_bounds: dict[tuple[int, str], dict[str, int | None]]
```

예시 적용:

```python
for (n, d), blocked in disallowed_shift_indices.items():
    for s_idx in blocked:
        m.Add(X(n, d, s_idx) == 0)

for (n, d), (allowed_set, min_required) in required_shift_indices.items():
    m.Add(sum(X(n, d, s_idx) for s_idx in allowed_set) >= min_required)

for (n, shift_code), bounds in shift_count_bounds.items():
    s_idx = shift_code_to_index[shift_code]
    scope_days = bounds["scope_days"]
    expr = sum(X(n, d, s_idx) for d in scope_days)
    if bounds.get("exact_count") is not None:
        m.Add(expr == bounds["exact_count"])
    else:
        if bounds.get("min_count") is not None:
            m.Add(expr >= bounds["min_count"])
        if bounds.get("max_count") is not None:
            m.Add(expr <= bounds["max_count"])
```

### 5.12 이 설계에서의 장점

1. `weekend`, `weekday`, `monthly`, `profile` 같은 도메인 용어가 engine core에서 사라진다.
2. 규칙이 늘어나도 primitive type은 3개라 복잡도 성장이 느리다.
3. State Machine은 primitive rule의 day-level 결과만 보고 누적하면 된다.
4. Graph도 primitive rule node만 연결하면 되어 설명력이 좋아진다.
5. CPSAT builder는 primitive masks/bounds만 소비하므로 결합도가 낮다.

### 5.13 아직 사용자 확인이 필요한 진짜 open question

현재 시점에서 유일하게 나중에 확인이 필요할 수 있는 건 **RequiredShiftRule의 정확한 의미**다.

예:
- `shift_codes={"O"}, min_required=1` 이면 사실상 `O 강제`
- `shift_codes={"D","O"}, min_required=1` 이면 `D 또는 O 허용/요구`

즉 RequiredShiftRule을

- “이 집합 중 하나는 반드시 선택”
로만 둘지,
- 또는 exact-one semantics까지 확장할지는
실제 개인 규칙 UI/요구사항이 들어올 때 최종 확정하면 된다.

현재 설계상으로는 **min_required=1의 disjunctive requirement** 로 두는 게 가장 안전하다.

---

### 5.14 Carryover State Artifact (다음 단계 핵심)

assignment / training / transfer 규칙을 primitive rule로 compile하는 것만으로는 충분하지 않다.

왜냐하면:

- 어떤 날에 어떤 shift가 가능한지는 rule로 표현 가능하지만,
- **경계일 직전까지 누적된 근무 상태**
  (`previous_shift`, `consecutive_work`, `consecutive_nights`, `recovery_debt`, `fatigue_score`)
  는 별도 artifact로 이어받아야 하기 때문이다.

권장 artifact:

```python
@dataclass(slots=True)
class CarryoverStateArtifact:
    nurse_id: str
    direction: Literal["inbound", "outbound", "transfer", "training"]
    boundary_day_index: int
    reference_group_id: str | None
    selected_schedule_id: str | None
    selected_schedule_basis: Literal["issued", "latest", "blank"]
    carries_state: bool
    tail_sequence: list[str]
    metrics: dict[str, Any]
    metadata: dict[str, Any]
```

### 5.15 Source/Target schedule 선택 정책 (현재 active path와 동일)

carryover artifact는 **파견 로직이 이미 사용하는 우선순위와 동일한 정책**을 따라야 한다.

#### A. 참조 schedule 우선순위

`_query_schedule_ref_for_month()` 기준:

1. `status='issued'` 스케줄이 있으면 → `issued`
2. 없으면 최신 version → `latest`
3. 둘 다 없으면 → `blank`

즉 artifact의 `selected_schedule_basis`는 반드시 아래 중 하나여야 한다.

- `issued`
- `latest`
- `blank`

#### B. inbound / transfer 시작 경계

- target 병동에서 먼저 생성할 때
- source 병동 같은 달 근무표를 참조
- source 근무표가 없으면 `blank`

즉:

- source 근무표 있음 → 그 tail 기준으로 state carryover
- source 근무표 없음 → 백지 상태에서 시작

#### C. outbound 복귀 경계

- source 병동으로 복귀할 때
- 직전 target 병동 근무표를 참조
- target 근무표가 없으면 `blank`

### 5.16 현재 구현 범위

지금 구현된 것:

- `roster_create_service.py`가 active assignments에서
  - `AssignmentWindowFact`
  - `CarryoverStateArtifact`
  를 만들어 `roster_system`에 attach
- `SemanticsSnapshot`이 이 artifact를 수집
- carryover artifact는 schedule 선택 기준(`issued/latest/blank`)과 tail sequence/metrics를 포함
- `NurseStateMachine`이 carryover artifact를 이용해
  - `consecutive_work`
  - `consecutive_nights`
  - `previous_shift`
  - `fatigue_score`
  - `recovery_debt`
  를 boundary day 초기 상태로 seed
- simulation이 carryover 기반
  - first-day transition ban
  - first-day recovery debt hard violation
  - fatigue risk warning
  을 평가

아직 미구현인 것:

- carryover artifact 간 충돌 검증
- graph node / graph state로 승격하는 단계
- fatigue/debt의 horizon-wide graph propagation

즉 현재 상태는:

- **carryover policy 기록/노출은 됨**
- **carryover state enforcement도 boundary day 수준까지는 됨**
- **다만 graph propagation은 아직 다음 단계**

---

## 6. Constraint graph node 스키마 (`graph_nodes.py`)

```python
@dataclass(slots=True)
class ConstraintNode:
    node_id: str
    family: ConstraintFamily
    mode: ConstraintMode
    scope: dict[str, Any]
    operator: str  # >=, <=, ==, implication, equality
    target: Any
    related_atom_ids: list[str]
    explanation_template: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConstraintEvaluation:
    node_id: str
    valid: bool
    slack: int | float | None
    pressure: int | float | None
    details: dict[str, Any] = field(default_factory=dict)
```

---

## 7. Graph builder API (`graph_builder.py`)

```python
def build_constraint_nodes(
    *,
    snapshot: SemanticsSnapshot,
    atoms: list[AssignmentAtom],
) -> list[ConstraintNode]:
    ...


def index_nodes_by_atom(
    nodes: list[ConstraintNode],
) -> dict[str, list[str]]:
    ...
```

### 7.1 Node family별 최소 builder

v1에서 반드시 만드는 node:

- `CoverageMin`
- `CoverageMax`
- `TeamMin`
- `GradeMin`
- `GradeMax`
- `PrecepteeSync`
- `ConsecutiveWorkWindow`
- `TransitionBan`
- `Recovery2N2O`
- `Recovery3N2O`
- `MonthlyOffMax`
- `MonthlyOffMin`
- `WeekendOnly`

---

## 8. Simulation API (`simulation.py`)

```python
@dataclass(slots=True)
class SimulationAction:
    action_id: str
    type: str  # assign_shift | set_fixed_wanted | add_off | remove_fixed ...
    assignments: list[dict[str, Any]]
    source: str  # user | agent | system
    reason: str | None = None


@dataclass(slots=True)
class SimulationResult:
    action_id: str
    valid_under_current_semantics: bool
    severity: SimulationSeverity
    changed_atom_ids: list[str]
    triggered_forced_atoms: list[DerivedAtom]
    violated_constraints: list[ConstraintEvaluation]
    risky_constraints: list[ConstraintEvaluation]
    causal_chain: list[str]
    notes: list[str]
```

핵심 함수:

```python
def simulate_action(
    *,
    snapshot: SemanticsSnapshot,
    current_atoms: list[AssignmentAtom],
    action: SimulationAction,
) -> SimulationResult:
    ...
```

### 8.1 반환 규칙

`valid_under_current_semantics`는 **현재 attempt semantics 기준**이다.

즉:

- primary attempt에서 invalid
- grade max retry semantics에서는 valid

일 수 있다.

그래서 추후 v2에서는 아래 확장이 필요하다.

```python
@dataclass(slots=True)
class MultiAttemptSimulationResult:
    primary: SimulationResult
    grade_max_retry: SimulationResult | None
```

---

## 9. Snapshot builder API (`snapshot_builders.py`)

### 9.1 가장 중요한 함수

```python
def build_semantics_snapshot_from_active_path(
    *,
    db,
    current_user,
    req,
    latest_config,
    nurses_in_group,
    preferences,
    shift_manage_data,
    fixed_cells: list[dict[str, Any]] | None,
    config_override: dict[str, Any] | None = None,
    attempt_index: int = 0,
    attempt_label: SolveAttemptLabel = "primary",
) -> SemanticsSnapshot:
    ...
```

이 함수는 **새 solver를 만들지 않고**, 현재 `_run_cp_sat_basic`에서 생성되는 의미를 정규화하는 역할만 해야 한다.

### 9.2 내부 helper 함수들

```python
def _extract_orchestration_inputs(...) -> dict[str, Any]:
    ...

def _extract_fixed_cell_facts(...) -> list[FixedCellFact]:
    ...

def _extract_preflight_alerts(...) -> list[PreflightAlertFact]:
    ...

def _extract_mid_feasibility(...) -> str | None:
    ...

def _extract_runtime_semantics_from_rs(rs) -> dict[str, Any]:
    ...

def _resolve_constraint_modes(snapshot: SemanticsSnapshot) -> list[ConstraintModeFact]:
    ...
```

---

## 10. 필드별 실제 추출 포인트 매핑

| Snapshot 필드 | 추출 위치 | 비고 |
|---|---|---|
| `nurse_ids_in_scope` | `roster_create_service.py` nurse scope 구성부 | inbound 포함 전/후 구분 필요 |
| `inbound_nurse_ids` | `roster_create_service.py` 128~145 부근 | fixed_wanted 조회용 확장 |
| `fixed_cells` | `roster_create_service.py` 4363~4494 부근 + engine input | weekly off / special / fixed_wanted 합쳐짐 |
| `special_fixed_requests` | `_collect_nurses_and_preferences` 결과 | 특수 근무 hard fixed 원천 |
| `merged_initial_constraints` | `_run_cp_sat_basic` 2724~2728 부근 | `cross_month` + `allowed` 병합 결과 |
| `preflight_alerts` | `feasibility_alerts.py::run_preflight_feasibility_alerts` | 문자열 alert를 fact로 wrapping |
| `mid_feasibility_error` | `mid_feasibility.py::validate_mid_hard_feasibility` | blocking error |
| `join`, `leave` | `cp_sat_basic.py::_build_full_model` 2294~2320 | 룩어헤드 extension 포함 |
| `fixed_wanted_cells` | `cp_sat_basic.py::_build_full_model` 2355~2378 | `fixed_source == fixed_wanted` |
| `fixed_type_by_cell` | 같은 구간 | `shift_type` 우선 |
| `vacation_off_cells`, `structural_off_cells` | `build_off_partitions(...)` 호출 결과 | runtime meaning 핵심 |
| `off_exception_*` | `cp_sat_basic.py` 2280~2283 | OFF semantics 영향 |
| `n_forbid_n` | `_n_forbid_n_set` / `cp_sat_basic.py` 2426~2439 | N 관련 hard 분기 |
| `preceptee_facts` | `cp_sat_basic.py` 2463~2506, 2760~2808 | follow + coverage count 분리 |
| `coverage_exclude_cells` | `cp_sat_basic.py` 2820~2860 | coverage 평가에 직접 영향 |
| `team_min_soft_fallback` | `cp_sat_basic.py` config build 607~610 | snapshot flag |
| `team_handoff_soft_fallback` | `cp_sat_basic.py` config build 611~614 | snapshot flag |
| `grade_allow_soft_fallback` | `_run_cp_sat_basic` 2793~2796 + grade config | retry lineage와 연결 |

---

## 11. v1 구현 범위와 비범위

### v1 포함

- snapshot builder
- atom 생성
- nurse-local sequence simulation
- coverage/team/grade/preceptee 핵심 node 생성
- direct violation + risky pressure 계산

### v1 제외

- full unsat core / IIS
- 전 solver variant 대응
- 완전한 global infeasibility 증명
- 모든 soft objective 항목에 대한 fine-grained simulation

---

## 12. 구현시 주의사항

1. **현재 active path를 재현하되, solver를 복제하지 말 것**
2. **skip/bypass를 숨기지 말고 fact로 드러낼 것**
3. **retry lineage(primary / grade_max_retry)를 별도 attempt로 저장할 것**
4. **`roster_system._find_violations()`를 canonical checker로 승격시키지 말 것**
5. **fixed_wanted를 global override 하나로 축약하지 말 것**

---

## 13. 바로 다음 구현 작업

이 설계 다음 단계로는 아래 순서가 가장 적합하다.

1. `types.py`, `snapshot.py` dataclass 정의
2. `snapshot_builders.py::build_semantics_snapshot_from_active_path()` 골격 구현
3. primary attempt snapshot 생성
4. grade retry attempt snapshot 생성
5. `AssignmentAtom` 생성기 구현
6. nurse state machine v1 구현
7. coverage/team/grade/preceptee graph node builder 구현
