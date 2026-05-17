# Harness ↔ Ontology Hypergraph Mapping Spec

> 목적: 체크리스트 평가 결과를 하이퍼그래프(온톨로지)로 정규화하여
> "무엇이 어떤 경로로 위반을 만들었는지"를 추적 가능하게 만든다.

---

## 1) Design Goals

1. 체크리스트 pass/fail을 단순 불리언이 아니라 **인과 그래프**로 저장
2. 동일 이슈의 반복 패턴을 월/그룹/설정 축으로 비교 가능
3. 신규 규칙 추가 시 매핑 확장이 쉬운 구조

---

## 2) Core Node Types

## 2.1 Entity Nodes
- `GroupNode(group_id)`
- `MonthNode(year, month)`
- `TeamNode(team_id)`
- `NurseNode(nurse_id)`
- `ShiftNode(code: D/E/N/M/O)`
- `DayNode(day_index)`

## 2.2 Constraint Nodes
- `CoverageMinNode(day, shift)`
- `CoverageMaxNode(day, shift)`
- `TeamMinNode(team, day, shift)`
- `GradeMinNode(grade, day, shift)`
- `GradeMaxNode(grade, day, shift)`
- `PrecepteeSyncNode(day, shift, pair_id)`
- `OffWindowNode(nurse, day_range)`
- `CarryoverTransitionNode(nurse, boundary_type)`

## 2.3 Harness Rule Nodes
- `RuleNode(rule_id, group, severity)`
  - source: `tools/harness/rules/checklist_core.yaml`

## 2.4 Observation Nodes
- `MetricNode(metric_name, value, run_id)`
- `ViolationNode(rule_id, run_id, slack, evidence)`
- `RunNode(run_id, strategy, solver_status, input_hash)`

---

## 3) Hyperedge Types

하이퍼엣지는 다수 원인 노드를 하나의 결과 노드로 연결한다.

## 3.1 Cause edges
- `CAUSES_VIOLATION`
  - from: `{ConstraintNode+, EntityNode+, MetricNode}`
  - to: `ViolationNode`

예시:
`{GradeMinNode(g1,d,N), TeamMinNode(t2,d,N), OffWindowNode(nX,[d0,d1])} -> ViolationNode(D_N_MIN)`

## 3.2 Rule bind edges
- `EVALUATED_BY`
  - from: `MetricNode`
  - to: `RuleNode`

- `FAILED_RULE`
  - from: `ViolationNode`
  - to: `RuleNode`

## 3.3 Context edges
- `OBSERVED_IN`
  - from: `ViolationNode`
  - to: `RunNode`

- `RUN_ON`
  - from: `RunNode`
  - to: `{GroupNode, MonthNode}`

---

## 4) Mapping Table (Rule → Graph)

| Rule Group | Rule Example | Primary Constraint Nodes | Primary Evidence |
|---|---|---|---|
| A | `A_NOD` | `CarryoverTransitionNode`, `OffWindowNode` | nurse_id, day, prev_shift, next_shift |
| D | `D_D_MIN` | `CoverageMinNode`, `TeamMinNode`, `GradeMinNode` | day, shift, need, assigned |
| D | `D_MAX_OVER` | `CoverageMaxNode` | day, shift, max, assigned |
| G | `G_PRECEPTEE_SYNC` | `PrecepteeSyncNode`, `CoverageMinNode` | pair_id, day, shift, same_shift flag |
| H | `H_NO_INFEASIBLE` | (aggregate) | run status, reason_code list |

---

## 5) Data Source Binding

## 5.1 From API / runtime
- `infeasibility.preflight_issues[*].reason_code/evidence`
- `constraint_impact.violated_constraints[*].node_id/slack/details`
- `constraint_impact.preceptee_atom_count`
- `constraint_impact.coverage_excluded_atom_count`
- generated roster schedule (actual assigned counts)

## 5.2 From harness rule engine
- rule pass/fail
- metric value
- threshold / pass_condition

---

## 6) Node ID Canonicalization

규칙:
- Coverage min: `coverage:min:{day}:{shift}`
- Team min: `team_min:{team_id}:{day}:{shift}`
- Grade min: `grade_min:{grade}:{day}:{shift}`
- Rule: `rule:{rule_id}`
- Violation: `violation:{run_id}:{rule_id}:{index}`

---

## 7) Minimal JSON Exchange Schema

```json
{
  "run": {
    "run_id": "2026-05-g10135890c287-combined-r03",
    "group_id": "10135890c287",
    "year": 2026,
    "month": 5,
    "strategy": "COMBINED",
    "input_hash": "sha256:..."
  },
  "rules": [
    {"rule_id": "D_D_MIN", "metric": 3, "pass": false, "severity": "blocking"}
  ],
  "violations": [
    {
      "rule_id": "D_D_MIN",
      "node_ids": ["coverage:min:24:E", "team_min:2:24:E", "grade_min:1:24:E"],
      "evidence": {"day": 24, "shift": "E", "need": 3, "assigned": 1},
      "slack": -2
    }
  ]
}
```

---

## 8) Progressive Extension Rules

신규 체크리스트 항목 추가 시 필수:
1. `checklist_core.yaml` 규칙 등록
2. `mapping_registry`에 Rule→NodeType 매핑 등록
3. evidence schema 정의
4. golden pass/fail fixture 추가

---

## 9) Recommended Implementation Order

1. `D_*` coverage 규칙부터 그래프 매핑
2. `A_*` hard transition/recovery 규칙 확장
3. `G_PRECEPTEE_*` 규칙으로 프리셉티 영향 경로 추가
4. run 비교 API (4월 vs 5월 등) 추가

---

## 10) Reviewer Checklist

- [ ] 모든 blocking rule이 RuleNode로 생성되는가
- [ ] 위반 시 최소 1개 ConstraintNode와 연결되는가
- [ ] run_id/input_hash로 재현 가능한가
- [ ] reason_code와 hypergraph edge가 상충하지 않는가
