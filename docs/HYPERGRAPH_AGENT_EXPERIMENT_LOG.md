# Hypergraph Agent Experiment Log (1~5)

> 원칙: 디테일 최소, 무엇을 했고/무슨 결과였고/핵심 노드가 무엇인지 중심 기록

---

## Exp-1: Baseline 실행 준비
- 무엇을 했나: harness 실행 경로/입력(토큰, 월, 전략) 확인
- 결과: `tools/harness/runner.py` 기준으로 token cookie가 필수임을 재확인
- 핵심 노드 관점: baseline run 노드에서 inbound conflict 묶음을 우선 확인하도록 절차 고정

## Exp-2: 인증/실행 가능성 점검
- 무엇을 했나: 기존 토큰들로 `/auth/me` 재검증, 세션 로그 토큰 재수집 시도
- 결과: 현재 시점 기준 기존 토큰 모두 401(만료)
- 핵심 노드 관점: 새 실행 run 생성이 불가하여 신규 hypergraph 비교 실험 보류

## Exp-3: 원인 해석 로직 정리(오프라인)
- 무엇을 했나: 기존 run artifact/노드 해석 규칙 재정리
- 결과: “노드 수 = 원인 종류 수”가 아님을 재확인, 패턴 반복은 cohort 영향으로 해석
- 핵심 노드: `cpsat_mus:max_night` 반복군, `n_only_vs_caps`, `infeasibility:no_assignment`

## Exp-4: 해결 레버 설계 반영
- 무엇을 했나: 대표 원인별 우선 완화 레버 표준화 문서 반영
- 결과: 50케이스 매트릭스에서 각 케이스에 대표 원인/완화 레버 명시 완료
- 핵심 노드 관점: `multi_nurse affected_count` 코어를 대표 원인으로 우선 노출

## Exp-5: 자동화 준비 상태 점검
- 무엇을 했나: 실행 템플릿(주입→generate→payload→graph→ontology node→원복) 정리
- 결과: 토큰만 유효하면 즉시 재개 가능한 상태
- 핵심 노드 관점: top-level pattern + member 경로 동시 검증이 필수

---

## 현재 판단(중간)
- 왜 해석이 다를 수 있었나:
  1) 동일 패턴의 nurse별 반복 노드를 원인 다종으로 오해
  2) top-level pattern 미노출 시 member 경로를 안 보면 누락처럼 보임
  3) data_quality 코어와 solver MUS 코어를 분리하지 않으면 과대해석됨

## 다음 실행 조건
- 유효한 access token 1개 확보 즉시:
  1) baseline harness 재실행
  2) 온톨로지 제안 해결안 1~5 적용
  3) 재실행 결과 비교(핵심 노드/대표 원인/완화 레버 변화)

---

## Exp-6: 토큰 확보 후 실제 1~5 실험 실행 (2026-07)

- 실행 방식: endpoint 저장 → harness 1회(COMBINED) → 결과 비교
- 기준 run: 2026-07 / group `10135890c287`

### 시나리오와 결과

1) **Baseline**
- 조치: 없음
- 결과: `status_code=500`, reason=`NO_ASSIGNMENT`, harness `FAIL`
- 핵심: 전역 infeasible (핵심 힌트: team/grade 최소요구 완화 권고)

2) **S1 TeamMin 완화(팀별 min_shift 일부 -1)**
- 조치: `/teams` PUT로 최소요구 완화
- 결과: `status_code=200`, schedule 생성, harness `PASS`
- 핵심: 대표 원인 = **TeamMin 계열 제약 과밀**

3) **S2 GradeMax 완화(constraints_max +1)**
- 조치: `/grade/config` POST로 max 상향
- 결과: `status_code=500`, `NO_ASSIGNMENT`, harness `FAIL`
- 핵심: grade 상한만 완화해서는 해소 안 됨

4) **S3 월 N 상한 완화(max_nig_per_month +2)**
- 조치: `/roster/config/save`
- 결과: `status_code=500`, `NO_ASSIGNMENT`, harness `FAIL`
- 핵심: 야간 상한 단독 완화는 효과 제한

