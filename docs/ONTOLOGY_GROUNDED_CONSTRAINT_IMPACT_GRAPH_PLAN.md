# Ontology-grounded Constraint Impact Graph 설계안

## 1. 왜 이 문서가 필요한가

현재 코드베이스에는 제약 의미가 이미 많이 존재한다.

- `roster_create_service.py`
  - cross-month / mid-month boundary 제약 생성
  - assignment overlay
  - fixed wanted / special fixed / 초기 제약 병합
- `cp_sat_basic.py`
  - 실제 hard 제약 적용
  - fixed 우선 / bypass / skip / off cap / night recovery / preceptee 관련 의미
- `services/constraints/*.py`
  - team / grade / handoff family
- `services/precheck/team_grade_precheck.py`
  - deterministic infeasibility reason code
- `services/cp_sat/feasibility_alerts.py`, `mid_feasibility.py`
  - pre-solver hard alert / feasibility 진단
- `wanted_service.py`
  - fixed wanted / assignment overlay / weekly off overlay
- `assignment_service.py`
  - inbound / outbound / transfer / target_* overlay

문제는 이 의미들이 **한 vocabulary로 통일되어 있지 않다**는 점이다.

따라서 agent/debugger/impact-graph를 강화하려면 solver를 새로 만드는 것이 아니라,
현재 runtime semantics 위에 **의미 체계(ontology)** 를 얹는 것이 가장 효과적이다.

이 문서는 그 ontology를 **가볍게(lightweight)** 추가하는 설계를 정리한다.

---

## 2. 핵심 결론

이 코드베이스에서 ontology는 다음 역할로만 도입한다.

```text
Semantics Snapshot
  = 이번 실행에서 실제로 어떤 제약/우회/skip/fallback이 일어났는가

Ontology
  = 그 제약 family / override / mode가 무슨 의미를 가지는가

Hypergraph
  = 특정 실행 시점에 어떤 atom과 constraint instance가 연결되는가

Agent / Debugger
  = snapshot + ontology + hypergraph를 이용해 hard conflict를 설명한다
```

즉 ontology는:

- solver 대체 아님
- feasible/infeasible 판정기 아님
- runtime semantics보다 앞서는 진실 소스 아님

반대로 ontology는:

- 의미 정규화
- 제약 family 분류
- override 의미 명시
- explanation template 제공
- graph node typing 제공

에만 집중한다.

---

## 3. 이번 브랜치에서의 현실적 전제

중요한 점: 이 브랜치 현재 상태에서는 우리가 이전 작업에서 상정했던
`app/services/constraint_impact/*` 소스 파일이 직접 보이지 않는다.

즉 ontology는 지금 당장 아래 서비스 seam에 붙이는 것이 맞다.

### 3.1 1차 attach point

1. `app/services/roster_create_service.py`
2. `app/services/cp_sat_basic.py`
3. `app/services/constraints/team_constraints.py`
4. `app/services/constraints/grade_constraints.py`
5. `app/services/constraints/team_grade_handoff_constraints.py`
6. `app/services/precheck/team_grade_precheck.py`
7. `app/services/cp_sat/feasibility_alerts.py`
8. `app/services/cp_sat/mid_feasibility.py`
9. `app/services/wanted_service.py`
10. `app/services/assignment_service.py`

### 3.2 왜 이 seam이 좋은가

- 이미 제약 family 의미가 여기서 만들어짐
- config / request / assignment overlay / fixed wanted / precheck / solver가 모두 여기서 만남
- CP-SAT 코어를 크게 흔들지 않고 metadata를 붙일 수 있음

---

## 4. 추천 아키텍처: Lightweight Ontology

### 4.1 하지 않을 것

다음은 1차 범위에서 제외한다.

- OWL
- RDF
- Triple Store
- SPARQL
- Reasoner
- Protege 기반 운영

### 4.2 할 것

새 패키지:

```text
app/services/semantics/
  ontology.yaml
  ontology.py
  ontology_attach.py
```

구조:

```text
YAML ontology catalog
    ↓
Python registry / resolver
    ↓
runtime facts / diagnostics / graph node metadata 에 attach
```

---

## 5. Ontology의 범위

### 5.1 Constraint family ontology

최초 버전은 아래 family만 정의한다.

#### CoverageConstraint 계열
- `CoverageMin`
- `CoverageMax`
- `TeamMin`
- `GradeMin`
- `GradeMax`
- `TeamGradeHandoff`

