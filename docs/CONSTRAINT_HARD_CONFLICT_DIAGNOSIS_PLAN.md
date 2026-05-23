# Hard Conflict Diagnosis 설계안 (Ontology + Probe + Conditional Retention)

## 0. 이 문서의 위치

이 문서는 다음 5개 문서의 **다음 단계**다.

- `CONSTRAINT_IMPACT_GRAPH_ACTIVE_BLUEPRINT.md` — 5계층 청사진
- `CONSTRAINT_IMPACT_GRAPH_SCHEMA_AND_API.md` — dataclass + 함수 시그니처
- `ONTOLOGY_GROUNDED_CONSTRAINT_IMPACT_GRAPH_PLAN.md` — ontology catalog
- `CONSTRAINT_AGENT_CONTROL_DRAFT.md` — module 단위 adjustment control 표면
- (Phase B implementation in `solver_emit.py`) — 솔버-attach-point emit recorder

원래 PRD 의 진짜 목표 — **"수많은 hard 제약 선택으로 인해 옵션이 많아지면서 발생하는 hard 충돌을 최소화"** — 의 마지막 구성 요소다. 지금까지는 "조절 표면" 만 만들었고, 이제 **"어디를 조절할지 진단"** 하는 도구가 필요하다.

---

## 1. 핵심 인사이트 — 이 모든 게 한 그래프 작업

지금까지 산발적으로 추가한 것들 (snapshot, atoms, ontology, control layer, solver_emit) 은 사실 같은 그래프의 **다른 layer** 였다. 명시적으로 정리하면:

### 1.1 4 Layer 모델

```
┌──────────────────────────────────────────────────────────────────┐
│ 4. Conflict Probe (reasoning / diagnosis)                        │
│    - emit + instances 위에서 IIS-like 충돌 원인 추적             │
│    - ontology 의 priority/scenario 로 search space 가지치기      │
│    - control layer 의 disable_module 로 probing 시뮬레이션       │
└──────────────────────────────────────────────────────────────────┘
                          ▲
┌──────────────────────────────────────────────────────────────────┐
│ 3. Solver Emit (event log, in-memory + outcome-conditional)      │
│    - "이 attempt 에서 m.Add 가 실제로 어디 어디 박혔다"          │
│    - SAT 시점엔 aggregate + interesting events 만                │
│    - UNSAT 시점엔 granular 전체 (probe 입력)                     │
└──────────────────────────────────────────────────────────────────┘
                          ▲
┌──────────────────────────────────────────────────────────────────┐
│ 2. Runtime Graph (A-box, instances)                              │
│    - snapshot 기반 인스턴스 노드 + atom 관계 엣지                │
│    - "지금 실행에서 어떤 (family, scope) 가 살아있냐"            │
│    - graph_builder.build_constraint_nodes / build_constraint_edges│
└──────────────────────────────────────────────────────────────────┘
                          ▲
┌──────────────────────────────────────────────────────────────────┐
│ 1. Ontology (T-box, KB)                                          │
│    - family 정의, override 매트릭스 (이미 있음)                  │
│    - **NEW: relaxation_priority, scope_explosion**               │
│    - **NEW: conflict_scenarios — 구체적 충돌 상황 카탈로그**     │
└──────────────────────────────────────────────────────────────────┘
```

이 그림에서 사용자가 묻는 모든 view 는 같은 시스템의 다른 단면이다:

- **"이 그룹의 룰이 뭐냐?"** = 1 + 2 → `list_constraint_modules()` (이미 있음)
- **"이번 SAT 런에서 뭐가 적용됐나?"** = 2 + 3(aggregate) → 이미 있음
- **"왜 UNSAT 인가?"** = 1 + 2 + 3(granular) + 4 → 이번 PRD 의 핵심
- **"무엇을 조절해야 하나?"** = 4 → ranked candidates + control layer 적용

---

## 2. 왜 raw emit 이 SAT 시점엔 노이즈고 UNSAT 시점엔 필수인가

### 2.1 SAT 시점

8820 개 BoundaryTransitionBan record 가 모두 `enforced` 라면:
- 정보 가치 0 (다 같은 결과)
- aggregate `{enforced: 8820}` 1 줄로 충분
- 뜨문뜨문 `bypassed_by_fixed` 가 있으면 그것만 노출 → "interesting events"