5) **S4 일별 N 요구 1일 완화(N_count day10 -1)**
- 조치: `/daily-shift` PUT
- 결과: `status_code=500`, `NO_ASSIGNMENT`, harness `FAIL`
- 핵심: 단일 일자 수요 완화는 구조 충돌 해소 부족

6) **S5 복합 완화(TeamMin + GradeMax + NightCap)**
- 조치: S1+S2+S3 동시
- 결과: `status_code=200`, schedule 생성, harness `PASS`
- 핵심: 단일 레버보다 **복합 완화**에서 해소됨

### 해석 차이 판단

- 왜 다르게 해석됐나:
  - baseline 메시지는 team/grade/night를 모두 제시하지만 실제로는 **TeamMin 축이 1차 병목**
  - Grade/Night 완화 단독은 실패, TeamMin 완화는 즉시 성공 → 원인 우선순위 재정렬 필요
- 개선 포인트:
  - 하이퍼그래프 안내문에서 "권고 리스트"를 동순위로 제시하지 말고
    1) 단독 효과 확인된 레버(TeamMin)
    2) 복합 필요 레버(Grade/Night)
    순으로 랭크 표기 필요

---

## Exp-7: ICU 그룹 교차 검증 (2026-07, group=10135857f9f9)

- 실행: baseline + (TeamMin 완화 / GradeMax 완화 / NightCap 완화 / 일별 N 수요 완화 / 복합 완화)
- 결과: **모든 시나리오 FAIL 유지**

요약:
- 일반병동에서는 TeamMin 단독 완화가 효과가 있었지만,
- ICU에서는 같은 레버 세트로도 해소되지 않음

핵심 판단:
- ICU는 단순 레버 우선순위 이전에 **구조 결핍 게이트(G0)** 가 필요
- 즉, “무엇을 먼저 풀지” 전에 “현재 구조가 풀 수 있는 상태인지”를 먼저 판정해야 함

---

## Exp-8: 다중 중첩 시나리오 확장 (A/B 병동 공통 10케이스)

- 케이스: baseline, team_relax, grade_relax, nightcap_relax, dailyN_relax, transition_off, not_one_night_off, off_days_plus1, combo_team_grade, combo_heavy

### A 병동(10135890c287)
- PASS: `team_relax`, `combo_team_grade`
- FAIL: 그 외 대부분
- 특이: `combo_heavy`는 생성(200)되었지만 harness blocking fail 3으로 최종 FAIL

### ICU 병동(10135857f9f9)
- 10개 전부 FAIL (모두 500 / blocking fail 유지)

### 핵심 관찰
- A 병동은 Team 축 완화에 민감(효과 있음)
- ICU는 단일/복합 완화 모두 비효과 → 레버 튜닝보다 구조 결핍 가능성 우세
- 따라서 추천 엔진은 병동 공통으로
  1) 레버 추천 전에 G0 구조 게이트
  2) PASS 경험이 있는 레버를 상위 랭크
  3) 생성 성공(200)과 품질 FAIL(harness blocking) 분리 표기
  를 반드시 가져가야 함

---

## Exp-9: 극단 완화 시나리오 추가 (A/B 병동 공통 6케이스)

- 케이스: `low_demand_all`, `teammin_zero_all`, `grade_off`, `trans_not1n_off`, `night_relax_big`, `all_relax_extreme`

### ICU(10135857f9f9)
- 6개 전부 FAIL(500/blocking fail 1 유지)
- 해석: 레버 튜닝으로는 해소가 어려운 구조 결핍 신호 강화

### A병동(10135890c287)
- 일부 시나리오에서 200 생성되지만 harness blocking fail 유지
- 해석: 생성 가능성과 품질/정책 만족이 분리되어 있음

### 최종 관찰(현재)
- ICU는 “레버 완화 우선순위”보다 “구조/데이터 정비 우선순위”가 선행되어야 함
- A병동도 단순 생성 성공을 해결 완료로 볼 수 없음 (품질 게이트 별도)