#### NurseLocalConstraint 계열
- `ConsecutiveWorkLimit`
- `ConsecutiveNightLimit`
- `NightRecovery`
- `NoSingleNight`
- `OffCap`
- `WeekendOffOnly`
- `BoundaryTransitionBan`

#### CouplingConstraint 계열
- `PrecepteeSync`
- `AssignmentWindow`
- `CarryoverBoundary`

#### MetaConstraint 계열
- `ConfigIntegrity`

#### OverridePolicy 계열
- `FixedWanted`
- `ManualFixed`
- `InitialForbiddenOverride`

#### MetaConstraintMode 계열
- `enforced`
- `soft_fallback`
- `skipped_by_capacity`
- `bypassed_by_fixed`
- `inactive`
- `precheck_blocked`

### 5.2 Override semantics

가장 중요한 것은 `FixedWanted` 의미를 정규화하는 것이다.

예:

```yaml
overrides:
  FixedWanted:
    parent: OverridePolicy
    bypasses:
      - TransitionBan
      - InitialForbidden
      - ProfileShiftLimit
    does_not_bypass:
      - ConsecutiveWorkLimit
      - OneShiftPerDay
    conditional_effects:
      blocks_recovery_slot: true
      counts_to_coverage: policy_dependent
      affects_preceptee_sync: policy_dependent
```

이 정의가 있어야 agent가
“fixed_wanted니까 다 무시 가능”
같은 잘못된 추론을 하지 않게 된다.

---

## 6. 추천 파일 구조

### 6.1 `ontology.yaml`

이 파일은 **권위 있는 의미 카탈로그**다. runtime fact를 저장하는 곳이 아니라,
constraint family / mode / override 의미를 정의하는 schema-level 파일이다.

#### 최종 스키마 규칙

최상위 키는 아래 4개로 고정한다.

```yaml
version: 1
constraints: {}
overrides: {}
modes: {}
relations: {}
```

#### 각 constraint 항목의 필수 필드

```yaml
constraints:
  <ConstraintId>:
    label: str
    parent: str
    scope: [str, ...]
    effective_modes: [str, ...]
    connects: [str, ...]
    explanation_template: str
```

#### 선택 필드

```yaml
    produces: [str, ...]              # forced_off, carryover_state, risk_flag 등
    consumes: [str, ...]              # previous_shift, tail_sequence 등
    aliases: [str, ...]               # 기존 코드/문서 표현 호환용
    default_severity: hard|soft|risk
    notes: str
```

#### 각 override 항목의 필수 필드

```yaml
overrides:
  <OverrideId>:
    label: str
    parent: str
    bypasses: [str, ...]
    does_not_bypass: [str, ...]
```

#### 각 mode 항목의 필수 필드

```yaml
modes:
  <ModeId>:
    label: str
    meaning: str
    is_enforced: bool
    severity: hard|soft|risk|none
```

#### relations 항목

이 항목은 ontology 자체의 정적 relation vocabulary를 담는다.

```yaml
relations:
  applies_to:
    source: Constraint
    target: RuntimeFact
  bypasses:
    source: OverridePolicy
    target: Constraint
  cannot_bypass:
    source: OverridePolicy
    target: Constraint
  carries_state:
    source: AssignmentWindow
    target: CarryoverBoundary
```

#### Constraint ID 네이밍 규칙

- PascalCase 유지
- family/class 단위 이름으로 정의
- runtime instance key와 분리

예:

- `TeamMin`
- `GradeMax`
- `BoundaryTransitionBan`
- `PrecepteeSync`
- `NightRecovery`

즉 ontology ID는 `team_min:T2:2026-05-26:D` 같은 instance key가 아니라,
항상 **class/family 의미**만 가진다.

초기 형태:

