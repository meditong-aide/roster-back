# Constraint Agent Control 초안 (Module-Level Adjustment)

## 0. 이 문서의 위치

이 문서는 다음 3개 문서의 **다음 단계**다.

- `CONSTRAINT_IMPACT_GRAPH_ACTIVE_BLUEPRINT.md` — snapshot/atom/state machine/graph 5계층 청사진
- `CONSTRAINT_IMPACT_GRAPH_SCHEMA_AND_API.md` — 위 5계층의 구체 dataclass·함수 시그니처
- `ONTOLOGY_GROUNDED_CONSTRAINT_IMPACT_GRAPH_PLAN.md` — ontology catalog + attach 패턴

위 3개는 "현재 hard 의미를 정규화하고 ontology로 의미를 입혔다" 까지를 말한다.

이 문서는 그 위에 **에이전트가 hard 충돌을 줄이려고 제약을 모듈/인스턴스 수준으로 조절(adjustment)** 할 수 있는 control 레이어를 정의한다.

검색(visualization)은 본 PRD의 1차 범위가 아니다 — 본 문서는 **adjust 가능한 표면**을 정의하는 것이 목적이다.

---

## 1. 핵심 개념 3종

### 1.1 ConstraintModule

= **ontology의 constraint family** 1개.

`ontology.yaml::constraints[<id>]` 의 entry 가 그대로 1개 module이다.

| ontology family id | 의미 | parent |
|---|---|---|
| `CoverageMin` | 일별/시프트별 최소 인원 | CoverageConstraint |
| `CoverageMax` | 일별/시프트별 최대 인원 | CoverageConstraint |
| `TeamMin` | 팀별 시프트 최소 인원 | CoverageConstraint |
| `GradeMin` | 등급별 시프트 최소 인원 | CoverageConstraint |
| `GradeMax` | 등급별 시프트 최대 인원 | CoverageConstraint |
| `TeamGradeHandoff` | 팀×등급 인계 제약 | CoverageConstraint |
| `ConsecutiveWorkLimit` | 연속근무일 한도 | NurseLocalConstraint |
| `ConsecutiveNightLimit` | 연속 N 한도 | NurseLocalConstraint |
| `NightRecovery` | N 블록 후 회복 OFF | NurseLocalConstraint |
| `OffCap` | 월 OFF 개수 한도 | NurseLocalConstraint |
| `BoundaryTransitionBan` | 경계 시프트 금지 (N→D 등) | NurseLocalConstraint |
| `PrecepteeSync` | 프리셉티 동기화 | CouplingConstraint |
| `AssignmentWindow` | 파견/이동 윈도우 | CouplingConstraint |
| `CarryoverBoundary` | tail state 이어받기 | CouplingConstraint |
| `FixedWanted` | 확정 원티드 override | OverridePolicy |
| `ConfigIntegrity` | 설정 정합성 | MetaConstraint |

에이전트가 하는 검색은 본질적으로 위 module list 이다. 그룹화는 `parent` 로.

### 1.2 ConstraintInstance

= **(family, scope dict)** 의 구체 인스턴스 1개.

`build_constraint_nodes(snapshot, atoms)` 결과의 `ConstraintNode` 1개가 1 instance.

예:
```
node_id = "team_min:T2:25:D"
family  = "TeamMin"
scope   = {"team": "T2", "day": 26, "shift": "D"}
mode    = "enforced"
target  = 1                   # 최소 1명
```

### 1.3 ConstraintAdjustment

= **에이전트가 module/instance 에 가하는 단위 변경**.

```python
@dataclass(slots=True)
class ConstraintAdjustment:
    family: str                          # ontology family id 또는 alias
    action: Literal[
        "disable_module",
        "force_soft_mode",
        "set_threshold",
        "narrow_scope",
    ]
    scope_filter: dict[str, Any] = {}    # 비어 있으면 family 전체에 적용
    target_value: Any = None             # set_threshold: int / narrow_scope: list[scope]
    reason: str | None = None
```