---

## Exp-10: 9B 추가 케이스 (fixed/carryover/role-pressure mix)

- 대상: group `10135890c287` (9B 토큰)
- 케이스: baseline, fixedwanted_pair, transition_not1n_off, structural_relief_mix, night_off_relax_big

결과:
- FAIL: baseline / fixedwanted_pair / transition_not1n_off / night_off_relax_big
- PASS: `structural_relief_mix` (team_min 전면완화 + grade 완화 + 수요완화)

해석:
- 9B는 단일 레버 완화보다 **복합 구조 완화**에서 해소 가능성이 높음
- fixed wanted 2건 주입 자체는 결정적 병목이 아니었고,
- 전이/1N 완화 단독도 효과 제한

---

## Exp-11: 타겟 제약 케이스 검증 (질의 대응)

- 대상 그룹: `10135890c287` (9B)
- 케이스:
  1) `n_exact + N-only 다수` (N-only 2인 n_exact 상향)
  2) `주말휴무자 + fixed wanted O 과다 + O cap 압박`
  3) `월 N cap 사실상 해제(31)`

결과:
- baseline 포함 4개 모두 FAIL(500 / blocking fail 1)

해석:
- 위 케이스들은 단일 레버 조정으로는 해소되지 않았고,
- 기존 결론(복합 구조 완화 필요, 또는 구조 결핍 게이트 선행)과 일치

---

## Exp-12: CarryoverBoundary wrap 적용 후 재검증

- 코드: 월경계 3N2OFF/2N2OFF 회복 제약에 `CarryoverTransitionNode` wrap 추가 (primary/fallback)
- 9B 재실행 케이스: baseline / transition_not1n_off / team_zero_all

결과:
- baseline, transition_not1n_off: 500 유지, `conflict_cores` 비어있음
- team_zero_all: 200 생성

해석:
- 이번 케이스들은 precheck/사전 불가능 단계에서 소진되어 MUS core가 생성되지 않아 carryover pattern 가시화까지는 도달하지 못함
- carryover wrap 자체는 적용됐고, 노출 확인은 "solver infeasible + precheck 통과" 케이스에서 추가 검증 필요

---

## Exp-13: CarryoverBoundary conflict core 노출 회귀 테스트 추가

- 목적: 실험 환경(precheck 소진/성공경로)과 무관하게, `carryover_boundary` wrap 메타가 `conflict_cores` payload로 보존되는지 자동 검증
- 코드: `tests/test_carryover_conflict_core_wrap.py` 신규 추가
  - 케이스1) nurse scope 단일 코어에서 `pattern=cpsat_mus:carryover_boundary`, `CarryoverTransitionNode`, `causal_layer=structural` 확인
  - 케이스2) non-nurse(scope=grade) 다중 코어 그룹핑 시 `affected_scope_keys` 보존 확인
- 결과: 신규 테스트 2개 PASS (`python -m pytest tests/test_carryover_conflict_core_wrap.py -q`)

해석:
- 라이브 토큰/데이터 상태와 무관하게 carryover wrap의 payload 경로는 회귀 방어가 걸린 상태.
- 남은 검증 포인트는 라이브 endpoint 실험에서 실제 solver-infeasible run이 발생했을 때 동일 패턴이 관측되는지의 운영 재현성 확인.

---

## Exp-14: ICU/9B 라이브 endpoint 재검증 시도 (인증 블로커 기록)

- 목표: ICU(2026-06), 9B(2026-07)에서 endpoint 기반(`config save -> generate`)으로 `carryover_boundary` 실제 노출 재확인
- 수행:
  - 이전 세션 토큰 복구 후 인증 3방식 재시도
    1) `Authorization: Bearer <token>`
    2) `Cookie: access_token=<raw_jwt>`
    3) `Cookie: access_token=Bearer <jwt>`
  - 검증 endpoint: `GET /auth/me`