```yaml
constraints:
  TeamMin:
    parent: CoverageConstraint
    scope: [team, day, shift]
    effective_modes: [enforced, skipped_by_capacity, soft_fallback, inactive]
    connects: [AssignmentAtom, Team, Day, Shift]
    explanation_template: "{day} {shift}에서 {team}은 최소 {min_required}명이 필요합니다."

  GradeMax:
    parent: CoverageConstraint
    scope: [grade, day, shift]
    effective_modes: [enforced, soft_fallback, inactive]
    connects: [AssignmentAtom, Grade, Day, Shift]
    explanation_template: "{day} {shift}에서 {grade}는 최대 {max_allowed}명까지 가능합니다."

  ConsecutiveWorkLimit:
    parent: NurseLocalConstraint
    scope: [nurse, day_window]
    effective_modes: [enforced, inactive]
    connects: [AssignmentAtom, Nurse, Day]
    explanation_template: "{nurse}는 최대 연속근무일 {max_days}일을 초과할 수 없습니다."

  NightRecovery:
    parent: NurseLocalConstraint
    scope: [nurse, day_window]
    effective_modes: [enforced, bypassed_by_fixed, inactive]
    connects: [AssignmentAtom, Nurse, Day]
    produces: [ForcedOff]
    explanation_template: "{nurse}는 {night_days}회 N 이후 {recovery_days}일 OFF가 필요합니다."

overrides:
  FixedWanted:
    parent: OverridePolicy
    bypasses: [TransitionBan, InitialForbidden, ProfileShiftLimit]
    does_not_bypass: [ConsecutiveWorkLimit, OneShiftPerDay]
    conditional_effects:
      blocks_recovery_slot: true
      counts_to_coverage: policy_dependent

modes:
  enforced:
    meaning: "실제 solver 또는 precheck에서 hard로 적용된 상태"
  soft_fallback:
    meaning: "retry/fallback 경로에서 hard가 soft objective로 내려간 상태"
  skipped_by_capacity:
    meaning: "후보/active member 부족으로 constraint가 실제 추가되지 않은 상태"
  bypassed_by_fixed:
    meaning: "fixed/fixed_wanted로 인해 특정 제약이 우회된 상태"
  inactive:
    meaning: "설정 또는 실행 경로상 비활성 상태"
  precheck_blocked:
    meaning: "solver build 이전 precheck 단계에서 차단된 상태"
```

### 6.2 `ontology.py`

역할:

- YAML 로드
- family → parent lookup
- family → scope lookup
- family → explanation template lookup
- override bypass 여부 판단

예상 API:

```python
class ConstraintOntology:
    def get_constraint(self, family: str): ...
    def get_parent(self, family: str) -> str | None: ...
    def get_scope(self, family: str) -> list[str]: ...
    def get_template(self, family: str) -> str | None: ...
    def can_bypass(self, override_type: str, family: str) -> bool | None: ...
```

#### 구체 데이터 구조

```python
@dataclass(slots=True)
class OntologyConstraintEntry:
    constraint_id: str
    label: str
    parent: str
    scope: list[str]
    effective_modes: list[str]
    connects: list[str]
    explanation_template: str
    produces: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    default_severity: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class OntologyOverrideEntry:
    override_id: str
    label: str
    parent: str
    bypasses: list[str]
    does_not_bypass: list[str]
    conditional_effects: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OntologyModeEntry:
    mode_id: str
    label: str
    meaning: str
    is_enforced: bool
    severity: str
```

#### registry API (구체화)

```python
class ConstraintOntology:
    def __init__(self, path: str | Path | None = None): ...

    def get_constraint(self, family: str) -> OntologyConstraintEntry | None: ...
    def get_mode(self, mode_id: str) -> OntologyModeEntry | None: ...
    def get_override(self, override_id: str) -> OntologyOverrideEntry | None: ...

    def get_parent(self, family: str) -> str | None: ...
    def get_scope(self, family: str) -> list[str]: ...
    def get_template(self, family: str) -> str | None: ...
    def get_default_severity(self, family: str) -> str | None: ...

    def resolve_alias(self, raw: str) -> str | None: ...
    def can_bypass(self, override_type: str, family: str) -> bool | None: ...
```

#### 초기화 정책

- process-wide singleton 허용
- 실패 시 애플리케이션을 죽이지 않고 warning + empty registry 허용
- unknown family는 passthrough 허용

즉 ontology는 “strict schema validator”가 아니라
**설명 메타데이터 provider** 로 동작해야 한다.

### 6.3 `ontology_attach.py`

역할:

- runtime diagnostics / graph node / debug payload에 ontology metadata 부착

예상 API:

```python
def attach_constraint_ontology(fact: dict, ontology: ConstraintOntology) -> dict: ...
def attach_node_ontology(node: dict, ontology: ConstraintOntology) -> dict: ...
```

#### 구체 attach 계약

모든 attach 결과는 기존 payload를 깨지 않도록 아래 필드만 추가한다.

```json
{
  "ontology": {
    "constraint_id": "TeamMin",
    "group": "CoverageConstraint",
    "scope": ["team", "day", "shift"],
    "mode": "skipped_by_capacity",
    "severity": "hard"
  }
}
```