### 2.2 UNSAT 시점

CP-SAT 가 UNSAT 을 반환하면 **어떤 hard 들이 합쳐서 모순을 만들었는지** 알아야 한다 (IIS — Irreducible Inconsistent Subset).

**Native 방법** (CP-SAT): 모든 m.Add 에 assumption literal 을 wrapping → `SufficientAssumptionsForInfeasibility()`. 침습적.

**Practical 방법** (지금 우리 구조에서 가능):

1. UNSAT 발생 시 emit records 전부 보존
2. ontology 의 conflict_scenarios + relaxation_priority 로 **충돌 후보 ranking**
3. 가장 의심되는 family 1 개를 control layer 의 `disable_module` 로 끄고 재시도
4. SAT 되면 그 family 가 충돌의 한 축
5. emit records 의 `related_atom_keys` 를 따라 어느 atom 묶음이 부딪쳤는지 추적

즉 UNSAT 시점엔 **granular records + ontology + control layer 가 모두 필요**.

---

## 3. Outcome-Conditional Retention 정책

| 결과 | response.solver_emitted_nodes | trace_store | 이유 |
|---|---|---|---|
| SAT, parity 일치 | `[]` 또는 0 sample | aggregate 만 | raw 가 정보 없음 |
| SAT, bypass 발생 | `interesting_events` (수십 개) | aggregate + bypasses | bypass 는 fixed_wanted 작동 증거 |
| SAT, parity drift | drift 항목만 (수십 개) | aggregate + drift | 그래프 vs 솔버 일관성 회귀 신호 |
| **UNSAT** | **family 별 sample 또는 전체 (gz)** | **전체 + ConflictProbeReport** | 진단 데이터 |
| Precheck blocked | 빈 emit (솔버 안 돔) | precheck reasons 만 | 솔버 이전 단계 |

이 분기는 `_build_constraint_impact_payload` 에서 처리.

---

## 4. Ontology KB 보강 — 구체적 충돌 시나리오

### 4.1 새로 추가될 family-level 메타

각 `constraints[<id>]` 에 다음 추가:

- `relaxation_priority`: 1 (제일 먼저 풀어볼 것) ~ 5 (절대 안 풀음, nurse safety)
- `scope_explosion`: `low` (≤ nurse 수) / `medium` / `high` (≥ nurse × day)

예:
```yaml
TeamMin:
  ...
  relaxation_priority: 3
  scope_explosion: high

OffCap:
  ...
  relaxation_priority: 5     # nurse welfare, 거의 안 풀음
  scope_explosion: low

NightRecovery:
  ...
  relaxation_priority: 4     # 안전 관련
  scope_explosion: medium
```

### 4.2 새로 추가될 conflict_scenarios 섹션

ontology.yaml 최상위에 추가:

```yaml
conflict_scenarios:
  - id: BTBAN_VS_FIXED_SEQUENCE
    involved_families: [BoundaryTransitionBan, FixedWanted]
    trigger_condition: |
      fixed_wanted 가 (D-1=N, D=D) 또는 (D-1=E, D=D) 시퀀스를 강제하면서
      해당 시퀀스가 BoundaryTransitionBan 의 bypass 매트릭스에 명시되지 않은 경우
    why_infeasible: |
      두 fixed cell 이 transition ban 을 피하지 못해 모델이 UNSAT
    suggested_relaxation: |
      FixedWanted 셀 검토 → 시퀀스 수정 또는 BoundaryTransitionBan 일시 disable
    detection_hint: |
      emit 에서 mode=enforced 인 BTBAN 노드의 related_atom_keys 가
      fixed_cells 와 모두 겹치는 케이스 카운트

  - id: OFFCAP_VS_WEEKEND_OFF_NARROW_WINDOW
    involved_families: [OffCap, WeekendOffOnly, AssignmentWindow]
    trigger_condition: |
      WeekendOffOnly nurse 의 active_days 가 OffCap 최소값보다 작거나
      active 윈도우 내 weekend slot 수가 부족
    why_infeasible: |
      OFF 강제 슬롯이 cap 을 못 채움
    suggested_relaxation: |
      해당 nurse 의 OffCap 완화 또는 WeekendOffOnly 예외
    detection_hint: |
      preflight_alerts 의 weekend_slot_shortage + nurse 단위 active day 수

  - id: NIGHT_RECOVERY_VS_COVERAGE_MIN
    involved_families: [NightRecovery, CoverageMin, TeamMin]
    trigger_condition: |
      특정 nurse 의 2N/3N 블록 직후 recovery OFF 강제 일자에
      해당 day 의 CoverageMin 이 빠듯한 상황
    why_infeasible: |
      복수 nurse 의 recovery OFF 가 같은 날에 몰려 D/E/N 인력 부족
    suggested_relaxation: |
      CoverageMin 의 해당 day soft 강등 또는 N 블록 분산
    detection_hint: |
      block_end_day 와 day-level coverage slack 의 교집합 카운트

  - id: TEAM_MIN_VS_GRADE_MAX
    involved_families: [TeamMin, GradeMax]
    trigger_condition: |
      특정 team 의 minimum 인원 요구가, 그 day/shift 의 grade max 한도와
      교집합에서 빈자리를 만들지 못하는 경우
    why_infeasible: |
      TeamMin 만족하려면 등급 X 사람이 필요한데, GradeMax 가 그 등급을 막음
    suggested_relaxation: |
      GradeMax 의 해당 grade soft 강등 또는 TeamMin disable
    detection_hint: |
      team membership 과 grade 의 교집합 사이즈 < TeamMin
```

이 시나리오들은 **단순 family 간 충돌 리스트가 아니라** 어떤 상황에서 어떻게 발생하는지 + 어떻게 푸는지 구체적으로 적는다. 사용자 의견 — "구체적 어떤 상황들에 발생할지 쓰는게 더 이상적" — 반영.

---

## 5. Conflict Probe 설계

### 5.1 입력

- `solver_emit_records: list[EmittedConstraint]` — 이번 attempt 의 raw emit
- `snapshot: SemanticsSnapshot` — 컨텍스트
- `ontology: ConstraintOntology` — KB

### 5.2 출력

```python
@dataclass(slots=True)
class ConflictProbeReport:
    ranked_candidates: list[RankedCandidate]    # 끄거나 풀어볼 우선순위 ordered list
    matched_scenarios: list[MatchedScenario]    # ontology.conflict_scenarios 매칭 결과
    probe_plan: list[ProbeStep]                 # 어떤 family 를 어떤 순서로 시뮬레이션할지
    notes: list[str]
```

```python
@dataclass(slots=True)
class RankedCandidate:
    family: str
    score: float                                # high = 충돌 후보로 강함
    reasons: list[str]                          # "relaxation_priority=2", "scope_explosion=high", ...
    sample_records: list[dict]                  # 해당 family 의 emit 중 일부 (추적 시작점)
```

```python
@dataclass(slots=True)
class MatchedScenario:
    scenario_id: str
    confidence: float                           # 0.0 ~ 1.0
    evidence: dict[str, Any]                    # detection_hint 가 발견한 사실
    suggested_relaxation: str
```

```python
@dataclass(slots=True)
class ProbeStep:
    order: int
    action: dict[str, Any]                     # ConstraintAdjustment shape
    rationale: str
```

### 5.3 Ranking 알고리즘 (v1)

1. 모든 family 별 emit count 추출
2. ontology.relaxation_priority 가 낮을수록 (= 풀기 쉬울수록) 점수 높임
3. ontology.matched_scenarios 에 등장하면 점수 추가
4. scope_explosion=high 는 점수 낮춤 (한꺼번에 다 끄면 너무 많이 영향)
5. 이미 이번 attempt 에서 enforced 가 0 인 family 는 제외

### 5.4 Probe Plan

ranked_candidates 를 순서대로 `disable_module` 시뮬레이션 plan 으로 변환:

```python
[
  ProbeStep(order=1, action={"family": "TeamMin", "action": "disable_module"},
            rationale="lowest relaxation_priority, matched scenario TEAM_MIN_VS_GRADE_MAX"),
  ProbeStep(order=2, action={"family": "BoundaryTransitionBan", "action": "disable_module"},
            rationale="next priority, matched scenario BTBAN_VS_FIXED_SEQUENCE"),
  ...
]
```