- 결과:
  - 3방식 모두 `401 Not authenticated`
  - 현재 세션에서 라이브 MSSQL endpoint 실험은 인증 만료로 진행 불가

해석:
- 이번 블로커는 코드/로직 문제가 아니라 인증 세션 유효성 문제.
- 따라서 라이브 재현성 검증(운영 데이터에서 `carryover_boundary` 노출 확인)은 유효 토큰 재발급 후 즉시 재개 가능.
- 오프라인 회귀 방어(Exp-13)는 PASS 상태 유지(신규 carryover 테스트 2개 PASS, 관련 구조진단/멤버예산 7개 PASS).

---

## Exp-15: 다음 wrap — carryover boundary(3N2OFF/2N2OFF) 잔여 하드 식 assumption 래핑

- 목표: 월경계 carryover 구간에서 아직 직접 `m.Add(...).OnlyEnforceIf(...)`로 남아 있던 하드 식을 assumption wrap으로 편입
- 반영 파일:
  - `app/services/cp_sat_basic.py`
  - `app/services/cp_sat/fallback_lex.py`
- 반영 범위:
  - 3N2OFF tail 강제식(월초 2OFF)
  - 3N 블록 가드식(회복 OFF 슬롯 fixed 충돌 시)
  - 2N2OFF 월초 boundary 강제식
  - 모두 `CarryoverTransitionNode`, `pattern=carryover_boundary` 메타로 통일

검증:
- `python -m py_compile app/services/cp_sat_basic.py app/services/cp_sat/fallback_lex.py` 통과
- `python -m pytest tests/test_carryover_conflict_core_wrap.py -q` 통과(3 passed)

해석:
- carryover 계열 제약의 온톨로지 노출 범위를 월경계 시작점뿐 아니라 잔여 tail/guard 경로까지 확장함.
- 라이브 endpoint 검증은 여전히 유효 토큰 재발급 후 재개 필요(Exp-14 블로커 동일).

---

## Exp-16: mixed-pattern 그룹핑 안정화 (first-seen bias 완화)

- 목표: 동일 nurse scope core 안에 여러 pattern이 섞일 때 first-seen pattern으로 고정되어 카드가 오해되는 문제 완화
- 반영 파일:
  - `app/services/cp_sat/hard_assumption.py`
  - `tests/test_conflict_core_mixed_pattern_normalization.py` (신규)

변경점:
- core 구성 시 `pattern_candidates`를 누적
- 후보가 2개 이상이면 대표 `pattern`을 `cpsat_mus:mixed`로 정규화
- `derivation`에 pattern normalization 단계 추가

검증:
- `python -m pytest tests/test_conflict_core_mixed_pattern_normalization.py tests/test_carryover_conflict_core_wrap.py -q` 통과 (5 passed)
- `lsp_diagnostics` (`hard_assumption.py`, severity=error) 0건

해석:
- mixed-core에서 representative label이 입력 순서에 따라 달라지는 불안정을 줄였고,
- downstream UI/agent가 `pattern_candidates`를 활용해 세부 원인 후보를 함께 보여줄 수 있게 됨.

---

## Exp-17: 라이브 endpoint 재개 (9B/ICU, 2026-06/07)

- 목표: 실제 endpoint 루프(`auth -> group switch -> generate`)에서 carryover/mixed pattern 노출 확인
- 산출물: `tools/harness/reports/experiment_results_live_auth_resumed.json`

실행 요약:
- 인증 복구 후 `/auth/me` 200 확인
- 9B(group=10135890c287)
  - 2026-07: generate 500, `violated_constraints.reason_code=NO_ASSIGNMENT`, `conflict_cores=[]`
  - 2026-06: generate 500, `conflict_cores=[]`
- ICU(group=10135857f9f9, `/auth/switch-group` 200)
  - 2026-06: generate 500, `conflict_cores=[]`
  - 2026-07: generate 200 (schedule 생성), `conflict_cores=[]`