즉 attach 함수는 **원본 필드 유지 + `ontology` 키 추가**만 수행한다.

---

## 6.4 실제 파일별 attach 대상 payload

### A. `team_grade_precheck.py`

현재 반환 구조:

```python
{"reason_code": ..., "severity": ..., "evidence": {...}}
```

확장 구조:

```python
{
  "reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
  "severity": "hard",
  "evidence": {...},
  "ontology": {
    "constraint_id": "TeamMin",
    "group": "CoverageConstraint",
    "mode": "precheck_blocked",
    "scope": ["team", "day", "shift"]
  }
}
```

### B. `feasibility_alerts.py`, `mid_feasibility.py`

현재는 문자열 경고 위주이므로,
초기에는 문자열을 유지하면서 병렬 구조를 추가한다.

```python
{
  "message": "...",
  "code": "MID_REQUIRED_MISSING",
  "ontology": {...}
}
```

### C. `roster_create_service.py diagnostics`

`diagnostics.reason_codes[]` 는 단순 문자열 대신 아래 객체 리스트로 확장하는 것을 권장한다.

```json
[
  {
    "reason_code": "CAPACITY_TOTAL_SHORTAGE",
    "ontology": {
      "constraint_id": "CoverageMin",
      "group": "CoverageConstraint",
      "mode": "precheck_blocked"
    },
    "evidence": {...}
  }
]
```

### D. `wanted_service.py`

`FixedWantedEntry.source_type`, assignment overlay 결과, skipped reason 등에
`ontology.override_id`, `ontology.relation` 를 붙일 수 있다.

예:

```json
{
  "source_type": "weekly_off",
  "ontology": {
    "override_id": "FixedWanted",
    "relation": "generated_from_weekly_off_overlay"
  }
}
```

### E. `assignment_service.py`

inbound/outbound/transfer/training/leave 이벤트에 대해
아래 수준까지 명시한다.

```json
{
  "assignment_reason": "병동이동",
  "ontology": {
    "constraint_id": "AssignmentWindow",
    "relation": "carries_state",
    "direction": "transfer"
  }
}
```

---

## 7. 실제 코드에 붙이는 방법

### 7.1 `roster_create_service.py`

여기서는 이미 다음이 만들어진다.

- cross-month constraints
- mid-month boundary constraints
- assignment overlay
- fixed wanted / special fixed
- initial_constraints merge

여기서 ontology는 다음 식으로 붙일 수 있다.

#### 제안 1: diagnostics payload 확장

```python
diagnostics["reason_codes"] ->
[
  {
    "constraint_id": "TEAM_MIN",
    "ontology_group": "CoverageConstraint",
    "mode": "enforced",
    "reason_code": "TEAM_ACTIVE_MEMBERS_INSUFFICIENT",
    ...
  }
]
```

#### 제안 2: merged initial constraints에 semantic tag 맵 병행

```python
config_dict["constraint_semantics"] = {
  "forced_off": {...},
  "forbidden": {...},
  "off_window_constraints": {...},
}
```

#### 구체 키 규약

`constraint_semantics` 는 primitive constraint payload와 같은 키를 공유한다.

예:

```python
config_dict["constraint_semantics"] = {
  "forced_off": {
    nurse_id: [
      {
        "day": 0,
        "ontology": {"constraint_id": "NightRecovery", "mode": "enforced"},
      }
    ]
  },
  "forbidden": {
    nurse_id: {
      day: [
        {
          "shift": "D",
          "ontology": {"constraint_id": "BoundaryTransitionBan", "mode": "enforced"},
        }
      ]
    }
  },
  "off_window_constraints": {
    nurse_id: [
      {
        "window": [0, 2],
        "ontology": {"constraint_id": "ConsecutiveWorkLimit", "mode": "enforced"},
      }
    ]
  }
}
```

초기 구현에서는 `cp_sat_basic.py`가 이 구조를 읽을 필요는 없고,
diagnostics / snapshot / graph attach 용도로만 유지해도 충분하다.

### 7.2 `cp_sat_basic.py`

이 파일은 실제 hard 제약 적용점이므로 ontology 자체를 깊게 넣기보다,
**constraint application point에 family id를 부착**하는 것이 좋다.

예:

- `ban_n_to_d` → `TransitionBan`
- `sum OFF in window >= 1` → `ConsecutiveWorkLimit`
- `2N/3N → 2OFF` → `NightRecovery`
- `is_night_nurse` 기반 shift 0화 → `ProfileShiftLimit`
- off cap / weekend off 처리 → `OffCap`, `WeekendOffOnly`

