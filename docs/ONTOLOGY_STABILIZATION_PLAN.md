# 온톨로지 안정화 + 하드/소프트 제약 통제 실행계획

**작성일:** 2026-06-02
**상태:** 설계 확정 + 순차 구현 착수 (ralph 루프)
**선행 문서:** `RESEARCH_FIT_CONSTRAINT_IMPACT_GRAPH_GAP_ANALYSIS.md`, `UNIFIED_ONTOLOGY_GRAPH_SCHEMA.md`
**연구목표 근거:** Constraint Impact Graph + LLM Agent — solver 실패를 도메인 원인으로 변환하고, 최소 변경 복구 액션을 제시·검증.

---

## 0. 한 줄 목표

온톨로지를 MUS(solver conflict) 단일 의존에서 떼어내 **결정론적 구조 grounding(max-flow) + MUS 보조**의 이중 토대로 안정화하고, 하드·소프트 제약을 **4-노드 통합 그래프** 위에서 균일하게 체크·통제하며, **각 병동 5/6월 데이터로 하드위반을 주입→액션 도출→최소변경 해결을 폐루프로 검증**한다.

---

## 1. 안정화의 핵심 원리 (왜 불안정했나 → 어떻게 고치나)

실증된 불안정(`test_conflict_core_real_solver.py`):
- 하드 제약이 `add_hard`로 wrap된 경우에만 MUS가 원인을 잡고, team/grade/coverage 등 대부분은 wrap 밖 → `conflict_cores=[]` 침묵.

해결: **이중 grounding**
```
[안정 floor] max-flow 수요·공급 점검 (결정론적, solve 이전에도 동작, 항상 답)
      +
[보강] MUS (assumption wrap 확장 시 신뢰)
      ↓
[통합] 4-노드 그래프(constraint/domain_object/state/action)로 합류
      ↓
[통제] ActionNode (set_threshold/force_soft/disable) — 최소변경 우선순위로 랭크
```

---

## 2. 구현 스토리 (순차)

### US-001 — Supply/Demand Max-Flow State 산출기
- `app/services/ontology_graph/supply_demand.py` 신규.
- 입력: nurses(eligibility/grade/team) + daily_shift_requirements + fixed/off.
- day×shift별 `required / available / shortage` 산출. min-cut 으로 구조적 병목 셀 식별.
- 출력: `StateNode(state_type="supply_demand")` 리스트 + 병목 엣지.
- 이게 "항상 작동하는 결정론적 바닥" — assumption-wrap 불필요.

### US-002 — Soft 제약 온톨로지 등재
- `ontology.yaml` 에 soft family 추가(`severity: soft`): night_deviation, isolated_work/off, nod_noe, experience_short, week_off_short, kld 분포.
- 각 soft penalty(`hardcoded_weights.py` / `objective_terms.py`)를 family에 매핑 + weight 를 control 노브로 등록.
- 3-stage lex(coverage>safety>quality) 계층을 family 메타로 표기.

### US-003 — 통합 그래프 빌더 (하드+소프트+상태+객체 합류)
- `app/services/ontology_graph/builder.py` 신규.
- `SemanticsSnapshot`(+ DB raw) → 4-노드 그래프 생성:
  - ConstraintNode(hard/soft), DomainObjectNode(nurse/day/shift/team/grade/leave/wanted_off),
    StateNode(supply_demand from US-001 + nurse_load), ActionNode(from US-002 + control.py).
  - 엣지: constrains/requires/supplied_by/reduces/belongs_to/pressures/mitigates/derived_from.

### US-004 — Action 추천 + 최소변경 랭킹
- 통합 그래프에서 `pressures`(shortage→constraint) 경로를 따라 `mitigates` ActionNode 후보 수집.
- `relaxation_priority` + 변경 규모로 **최소변경 우선** 랭킹 (greedy hitting set).
- 출력: ranked actions(각각 config delta + 예상 영향).

### US-005 — 오프라인 병동 Eval 하네스 (프로덕션 무변경)
- `tools/ontology_eval/harness.py` 신규.
- 병동 raw 데이터를 **read-only** 로드(MSSQL `db.client2`, JWT 오프라인 디코드) → `generate_roster_cp_sat` **raw 진입점** 직접 호출(스케줄 DB 쓰기 없음).
- 하드위반 주입은 **in-memory config 사본**에만 적용 → 실행 후 폐기 → 프로덕션 무변경(되돌릴 것 없음).
- 병동: 9B(`10135890c287`), ICU(`10135857f9f9`), group(`run_june_gen_group`) × {2026-05, 2026-06}.

### US-006 — 폐루프 평가 실행 (전 병동 통과까지)
- 사전 정의 평가항목(§3)으로 각 (병동×월×주입케이스) 평가.
- 액션대로 최소변경 적용 → 재실행 → 해결 여부 측정 → 미해결 시 원인분석·개선 → 재측정 반복.

---

## 3. 평가항목 (사전 정의 — 측정 가능)

각 (병동 × 월 × 주입 하드위반 케이스)에 대해:

| 코드 | 평가항목 | 측정 / 통과 기준 |
|---|---|---|
| **E1. 액션 도출** | infeasible에서 실제 해결 액션이 나왔는가 | ranked actions 비어있지 않고, 1순위가 주입한 위반 family를 타겟 |
| **E2. 해결성** | 액션 적용 후 feasible 해졌는가 | 액션 in-memory 적용 → 재실행 status=feasible(work_cells>0, status≠INFEASIBLE) |
| **E3. 최소변경** | 최소 변경으로 풀렸는가 | 적용 delta가 사전 정의 `minimality_budget` 이내 (예: 단일 family·단일 노브·정수 step ≤ 주입량). 과도완화(전면 disable 등)면 실패 |
| **E4. 무회귀** | 해결이 새 하드위반을 만들지 않았는가 | 재실행 결과 server-side hard violation 0 |
| **E5. 정직성** | 원인이 결과론(NO_ASSIGNMENT)이 아닌 구조원인인가 | cause가 supply_demand/structural 노드로 grounding (symptom 단독 노출 금지) |

미해결(E2 fail) 또는 과잉(E3 fail) 시: 원인 분석 → 빌더/랭킹/액션 매핑 개선 → 재측정. **전 병동 모든 케이스 E1–E5 통과 시 종료.**

주입 하드위반 케이스(대표):
- C1 coverage_min 과다 (특정일 demand > 가용)
- C2 team_min 과밀 (팀 최소 > 팀 가용)
- C3 grade_max 과소 (등급 상한 < 필요)
- C4 night cap 과소 (월 N 상한 < N 수요)

---

## 4. 안전 원칙

- **프로덕션 DB 무변경**: 모든 주입은 in-memory config 사본. 스케줄 영속화 경로 미사용.
- 하네스는 read-only 쿼리만. write/commit 금지.
- 각 실행은 격리된 config dict 사본으로 수행, 원본 불변.

---

## 5. 진행 추적

- PRD: `.omc/prd.json` (US-001 ~ US-006)
- 진행 로그: `.omc/progress.txt`
- 평가 결과: `tools/ontology_eval/reports/*.json`