에이전트는 이 plan 을 받아서 control layer 로 1 step 씩 시뮬레이션 → SAT 되는 첫 step 에서 멈춤.

### 5.5 v1 범위 / v2 확장

**v1 범위**:
- ranking + scenario matching + probe plan 생성
- 자동 시뮬레이션 실행은 안 함 (에이전트/사용자 책임)

**v2 확장**:
- 자동 probe 실행 (멀티 attempt)
- IIS 정확화 (assumption literal wrapping)
- 시나리오 confidence 학습 (실제 풀린 사례 fingerprint)

---

## 6. Emit 슬라이스 확장

이번 PRD 에서 추가:

| Family | 위치 | 예상 emit 수 |
|---|---|---|
| AllowedShiftMask | cp_sat_basic.py:3289-3295 | nurse × day × shift code (최대 ~3600) |
| NotOneNight | cp_sat_basic.py:3315 | nurse × interior day (최대 ~840) |

기존 BoundaryTransitionBan 과 합쳐 emit family 3 개 — 충돌 진단 데이터의 의미 있는 첫 sample.

ontology.yaml 에도 두 family entry 추가 (없으면) → list_constraint_modules 에 노출.

---

## 7. 구현 순서 (사용자 요구 순서)

```
(d) Ontology KB 보강           ← US-002
        │
        ▼
(e) Conflict Probe              ← US-003
        │
        ▼
(c) Outcome-conditional         ← US-004
        │
        ▼
(b) Emit 슬라이스 확장          ← US-005
        │
        ▼
라이브 검증 + 원복              ← US-006
```

각 단계마다 단위 테스트 + 라이브 검증. 누락 없이.

---

## 8. 원래 목표와의 정렬

원래 PRD 목적:
> "수많은 하드제약 선택들로 인해 옵션이 많아지면서 발생하는 hard 제약 충돌을 최소화"
> "에이전트가 이걸 조절할 수 있어야 하니까"

지금까지 성취:
- 조절 표면 (control layer) ✓
- 모듈 단위 검색 (list_constraint_modules) ✓
- Solver-attach-point emit (1 family) ✓
- Graph nodes (부분) △
- Ontology vocabulary ✓

이번 PRD 가 닫는 갭:
- **"어디를 조절할지 모름"** → ConflictProbe 가 ranked_candidates 제공
- **"raw 8820 노이즈"** → outcome-conditional retention 으로 노이즈 제거
- **"emit 1 family 만"** → 3 family 로 확장 → conflict 진단 데이터 의미 있는 sample

이 PRD 완료 후 에이전트는:
1. UNSAT 응답 받음
2. response.constraint_impact.conflict_probe.ranked_candidates 확인
3. 1 순위 candidate 를 control layer (constraint_adjustments) 로 시도
4. SAT 될 때까지 반복 또는 사용자에게 옵션 제시

이게 "에이전트가 모듈 단위로 hard 충돌을 줄이는" 자동화의 첫 완성 형태.

---

## 9. 본 PRD 에서 하지 않는 것

- IIS 정확화 (assumption literal wrapping) → v2
- 자동 multi-attempt probing → 별도 PRD
- Trace store 영속화 (per-run 파일 저장) → 별도 PRD
- Viewer (HTML UI) → 별도 PRD
- 모든 hard 의 emit 추가 (현재는 3 family) → 점진 추가

---

## 10. 위험 요소

1. **ontology conflict_scenarios 의 정확도** — 도메인 전문가 검증 필요. 일단 backend 도메인에서 관찰된 케이스 4 개로 시작.
2. **probe ranking 휴리스틱** — v1 의 점수 공식은 단순. 운영 데이터로 보정 필요.
3. **outcome-conditional retention 의 SAT 판정** — `valid_under_current_semantics` 가 false 면 UNSAT 으로 간주, 그 외엔 SAT. 회색 지대 (예: feasible 이지만 grade fallback 적용) 는 SAT 으로 분류 + flag.
4. **emit 추가 시 cp_sat_basic 의 가독성** — 매 m.Add 옆에 emit. 가독성 약간 낮아짐. 다음 단계에서 데코레이터/컨텍스트 매니저로 정리 가능.
