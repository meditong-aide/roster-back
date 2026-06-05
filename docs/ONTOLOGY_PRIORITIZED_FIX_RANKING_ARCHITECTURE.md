# Ontology Prioritized Fix Ranking Architecture (Cross-Ward)

> 목적: 병동이 달라도(coverage/team/grade/nurse attrs/전월/fixed wanted 차이)  
> “근무표 생성불가 해결 우선순위”를 일관되게 산출하는 기준 구조를 정의한다.

---

## 1) 문제 정의

현재 infeasible 안내가 “권고 목록 나열”에 가까우면 병동별로 해석 오차가 발생한다.

- 일반병동(그룹 A): TeamMin 완화 단독으로 해소되는 케이스 존재
- 중환자실(그룹 B): Team/Grade/Night/일별 수요 완화를 각각/복합으로 건드려도 여전히 실패

따라서 구조는 다음을 분리해야 한다.

1. **레버가 실제로 해소효과를 가진 병목인지**
2. **레버를 바꿔도 안 풀리는 구조적 결핍인지**
3. **단일 레버 vs 복합 레버 vs 사전데이터 정비** 중 무엇이 맞는지

---

## 2) 공통 3-Stage 판정 구조

## Stage-G0: Structural Feasibility Gate (선행 게이트)

먼저 “완화 이전에 풀 수 있는 구조인지”를 판정한다.

입력(최소):
- `pool_snapshot` (team_pool/grade_pool/common_pool shortage)
- `violated_constraints.reason_code`
- carryover/fixed wanted 존재량

출력:
- `gate_status = PASS | HARD_BLOCK`
- `hard_block_reason = capacity_structural | role_isolation | carryover_lock | fixed_lock` (복수 가능)

규칙(예시):
- `NO_ASSIGNMENT` + 다수 pool shortage + 레버 변경 후에도 status 불변 → `capacity_structural`
- AllowedShift/role 제한으로 pool cover 불가 → `role_isolation`

> G0가 HARD_BLOCK이면 “레버 우선순위”보다 “구조 정비 우선순위”를 출력해야 함.

### G0 판정 규칙 (Low-sample 안전 모드)

가중치 점수 대신 결정 트리 규칙으로 판정한다.

1. `RULE_1`: 수학적 불가능 코드 존재
   - 예: `GRADE_MIN_SUM_EXCEEDS_NEED`, `TEAM_MIN_EXCEEDS_GLOBAL_NEED`, `CAPACITY_TOTAL_SHORTAGE`
   - 결과: `hard_block_structural`
2. `RULE_2`: `NO_ASSIGNMENT` + pool shortage 동시 존재
   - 결과: `hard_block_structural`
3. `RULE_3`: role/fixed/carryover 계열 충돌 존재
   - 결과: `mixed_relaxation_needed`
4. `RULE_4`: 위 규칙 불일치
   - 결과: `relaxation_candidate`

응답에는 `decision_trace`를 포함해 어떤 규칙이 매치됐는지 공개한다.

## Stage-G1: Cause Cohorting (대표 원인 묶음)

노드를 그대로 나열하지 말고 아래 단위로 묶는다.

- Cohort key: `(pattern_family, scope, causal_layer)`
- 보조 집계: `affected_count`, `affected_nurse_ids`, `pool_shortage_magnitude`

출력:
- 대표 원인 카드 N개 (보통 3~5개)
- 각 카드에 “영향 범위/근거 run/provenance” 표시

## Stage-G2: Ranked Fix Plan (단일/복합/실험)

점수식(권장):

`R(f) = 0.30*PolicyEase + 0.25*RootCauseStrength + 0.20*CoverageGainPotential + 0.15*BlastRadiusSafety + 0.10*Fixability`

추가 분기:
- **Single-lever mode**: 상위 1개 레버가 과거 유사문맥에서 단독 해소 이력 존재
- **Multi-lever mode**: 단독 효과 불충분, 상위 2~3개 조합 제시
- **Experiment mode**: 증거 충돌/부족 시 A/B 시나리오 2~3개 제시

