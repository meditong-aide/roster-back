# Unified Ontology Graph Schema — 4종 노드 단일 스키마

**작성일:** 2026-06-02
**상태:** 설계 확정 + 구현 착수
**결정:** 기존 2레이어(`constraint_impact/graph_nodes.py` 런타임 그래프 + `semantics/ontology.yaml` 진단 온톨로지)를 엎고, 연구계획의 4종 노드를 1급 시민으로 갖는 **단일 통합 그래프 스키마**로 통일한다.
**구현 위치:** `app/services/ontology_graph/`

---

## 0. 왜 단일 스키마인가

기존 구조 갭(`RESEARCH_FIT_CONSTRAINT_IMPACT_GRAPH_GAP_ANALYSIS.md` 후속 점검):

- ① 제약 노드 — 있음(단 graph layer + ontology layer 양쪽 분리)
- ② 도메인 객체 노드 — **없음** (간호사·날짜·shift·팀·Grade가 `scope` dict 필드값으로만 존재)
- ③ 상태 노드 — **절반만** (개인 피로/연속근무만, 수요·공급 상태 없음)
- ④ 완화 action 노드 — 있음(단 ontology layer에만, 그래프 1급 노드 아님)

→ 4종을 하나의 그래프에서 노드/엣지로 다루려면 단일 스키마가 필요.

---

## 1. 노드 4종 (연구계획 1:1)

| 노드 kind | 의미 | subtype | 기존 코드 대응 |
|---|---|---|---|
| **constraint** | 제약 함수 | family (coverage/team_min/grade_*/transition_ban/recovery_*/…) | `graph_nodes.ConstraintNode`, `ontology.yaml constraints:` |
| **domain_object** | 도메인 객체 | nurse / day / shift / ward / team / grade / leave / wanted_off | (신규) — 기존엔 scope dict 필드값뿐 |
| **state** | 수요·공급·부하 상태 | supply_demand / skill_supply / nurse_load | `graph_nodes.GraphExpansionState`(nurse_load만) + (신규 supply_demand) |
| **action** | 완화 action | force_soft_mode / disable_module / set_threshold / narrow_scope / data_correction_required | `ontology.yaml treatments:`, `constraint_impact/control.py` |

공통 필드: `node_id`(stable kebab), `kind`, `label`, `attrs: dict`, `evidence: dict`.

---

## 2. 엣지 (directed, typed)

| relation | tail → head | 의미 (연구계획 예시) |
|---|---|---|
| `constrains` | constraint → domain_object | 제약이 어떤 객체에 적용되는가 |
| `requires` | constraint → state(demand) | 제약이 특정일·shift 수요를 만든다 |
| `supplied_by` | state(supply) → domain_object(nurse) | 가용 인력의 공급원 |
| `reduces` | domain_object(leave/wanted_off) → state(supply) | **연차·희망OFF가 특정일 가용 인력을 줄인다** |
| `belongs_to` | domain_object(nurse) → domain_object(team/grade) | 소속 |
| `pressures` | state(shortage) → constraint | **가용 부족이 최소인원/skill 제약 위반으로 이어진다** |
| `mitigates` | action → constraint \| state | **완화 action이 어떤 충돌을 해소하는가** |
| `derived_from` | state → state | 상태 파생 (예: 누적부하 → 회복 의무) |

엣지 끝점 kind 적법성은 스키마가 강제(governance). 부적합 엣지는 검증 단계에서 reject.

연구계획의 대표 인과 경로가 그대로 그래프 경로가 된다:

```
wanted_off(간호사X, day12) --reduces--> state:supply_demand(day12,N)
state:supply_demand(day12,N){required=3, available=2, shortage=1} --pressures--> constraint:coverage_min(day12,N)
action:set_threshold(daily_N[day12] -1) --mitigates--> constraint:coverage_min(day12,N)
```

---

## 3. 기존 자산 매핑 (엎되, 재사용)

| 기존 | 신규에서 |
|---|---|
| `constraint_impact/types.py` `ConstraintFamily`, `ConstraintMode` | 그대로 재사용 (constraint 노드 family/mode) |
| `graph_nodes.ConstraintNode` | → `constraint` 노드로 흡수 |
| `graph_builder.build_constraint_nodes` (coverage/team/grade/preceptee/transition/recovery) | → 신규 builder가 동일 로직으로 constraint+state+domain_object+엣지 생성 |
| `atoms.AssignmentAtom` | → `supplied_by` 엣지의 evidence (간호사가 day×shift 공급) |
| `nurse_state_machine` (consec/recovery/fatigue) | → `state:nurse_load` 노드 + `derived_from` 엣지 |
| `ontology.yaml treatments:` / `OntologyTreatment` | → `action` 노드 |
| `ontology.yaml causes:` / `OntologyCause` | → `state(shortage)` + `pressures` 엣지로 표현 (cause는 별도 kind 아님) |
| **max-flow(미구현)** | → `state:supply_demand`의 available/shortage 산출기로 신규 추가(갭분석 ① Tier-1) |

---

## 4. 마이그레이션 단계

1. **schema.py** — 4종 노드 + 엣지 + `OntologyGraph` 컨테이너 + 끝점 적법성 검증 (이번 커밋)
2. **builder.py** — `SemanticsSnapshot` → 통합 그래프 생성 (기존 graph_builder 로직 이식 + domain_object/supply_demand 신규)
3. **supply_demand** — max-flow/산술로 day×shift available·shortage 산출 → state 노드 채움
4. **action 연결** — `ontology.yaml treatments` → action 노드 + `mitigates` 엣지
5. **구 레이어 제거** — graph_nodes/graph_builder 의존처를 신규로 이전 후 폐기
6. **평가 훅** — cause recall / top-k 채점은 통합 그래프 경로 위에서 산출

---

## 5. 하지 말 것

- domain_object를 다시 constraint의 scope dict로 흡수 (②가 사라짐 — 엎는 이유 자체)
- cause를 별도 노드 kind로 부활 (cause = state shortage + pressures 엣지로 충분)
- 끝점 kind 검증 없이 임의 엣지 허용 (단일 스키마 거버넌스 붕괴)