초기 목표:

- CP-SAT 로직을 바꾸지 않고
- debug/log/violation payload에 family id만 추가

#### 구체 attach point

우선순위 높은 attach 지점:

1. fixed cell normalization 직후
2. off partition 생성 직후
3. `n_forbid_n` 계산 직후
4. team/grade/handoff constraint 함수 호출 직전/직후
5. transition ban / recovery / off-window hard rule 추가 직전

각 attach 지점에서 남겨야 할 최소 정보:

```python
{
  "constraint_id": "NightRecovery",
  "mode": "enforced",
  "source": "cp_sat_basic",
  "scope": {"nurse_index": n, "day": d},
}
```

### 7.3 `services/constraints/*.py`

여기는 ontology attach가 가장 쉽다.

- `team_constraints.py` → `TeamMin`
- `grade_constraints.py` → `GradeMin`, `GradeMax`
- `team_grade_handoff_constraints.py` → `TeamGradeHandoff`

그리고 지금 이미 있는 mode (`soft_fallback`, `skipped_by_capacity`)를 ontology mode와 1:1 정렬하면 좋다.

### 7.4 `team_grade_precheck.py`

여기는 reason_code registry와 ontology family 매핑이 가장 큰 가치를 준다.

예:

- `TEAM_MIN_EXCEEDS_GLOBAL_NEED` → `TeamMin`
- `GRADE_MAX_SUM_BELOW_NEED` → `GradeMax`
- `TEAM_GRADE_INTERSECT_SHORTAGE` → `CoverageConstraint` + `CrossConstraint`

즉 precheck는 이미 ontology-friendly 한 구조를 가지고 있다.

#### 구체 reason_code 매핑 원칙

1. reason_code 하나는 반드시 하나의 `constraint_id`를 가져야 한다.
2. group(parent)는 ontology가 자동 계산한다.
3. mode는 precheck 결과이므로 기본 `precheck_blocked`.
4. evidence는 절대 ontology가 바꾸지 않는다.

예:

| reason_code | constraint_id | group | mode |
|---|---|---|---|
| `GLOBAL_DAY_CAPACITY_SHORTAGE` | `CoverageMin` | `CoverageConstraint` | `precheck_blocked` |
| `TEAM_MIN_EXCEEDS_GLOBAL_NEED` | `TeamMin` | `CoverageConstraint` | `precheck_blocked` |
| `GRADE_MAX_SUM_BELOW_NEED` | `GradeMax` | `CoverageConstraint` | `precheck_blocked` |
| `MID_DISABLED_BUT_USED` | `ConfigIntegrity` | `MetaConstraint` | `precheck_blocked` |
| `MID_REQUIRED_MISSING` | `ConfigIntegrity` | `MetaConstraint` | `precheck_blocked` |

여기서 `ConfigIntegrity` 같은 meta family는 필요 시 ontology에 추가한다.

### 7.5 `wanted_service.py`

여기서는 다음을 ontology로 정규화할 수 있다.

- fixed wanted entry
- assignment overlay
- weekly off overlay
- target weekly off overlay

특히 `FixedWanted` / `AssignmentWindow` / `RequiredShiftRule` 관계를 같이 잡으면 좋다.

### 7.6 `assignment_service.py`

여기서는

- inbound
- outbound
- transfer
- training
- leave

를 ontology relation으로 붙이기 좋다.

그리고 `target_shift_types`, `target_grade`, `target_weekly_off_*` 는
향후 rule compiler 입력 primitive로 내려가는 semantic source로 분류 가능하다.

---

## 8. Hypergraph와 ontology를 어떻게 결합하나

Ontology는 hypergraph를 바꾸지 않는다.

대신 node/edge에 metadata를 붙인다.

예:

```json
{
  "id": "team_min:T2:2026-05-26:D",
  "type": "ConstraintInstance",
  "constraint_family": "TeamMin",
  "ontology_group": "CoverageConstraint",
  "effective_mode": "skipped_by_capacity",
  "scope_type": ["team", "day", "shift"]
}
```

즉 graph는 그대로 유지하고,
ontology가 이 graph를 **의미 typed graph** 로 만들어준다.

#### graph node 최소 확장 계약

현재 graph node/edge 구현이 들어갈 경우,
node는 아래 필드를 최소 포함해야 한다.