---

## 3) 데이터 계약 (현재 코드와 매핑)

필수 필드:
- ontology: `relaxation_priority`, `scope_explosion`, `conflict_scenarios`
- run payload: `conflict_cores[]`, `violated_constraints[]`, `pool_snapshot.shortages[]`
- graph: `ConflictCoreNode`, `BLOCKED_RUN`, `MEMBER_OF_CONFLICT`
- G0 진단: `infeasibility.structural_diagnosis`
  - `mode`: `hard_block_structural | mixed_relaxation_needed | relaxation_candidate`
  - `primary_causes`: `capacity_structural | role_isolation | fixed_lock | carryover_lock`
  - `signals`: reason_code/shortage/conflict pattern/relaxation 카운트

권장 필드:
- 실행 이력: `{lever_set_applied, outcome, run_id, ward_id, month}`
- 구조 지표: `team/grade/role coverage margin`

---

## 4) 교차 병동 실험에서 얻은 공통 규칙

### 그룹 A (10135890c287)
- baseline FAIL
- TeamMin 완화 단독 PASS
- Grade/Night 단독 FAIL
- Team+Grade+Night 복합 PASS

해석:
- TeamMin 축이 1차 병목, Grade/Night는 2차 보조 레버

### 그룹 B (ICU, 10135857f9f9)
- baseline FAIL
- Team/Grade/Night/일별 N 수요/복합 모두 FAIL

해석:
- 단일/복합 레버 이전에 구조 결핍(HARD_BLOCK) 가능성 높음
- G0 게이트에서 `capacity_structural` 또는 `role_isolation`로 라우팅해야 함

### 공통 결론
- “추천 레버 나열”만으로는 불충분
- 먼저 G0로 “풀 수 있는 문제인지”를 판정해야 오해가 줄어듦

### 추가 결론 (극단 완화 실험 반영)
- ICU는 수요완화/TeamMin 0/Grade 완화/전이완화/야간상한 대폭완화/복합완화에도 실패
- 따라서 ICU류 케이스는 Recommendation 모드를 `hard_block_structural`로 강제하고,
  “레버 순위” 대신 “구조 진단 체크리스트(인력풀/역할제약/전월잠금/고정잠금)”를 우선 표출해야 함
- A병동류 케이스는 `solvable_with_relaxation`이더라도 품질 게이트(하네스 blocking)를 별도로 통과해야 완료 처리

---

## 5) 사용자 노출 포맷(권장)

1. **현재 상태 판정**
   - `생성불가 유형: 구조 결핍 / 제약 과밀 / 혼합`
2. **대표 원인 3개**
   - 원인 카드(영향범위 + 핵심 근거)
3. **우선순위 계획**
   - A안(단일) / B안(복합) / C안(구조정비)
4. **실행 후 기대 변화**
   - 어떤 reason_code/pattern이 줄어야 정상인지

---

## 6) 구현 순서

1. G0 판정기 추가 (`capacity_structural` 등)
2. Cause cohorter 추가 (대표 원인 카드 생성)
3. Ranker v1 적용 + tie-breaker 고정
4. 추천 출력에 `single|multi|experiment` 모드 명시

### 현재 반영 상태
- ✅ `app/services/precheck/structural_diagnosis.py` 추가
- ✅ `build_blocking_payload`, `build_unrecoverable_payload`, `build_success_payload`에 `structural_diagnosis` 필드 연결
- ✅ 회귀 테스트: `tests/test_structural_diagnosis_payload.py`
- ✅ conflict core member budget/collapse 메타 추가
  - `members_total`, `members_visible`, `members_collapsed_count`, `members_truncated`, `members_overflow_sample`
  - 적용 위치: `app/services/cp_sat/hard_assumption.py::_apply_member_budget`