해석:
- 현재 라이브 경로에서는 carryover/mixed 패턴 노출 이전에 `NO_ASSIGNMENT` 또는 기타 초기 infeasibility로 종료되는 케이스가 많아 conflict core가 비어 있음.
- 즉 이번 코드 변경의 회귀 안전성(Exp-13/15/16 테스트 PASS)은 확보됐고, 운영 데이터에서 패턴을 보려면
  1) `NO_ASSIGNMENT`를 유발하는 입력 상태(고정/커버리지/허용 시프트/휴가 분포) 정리,
  2) precheck 통과 + solver infeasible 구간으로 유도하는 케이스 설계
  가 선행되어야 함.

---

## Exp-18: /ontology 실행형 수정안 반영 (fix_plan)

- 배경: `NO_ASSIGNMENT` 케이스에서 결과 노드만 보이고 "무엇을 먼저 수정할지"가 약하다는 운영 피드백 반영

반영 내용:
- 신규: `app/services/precheck/fix_plan.py`
  - 입력: `structural_diagnosis`, `preflight_issues`, `violated_constraints`, `conflict_cores`, `pool_snapshot`
  - 출력: 우선순위 액션 목록(`actions[]`) + 근거(`reason_codes`, shortage targets)
- 연결: `app/services/precheck/payload.py`
  - `build_blocking_payload`, `build_success_payload`, `build_unrecoverable_payload` 모두 `infeasibility.fix_plan` 포함
- 노출: `app/routers/ontology.py`
  - `GET /ontology/conflict_summary` 응답에 `fix_plan` 필드 추가 (최근 attempt의 infeasible_detail에서 추출)

테스트:
- `tests/test_structural_diagnosis_payload.py` 확장
  - `fix_plan` 존재 검증
  - shortage 기반 1순위 액션(`adjust_coverage_or_supply`) 검증
- 실행 결과: `python -m pytest tests/test_structural_diagnosis_payload.py -q` → 6 passed

보강(신뢰도/해석 모드):
- `conflict_cores=[]` 인 경우 `fix_plan.plan_mode=hypothesis_checks`로 표기
- action별 `confidence` 추가(주로 low/medium)
- `/ontology/conflict_summary`에 `fix_plan_context`(run_id, generated_at) 추가해 stale 해석 방지

---

## Exp-19: 다음 wrap — NO_ASSIGNMENT 세분화 + pool/action 링크 + mixed 후보 노출

반영:
- `app/services/precheck/fix_plan.py`
  - `no_assignment_breakdown` 필드 추가
  - 분류 축: `capacity_shortage`, `eligibility_lock`, `fixed_lock`, `carryover_lock`
- `app/routers/ontology.py`
  - `fix_plan_links` 추가 (action target pool_id ↔ 그래프 pool node 존재 여부)
  - conflict core / operator card에 `pattern_candidates` 노출

테스트:
- `tests/test_structural_diagnosis_payload.py` 확장
  - `NO_ASSIGNMENT` 분해 축(capacity/eligibility/carryover) 검증
- 회귀 실행:
  - `python -m pytest tests/test_structural_diagnosis_payload.py tests/test_conflict_core_mixed_pattern_normalization.py tests/test_carryover_conflict_core_wrap.py -q`
  - 결과: 12 passed

해석:
- 이제 /ontology에서 `NO_ASSIGNMENT`를 단일 결과 노드로만 보지 않고,
  "어떤 원인 축을 먼저 수정해야 하는지"를 분해해서 볼 수 있음.

---

## Exp-20: NO_ASSIGNMENT direct-reason 케이스 매트릭스 확장 + 자동테스트

반영:
- 신규 문서: `docs/NO_ASSIGNMENT_DIRECT_REASON_CASE_MATRIX.md`
  - canonical sub-reason 4축
  - evidence 매핑 규칙
  - 24케이스 카탈로그(단일/복합)
  - direct reason migration plan (`NO_ASSIGNMENT_*` 세부코드)

