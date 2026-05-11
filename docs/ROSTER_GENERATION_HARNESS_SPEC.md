# Roster Generation Harness Spec (Mandatory QA Gate)

> 목적: 근무표 생성 시 하드제약/커버리지/후처리 품질을 **매번 자동 검증**하고,
> 설정 변경(Team/Grade/Preceptee/Max Coverage 등)의 영향 범위를 조기에 감지한다.

---

## 1) Scope & Principles

### 1.1 Mandatory Gate
- 본 하네스는 **생성 테스트의 필수 게이트**다.
- 체크리스트 항목(A~H)은 기본적으로 모두 평가한다.
- `severity=blocking` 항목 하나라도 실패하면 테스트 세트 전체를 실패 처리한다.

### 1.2 Why this harness
- 솔버/설정 변경 시 silent regression 방지
- “같은 조건”의 재현성 확보 (input snapshot + run metadata)
- 단일 run 결과가 아닌 반복 실행 분산(variance) 관측

### 1.3 Non-goals
- UI 설명 문서 대체 아님 (기존 frontend guide 유지)
- solver 내부 알고리즘 최적화 자체를 직접 수행하지 않음

---

## 2) Inputs / Outputs

## 2.1 Input Snapshot (per run-set)
- Auth context: office_id, group_id, user role
- Month context: year, month, num_days
- Config snapshot:
  - roster_config(version 포함)
  - daily_shift(month summary + by-day + max_enabled)
  - team(min_shift/handoff)
  - grade(min/max, allow_soft_fallback)
  - monthly_limits(optional)
  - assignments(active/completed overlap)
  - fixed_wanted entries

### Snapshot hash
- 위 입력을 canonical JSON으로 정렬 후 hash 생성
- 결과 리포트에 `input_hash` 저장

## 2.2 Output Artifacts
- `run_result.json`: 실행별 raw 결과 + 핵심 지표
- `summary.json`: 체크리스트 통과/실패 집계
- `diff_report.json`: baseline 대비 변동
- `triage.md`: 실패 항목 요약(원인 family/추천 조치)

---

## 3) Execution Model

## 3.1 Test Plan Types
1. **Baseline plan**: 현재 운영값 그대로 N회 반복 (권장 5~10회)
2. **Sweep plan**: 특정 파라미터 스윕 (team min / grade min-max / preceptee 등)
3. **Stress plan**: 극단값으로 save-time/precheck 차단 정상 동작 검증

## 3.2 Repeat policy
- stochastic/비결정성 관측을 위해 같은 조건을 최소 5회 반복
- 평가 지표:
  - success_rate
  - under_coverage_runs
  - over_coverage_runs
  - violation_family_frequency

## 3.3 Exit criteria
- blocking 항목 0건
- warning 항목은 허용 가능하나 threshold 초과 시 실패 전환 가능

---

## 4) Checklist Mapping (A~H)

아래는 사용자 제공 체크리스트를 하네스용 규칙으로 정규화한 표준이다.

### Rule schema
```json
{
  "id": "D_COVERAGE_D_MIN",
  "group": "D",
  "name": "일자별 D 최소 인원 충족",
  "severity": "blocking",
  "metric": "under_count(D)",
  "pass_condition": "== 0",
  "data_sources": ["generated roster", "daily_shift_requirements_by_day"],
  "owner_family": ["coverage", "team_min", "grade_min", "off_window"]
}
```

### Severity baseline
- **blocking(기본 0건)**: A 전부, D의 min 미달, F 핵심 이월 위반, H infeasible
- **warning(기본 threshold)**: C 균등 분배 계열, 일부 B 편차 계열

> 체크리스트 원문(A~H)은 그대로 유지하되, 각 항목에 rule id/metric/pass_condition을 매핑해 자동 평가한다.

---

## 5) Metric Definitions (핵심)

## 5.1 Coverage
- `under_count(shift)`: Σ_day max(need-assigned, 0)
- `over_count(shift)`: Σ_day max(assigned-need, 0)
- `under_days(shift)`: need>assigned 인 day 수
- `over_days(shift)`: assigned>need 인 day 수