#### action 4종 의미 정의

| action | 의미 | 적용 위치 |
|---|---|---|
| `disable_module` | 해당 family 인스턴스 전체 비활성 (config_dict에서 비움) | 솔버 build 직전 |
| `force_soft_mode` | family hard → soft fallback (이미 있는 fallback 플래그 ON) | 솔버 build 직전 |
| `set_threshold` | 특정 instance 의 min/max 값을 변경 | 솔버 build 직전 (config 수정) |
| `narrow_scope` | 특정 (team/grade/day/shift) 인스턴스만 비활성 | 솔버 build 직전 |

---

## 2. ontology family ↔ config_dict 매핑표 (적용 핵심)

| family | adjustment | config_dict 변경 |
|---|---|---|
| `TeamMin` | disable_module | `team_min_by_team = {}` |
| `TeamMin` | force_soft_mode | `team_min_soft_fallback = True` |
| `TeamMin` | set_threshold(team, shift) | `team_min_by_team[team][shift] = target_value` |
| `TeamMin` | narrow_scope(team) | `team_min_by_team.pop(team)` |
| `GradeMin` | force_soft_mode | `grade_config.allow_soft_fallback = True` (`_force_grade_min_soft_fallback=True` flag) |
| `GradeMin` | disable_module | `grade_config.constraints` 의 min 항목 제거 |
| `GradeMax` | force_soft_mode | `_force_grade_max_soft_fallback = True` (기존 플래그 재사용) |
| `GradeMax` | disable_module | `grade_config.constraints` 의 max 항목 제거 |
| `TeamGradeHandoff` | force_soft_mode | `team_handoff_soft_fallback = True` |
| `CoverageMin` | (제한적) set_threshold | `daily_shift_requirements_by_day[day][shift]` 직접 수정 |
| `BoundaryTransitionBan` | disable_module | `ban_n_to_d/ban_e_to_d/ban_n_to_e = False` |
| `ConsecutiveWorkLimit` | set_threshold | `max_consecutive_work_days = target_value` |
| `ConsecutiveNightLimit` | set_threshold | `max_consecutive_nights = target_value` |
| `NightRecovery` | disable_module | `two_offs_after_two_nig=False; two_offs_after_three_nig=False` |

위 표에 없는 family + action 조합은 `ValueError("unsupported adjustment: <family>/<action>")`.

---

## 3. 적용 경로 (apply path)

```
[Agent]
   │ POST /roster_create/generate
   │   { ..., constraint_adjustments: [ ConstraintAdjustment, ... ] }
   ▼
[roster_create_service.generate_roster_service]
   │ (req 그대로 전달)
   ▼
[roster_create_service._run_cp_sat_basic]
   │ config_dict 빌드 직후
   │ if req.constraint_adjustments:
   │     config_dict = apply_adjustments_to_config(
   │         config_dict, req.constraint_adjustments
   │     )
   ▼
[cp_sat_basic.generate_roster_cp_sat]
   │ 변경된 config_dict 로 솔버 build/solve
   ▼
[솔버 결과 + constraint_impact analysis]
   │ snapshot 도 변경된 config_dict 로 재구성
   │ → analyze_current_roster() 결과가 변경된 mode 반영
   ▼
[response]
```

핵심: **adjustment 는 솔버 input layer 까지만 영향**. snapshot/atom/state machine 의 derivation 로직은 변경하지 않음.

---

## 4. Conflict Resolution 정책

같은 family 에 다중 adjustment 가 들어오면 **list 순서대로 적용**한다 (stable, last-write-wins).

특정 충돌 케이스의 처리:

| 입력 | 처리 |
|---|---|
| `disable_module` + `set_threshold` 동시 | 적용 순서대로 처리. disable 이 마지막이면 비워짐. |
| `force_soft_mode` + `disable_module` | disable 이 우선 (disable 이 의미상 강함) — 마지막에 disable 적용된 것으로 간주 |
| 같은 instance 에 `set_threshold` 2회 | 마지막 값으로 덮어씀 |
| ontology unknown family | 즉시 `ValueError` (silent skip 금지) |
| ontology resolve 가능하지만 매핑표에 없는 action | 즉시 `ValueError` |

---

## 5. Lifecycle / Persistence

이 PRD 1차 범위에서 adjustment 는 **요청 1회 생애주기** 만 가진다.

- `RosterRequest.constraint_adjustments` 에 실려서 들어옴
- `_run_cp_sat_basic` 의 한 솔버 attempt 에만 적용됨
- DB 영구 저장 안 함

이유:
1. 에이전트는 매 호출마다 다른 adjustment 실험을 할 수 있어야 함
2. DB 영구화는 다음 단계(룰 영속화 + UI 노출) 에서 별도 PRD로 처리
3. 1차 목표는 "agent 가 호출 단위로 조절 가능" 까지만

다음 단계에서 가능한 영속화 옵션:
- `constraint_adjustment_overlay` 테이블 (group×year×month 키)
- 또는 `roster_config` 의 JSON 컬럼 1개

---

## 6. 검색(read) 표면

### 6.1 list_constraint_modules(snapshot) → list[ModuleSummary]

```python
@dataclass(slots=True)
class ModuleSummary:
    family: str                     # ontology id
    label: str                      # 한국어 라벨
    parent: str                     # CoverageConstraint / NurseLocalConstraint / ...
    scope: list[str]                # ["team", "day", "shift"]
    effective_modes: list[str]      # ontology effective_modes
    current_mode_estimate: str      # 현재 config 기반 추정 mode
    instance_count_estimate: int    # 노드 수 (build_constraint_nodes 통과 시)
    supported_actions: list[str]    # 매핑표에서 지원하는 action 리스트
```

### 6.2 find_constraint_instances(snapshot, family=None, scope_filter=None) → list[InstanceView]

```python
@dataclass(slots=True)
class InstanceView:
    node_id: str
    family: str
    mode: str
    scope: dict[str, Any]
    operator: str
    target: Any
    explanation: str
```

---

## 7. 안전장치

1. `apply_adjustments_to_config` 는 **항상 deepcopy 한 dict** 를 반환. 호출자 config 를 in-place 변형 금지.
2. 모든 adjustment 는 ontology family 또는 alias 를 통해 정규화. raw string 직접 매칭 금지.
3. snapshot 변경은 본 PRD 범위 밖. 변경된 config 로 snapshot 을 다시 build 해야 함 (`build_semantics_snapshot_from_roster_system` 자연스럽게 지원).
4. solver 가 unsat 으로 가면 adjustment 적용 후라도 그 자체로 보고됨. control 레이어는 "조절은 했다, 결과는 솔버 책임" 의 분리를 유지.

---

## 8. 본 PRD 범위 (다시 확인)

**포함**:
- 모듈/인스턴스/조절 개념 정의
- adjustment action 4종 + family↔config 매핑
- list/find/apply API
- 라우터 노출 (GET 2개)
- 솔버 경로에 주입
- 단위/라이브 테스트

**제외**:
- DB 영속화
- 시각화/그래프 UI
- 모든 hard 충돌의 자동 풀이 (이 레이어는 "조절 도구" 만 제공)
- 새 ontology family 추가 (기존 catalog 만 사용)

---

## 9. 다음 단계 (이 PRD 종료 후)

1. adjustment 영속화 (overlay 테이블)
2. 에이전트 LLM tool spec 화 (해당 도구가 호출되어 RosterRequest.constraint_adjustments 를 채우도록)
3. simulation API 와 결합 — adjustment 적용 후 simulate 로 hard violation 사전 체크
4. parity harness 에 adjustment 시나리오 fixture 추가