- 신규 테스트: `tests/test_no_assignment_case_matrix.py`
  - 다양한 조합 케이스에서 `fix_plan.no_assignment_breakdown` 정확성 검증
  - shortage target이 1순위 액션에 연결되는지 검증

수정/보강:
- `app/services/precheck/fix_plan.py`
  - fixed 신호와 carryover 신호 분리
  - `initial_forbidden -> fixed_lock`, `PREV_* -> carryover_lock` 누락 보강

검증:
- `python -m pytest tests/test_no_assignment_case_matrix.py tests/test_structural_diagnosis_payload.py tests/test_conflict_core_mixed_pattern_normalization.py tests/test_carryover_conflict_core_wrap.py -q`
- 결과: 24 passed

해석:
- 현재는 signal-based 분해지만, direct-reason 전환 시 동일 매트릭스로 회귀 방어 가능.

---

## Exp-21: E2E 폐루프 검증 (endpoint 기반, 보수적 단계 조정)

목표:
- `generate -> infeasible 확인 -> fix_plan 확인 -> 소폭 endpoint 수정 -> 재생성` 폐루프 검증

산출물:
- `tools/harness/reports/e2e_fix_plan_closed_loop_9B_2026_07.json`
- `tools/harness/reports/e2e_fix_plan_team_step_9B_2026_07.json`

실행 결과 요약:
1) 1차 소폭 조정(구성 설정 단일 항목)
   - 변경: `max_conseq_work` 5 -> 6 (단일 +1)
   - 결과: `500 -> 500` (여전히 `NO_ASSIGNMENT`)
   - 해석: 가드레일대로 단일 변경으로 원인이 풀리지 않을 수 있음을 확인

2) 2차 소폭 조정(팀 최소 수요 1칸)
   - 변경: Team 2 `min_shift.D` 1 -> 0 (단일 -1)
   - 결과: `500 -> 200`, `schedule_id=8665f0d7cacc`
   - 이후 원복: Team 2 `min_shift.D` 0 -> 1 복원 완료

결론:
- 과격한 일괄 해제 없이도, 작은 단계 조정(1칸)으로 infeasible 해소 가능 케이스를 E2E로 확인.
- "큰 폭 변경 금지 + 단계별 재실행" 가이드가 실제로 유효함.

---

## Exp-22: 마지막 마감 작업 — direct reason emit + /ontology UI 렌더 마무리

### A) Direct reason emit (NO_ASSIGNMENT 4축)

- 파일: `app/services/roster_create_service.py`
- 변경:
  - `_extract_unrecoverable_violated_constraints`에서 `NO_ASSIGNMENT_*` direct reason 추가 emit
    - `NO_ASSIGNMENT_CAPACITY`
    - `NO_ASSIGNMENT_ELIGIBILITY`
    - `NO_ASSIGNMENT_FIXED`
    - `NO_ASSIGNMENT_CARRYOVER`
  - 기존 reason extraction 후 규칙 기반 추가 분해를 적용하여 `violated_constraints`에 함께 노출

### B) fix_plan direct precedence

- 파일: `app/services/precheck/fix_plan.py`
- 변경:
  - direct reason이 존재하면 해당 분해축을 source-of-truth로 우선 채택
  - `fix_plan.reason_source` 필드 추가 (`direct` | `inferred`)

### C) /ontology UI 렌더 마감

- 파일: `app/routers/ontology.py` (내장 JS)
- 변경:
  - conflict summary 상단에 `fix_plan` 영역 렌더
    - `reason_source`, `plan_mode`, `no_assignment_breakdown` 배지 노출
    - `fix_plan_links` (action → pool 연결) 리스트 노출
  - cause/operator 카드에 `pattern_candidates` 노출

### 검증

- 신규 테스트:
  - `tests/test_no_assignment_direct_reason_emit.py`
  - `tests/test_no_assignment_case_matrix.py` direct precedence 검증 추가
- 실행:
  - `python -m pytest tests/test_no_assignment_direct_reason_emit.py tests/test_no_assignment_case_matrix.py tests/test_structural_diagnosis_payload.py tests/test_conflict_core_mixed_pattern_normalization.py tests/test_carryover_conflict_core_wrap.py -q`