## 5.2 Team/Grade
- `team_min_violation_count`: `team_min:*` node slack<0 수
- `grade_min_violation_count`: `grade_min:*` node slack<0 수
- `team_grade_intersect_shortage_count`: reason_code 기준

## 5.3 Preceptee impact
- `preceptee_atom_count`
- `coverage_excluded_atom_count`
- preceptee on/off A/B 시 under_count 변화율

## 5.4 OFF / swap
- off_swap 관련 로그 검출:
  - `[OffSwap][CALL]`
  - `[OffSwap][DONE] converted=N`

## 5.5 Stability
- `solve_time_ms`
- `solver_status` (primary/fallback)
- `infeasibility.severity`

---

## 6) Data Collection API Contract

필수 호출 순서(권장):
1. `/auth/me`
2. `/roster/config/version/v1`
3. `/daily-shift?office_id=&group_id=&year=&month=`
4. `/teams`
5. `/grade/config`
6. `/nurses/monthly-limits?year=&month=`
7. `/nurses/assignments?status=active`
8. `/wanted/fixed/{year}/{month}`
9. `POST /roster_create/generate` (N회)
10. `/roster/schedule/{schedule_id}` (실제 저장 결과 검증)

---

## 7) Failure Taxonomy (원인 분류)

하네스는 실패를 아래 family로 자동 태깅한다.
- `coverage`
- `team_min`
- `grade_min` / `grade_max`
- `team_grade_intersect`
- `preceptee`
- `off_window` / `assignment_window`
- `fixed_wanted`
- `carryover_prev_month`
- `config_integrity`

분류 근거:
- `infeasibility.preflight_issues[].reason_code`
- `constraint_impact.violated_constraints[].node_id`
- `constraint_impact` 집계값

---

## 8) Progressive Extension Model (점진 추가)

새 요구사항 추가 시 아래 절차를 강제한다.

1. **Rule 등록**
   - `rules/<domain>.yaml`에 id/group/severity/metric/pass_condition 정의
2. **Collector 연결**
   - 필요한 API/DB/로그 수집기 추가
3. **Evaluator 구현**
   - metric 계산 함수 추가
4. **Golden scenario 추가**
   - 최소 1개 pass/1개 fail fixture 등록
5. **CI 게이트 연결**
   - blocking fail 시 PR 실패

---

## 9) Recommended Repository Layout

```text
tools/harness/
  README.md
  runner.py
  collectors/
    api_client.py
    snapshot_collector.py
    roster_collector.py
  evaluators/
    hard_constraints.py
    coverage.py
    off_swap.py
    preceptee.py
    carryover.py
  rules/
    checklist_core.yaml
    checklist_extended.yaml
  reports/
    render_markdown.py
```

---

## 10) CI / 운영 적용 방안

## 10.1 CI Stage
- `harness-baseline` (빠른 2회)
- `harness-nightly` (5~10회 반복 + sweep)

## 10.2 Release Gate
- blocking rule fail 1건 이상: 배포 차단
- warning threshold 초과: 승인 필요

---

## 11) Immediate Next Steps (실행 순서)

1. 사용자 체크리스트(A~H)를 `checklist_core.yaml`로 규칙화
2. 수집기 우선순위 구현:
   - generate 결과 + roster/schedule + constraint_impact
3. coverage/under-over evaluator 우선 구현
4. precheck reason_code 매핑 evaluator 추가
5. preceptee A/B 시나리오 템플릿 추가

---

## 12) Notes

- 현재 프로젝트에서는 `coverage_gaps`가 under 중심으로만 해석될 수 있으므로,
  하네스는 **under/over를 별도 metric으로 독립 계산**한다.
- `constraint_impact`는 설명용 신호로 사용하고, 최종 합격 판정은
  저장된 roster 실배정(일자×shift 집계) 기준으로 내린다.

---

## 13) Linked Specs

- Rule source of truth:
  - `tools/harness/rules/checklist_core.yaml`
- Ontology/Hypergraph mapping:
  - `docs/HARNESS_TO_ONTOLOGY_HYPERGRAPH_MAPPING.md`
- Constraint impact graph base:
  - `docs/CONSTRAINT_IMPACT_GRAPH_SCHEMA_AND_API.md`