```json
{
  "node_id": "team_min:T2:2026-05-26:D",
  "constraint_family": "TeamMin",
  "ontology_group": "CoverageConstraint",
  "effective_mode": "skipped_by_capacity",
  "scope_type": ["team", "day", "shift"],
  "default_severity": "hard"
}
```

edge는 다음 수준이면 충분하다.

```json
{
  "edge_id": "e1",
  "relation": "pressures",
  "source": "NightRecovery",
  "target": "CoverageMin",
  "ontology_relation": "produces"
}
```

---

## 9. 지금 당장 구현 순서

### Phase 1 — ontology catalog

- [x] `app/services/semantics/ontology.yaml`
- [x] `app/services/semantics/ontology.py`
- [ ] 핵심 family 10~15개만 정의
- [x] `ontology_attach.py` 생성
- [ ] family alias / mode alias 정의

### Phase 2 — precheck / diagnostics attach

- [x] `team_grade_precheck.py` reason_code → family 매핑
- [ ] `feasibility_alerts.py` / `mid_feasibility.py` 에 ontology tag 추가
- [x] `roster_create_service.py` infeasible reason string 옆에 ontology metadata 병행 저장(`roster_system._ontology_last_reason`)
- [ ] `roster_create_service.py diagnostics` 응답 payload 에 ontology metadata 병행 저장
- [ ] `IssuedRosterSnapshot.violations_json` 구조와 호환되도록 attach 형식 고정

#### 결정 확정

- **canonical 1차 저장 위치는 응답 payload** 로 한다.
- DB snapshot(`IssuedRosterSnapshot.meta_json`, `violations_json`) 저장은 Phase 2 후속 단계로 둔다.
- 즉, 1차 목표는 runtime response/diagnostics에서 ontology metadata를 일관되게 제공하는 것이다.

### Phase 3 — runtime constraint attach

- [ ] `cp_sat_basic.py`의 주요 hard rule application point에 family id 연결
- [ ] `services/constraints/*.py` mode와 ontology mode 정렬
- [ ] `wanted_service.py`, `assignment_service.py` 에 override/relation attach

### Phase 4 — graph attach

- [ ] hypergraph node metadata에 `ontology_group`, `scope_type`, `effective_mode` 추가
- [ ] agent/debugger summary를 ontology group 기준으로도 집계
- [ ] graph node/edge schema에 ontology relation field 추가

### Phase 5 — test / validation

- [x] ontology loader 단위 테스트
- [x] reason_code ↔ constraint_id mapping 테스트
- [ ] fixed_wanted bypass 판정 테스트
- [ ] diagnostics payload backward compatibility 테스트
- [x] unknown constraint family fallback 테스트

---

## 10. 이 설계의 장점

1. solver를 건드리지 않고 의미를 통일할 수 있다.
2. fixed_wanted 같은 복잡한 override를 한곳에서 설명할 수 있다.
3. configured hard vs enforced hard를 분리해 설명하기 쉬워진다.
4. graph가 단순 연결 구조가 아니라 의미 typed graph가 된다.
5. agent reasoning에 더 안전한 도메인 상식을 줄 수 있다.

---

## 11. 이 설계에서 하지 말아야 할 것

1. ontology로 feasible/infeasible 판단하기
2. ontology만 보고 hard conflict 결론내리기
3. ontology를 solver truth보다 앞세우기
4. 처음부터 OWL/RDF로 크게 시작하기
5. family taxonomy와 runtime mode semantics를 섞어버리기

---

## 12. 최종 권고

이 repo에서는 ontology를 붙이는 것이 맞다.

다만 방식은 반드시:

```text
YAML ontology catalog
→ Python registry
→ runtime diagnostics / graph metadata attach
```

로 가야 한다.

즉,

- ontology = meaning layer
- hypergraph = runtime connection layer
- snapshot/diagnostics = runtime fact layer

이 3층 분리가 유지되어야 한다.

그렇게 하면,
현재 코드베이스의 분산된 제약 의미를 무리 없이 통합할 수 있고,
향후 agent/graph 설명력도 크게 좋아질 것이다.

---

## 13. 남아 있는 최소 결정사항 (질문 필요 후보)

현재 문서 기준으로 남아 있던 2개 결정은 아래와 같이 확정한다.

1. ontology metadata의 canonical 1차 저장 위치
   - **응답 payload 우선**
   - DB snapshot 저장은 후속 단계

2. 설정 정합성 오류 ontology family
   - **`ConfigIntegrity` 별도 family 채택**

이 둘이 결정되었으므로, 현재 문서는 구현 착수 가능한 수준으로 본다.