- 결과: **27 passed**

---

## Exp-23: Explainable Validator 업그레이드 실구현 + E2E 실증

구현:
- `app/services/roster_create_service.py`
  - `_collect_validator_evidence(...)` 추가
    - day/shift별 `required/assigned/eligible/shortage` 스냅샷 수집
    - `top_failed_cells`, `eligible_zero_cells`, `required_minus_assigned_total` 요약
  - `_validate_generated_roster(...)`에서 evidence를 `roster_system._validator_evidence`로 저장
  - `_extract_unrecoverable_violated_constraints(...)` direct reason 추론 시 evidence 반영
    - `NO_ASSIGNMENT_CAPACITY/ELIGIBILITY/FIXED/CARRYOVER`
    - details에 `validator_evidence` 포함

- `app/services/precheck/payload.py`
  - `validator_evidence_summary` 필드 추가
  - `violated_constraints.details.validator_evidence`를 요약해 payload에 노출

테스트:
- 신규/보강:
  - `tests/test_no_assignment_direct_reason_emit.py`
  - `tests/test_structural_diagnosis_payload.py`
- 회귀 실행:
  - `python -m pytest tests/test_no_assignment_direct_reason_emit.py tests/test_no_assignment_case_matrix.py tests/test_structural_diagnosis_payload.py tests/test_conflict_core_mixed_pattern_normalization.py tests/test_carryover_conflict_core_wrap.py -q`
  - 결과: **29 passed**

E2E 실증:
- 리포트: `tools/harness/reports/e2e_validator_explainable_9B_2026_07.json`
- baseline (9B 2026-07):
  - status 500
  - reason_codes: `NO_ASSIGNMENT`, `NO_ASSIGNMENT_CAPACITY`
  - fix_plan.reason_source: `direct`
  - validator_evidence_summary 포함 (`total_failed_cells=93`, `required_minus_assigned_total=248`)
- 소폭 조정(Team2 D 최소 1 -> 0) 후 재실행:
  - status 200, `schedule_id=baf9be58f1a3`
  - 변경 원복 완료

결론:
- validator 실패에서도 결과 라벨만이 아니라 direct reason + 근거(evidence)를 함께 제공하는 경로가 실증됨.

---

## Exp-24: Direct reason 커버리지 확장 (fixed/carryover evidence 포함)

반영:
- `app/services/roster_create_service.py`
  - validator evidence에 추가 집계:
    - `fixed_forbidden_count`
    - `carryover_artifact_count`
  - `NO_ASSIGNMENT_*` 분해 시 evidence 기반 규칙 확장:
    - `NO_ASSIGNMENT_FIXED` (fixed/forbidden 단서)
    - `NO_ASSIGNMENT_CARRYOVER` (carryover artifact 단서)
- `app/services/precheck/payload.py`
  - `validator_evidence_summary`에 위 2개 필드 포함

테스트:
- 회귀 실행:
  - `python -m pytest tests/test_no_assignment_direct_reason_emit.py tests/test_no_assignment_case_matrix.py tests/test_structural_diagnosis_payload.py tests/test_conflict_core_mixed_pattern_normalization.py tests/test_carryover_conflict_core_wrap.py -q`
  - 결과: **30 passed**

endpoint 샘플 확인:
- `tools/harness/reports/validator_direct_reason_sample_9B_2026_07.json`
- 확인값:
  - `reason_codes = [NO_ASSIGNMENT, NO_ASSIGNMENT_CAPACITY, NO_ASSIGNMENT_FIXED]`
  - `reason_source = direct`
  - `validator_evidence_summary.fixed_forbidden_count = 124`



의미:
- 이제 `NO_ASSIGNMENT`라도 /ontology에서 단순 결과 표시를 넘어,
  "수요/공급, allowed-shift/role, fixed/carryover" 축으로 바로 조치 우선순위를 제공할 수 있음.
