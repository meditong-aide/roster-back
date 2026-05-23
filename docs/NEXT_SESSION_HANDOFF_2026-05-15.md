# Next Session Handoff (2026-05-15)

## 1) 현재 상태 요약

### 핵심 진척
- `NO_ASSIGNMENT`를 단일 결과 라벨로만 보지 않고 direct reason으로 분해하는 경로를 강화함.
  - `NO_ASSIGNMENT_CAPACITY`
  - `NO_ASSIGNMENT_ELIGIBILITY`
  - `NO_ASSIGNMENT_FIXED`
  - `NO_ASSIGNMENT_CARRYOVER`
- validator evidence를 수집/노출하도록 보강함.
  - `total_failed_cells`, `eligible_zero_cells`, `required_minus_assigned_total`
  - `fixed_forbidden_count`, `carryover_artifact_count`
  - `top_failed_cells`
- `/ontology` UI에서 run 선택 시 conflict summary(대표 원인 포함) 자동 표시되도록 변경함.
  - 별도 버튼 클릭 없이 자동 노출
  - 첫 진입 시 first RunNode 자동 선택

### reason 비어있던 이슈 해결
- 일부 실패가 문자열 detail로 떨어질 때 `reason_codes=[]`가 되던 문제를 라우터 fallback으로 보강.
- `app/routers/roster_create.py`에서 비구조화 예외를 표준 infeasibility payload로 변환하도록 추가.
  - 최소 `INTERNAL_GENERATION_ERROR` 또는 추정 가능한 `NO_ASSIGNMENT_*`를 항상 채움.

### 구체 조치안(유저 피드백) 강화
- `NO_ASSIGNMENT_CAPACITY` + validator evidence가 있을 때,
  추상 문구 대신 `day/shift` 단위 조정 타깃을 `fix_plan.actions[].targets`로 생성하도록 추가.
  - `target_type=daily_requirement`, `day`, `shift`, `suggested_delta=-1`

---

## 2) 최근 실증 결과

## A. harness/ontology 데이터 경로 확인
- `/ontology`는 DB generate 결과를 직접 읽지 않고,
  `tools/harness/reports/run-*/graph_export.json` 산출물을 읽음.
- 그래서 run이 비어 있으면 `/ontology/runs`가 0으로 보일 수 있음.
- harness 1회 실행 후 `/ontology/runs`가 즉시 채워지는 것 확인.

## B. direct reason 다중 케이스
- 리포트: `tools/harness/reports/harness_cases_direct_reason_9B_ICU_v3.json`
- 결과:
  - 9B-2026-06: 500, `NO_ASSIGNMENT + CAPACITY + FIXED`, `reason_source=direct`
  - 9B-2026-07: 500, `NO_ASSIGNMENT + CAPACITY + FIXED`, `reason_source=direct`
  - ICU-2026-06: 200
  - ICU-2026-07: 200

## C. 실패 → 피드백 확인 → 소폭 조정 → 성공
- 리포트: `tools/harness/reports/e2e_feedback_apply_rerun_9B_2026_07.json`
- run node:
  - `run:2026-07-10135890c287-COMBINED-20260515-170200`
- 피드백:
  - `reason_source=direct`
  - breakdown=`capacity_shortage`, `fixed_lock`
- 소폭 적용:
  - Team2 D min `1 -> 0`
- 재실행:
  - `200`, `schedule_id=c7a3f83c55f6`
- 원복 완료.

---

## 3) 최근 코드 변경 파일

- `app/services/roster_create_service.py`
  - validator evidence 수집 추가
  - direct reason evidence 연계 강화
- `app/services/precheck/payload.py`
  - `validator_evidence_summary` 노출
- `app/services/precheck/fix_plan.py`
  - direct precedence 유지
  - `NO_ASSIGNMENT_CAPACITY` 시 concrete daily target 생성
- `app/routers/roster_create.py`
  - 비구조화 예외 → 표준 infeasibility payload fallback
- `app/routers/ontology.py`
  - run 선택 시 conflict summary 자동 표시
  - 첫 진입 자동 run 선택
- `app/services/cp_sat_basic.py`
- `app/services/cp_sat/fallback_lex.py`
  - carryover wrap 구간 `OnlyEnforceIf` 런타임 오류 경로 보정

테스트 파일:
- `tests/test_router_error_payload_fallback.py` (신규)
- `tests/test_no_assignment_direct_reason_emit.py`
- `tests/test_no_assignment_case_matrix.py`
- `tests/test_structural_diagnosis_payload.py`

---

## 4) 테스트 상태

최근 실행 기준:
- direct reason/route fallback/fix_plan 관련 회귀: pass
- 예시:
  - `python -m pytest tests/test_router_error_payload_fallback.py tests/test_no_assignment_direct_reason_emit.py tests/test_no_assignment_case_matrix.py tests/test_structural_diagnosis_payload.py -q`
  - `27 passed`
- capacity concrete target 추가 후:
  - `python -m pytest tests/test_structural_diagnosis_payload.py tests/test_no_assignment_case_matrix.py -q`
  - `22 passed`

---

## 5) 제약/운영 가드레일 (중요)

- 케이스 테스트 시 `max_conseq_work`는 변경하지 않음.
  - 문서 반영 위치: `tools/harness/README.md`
- /ontology 확인 전제:
  - harness runner로 run artifact 생성 필요.

---

## 6) 다음 세션 즉시 작업 TODO

### TODO-1 (최우선): 제약별(룰 단위) actionable feedback 확장
현재는 `NO_ASSIGNMENT` 축 중심으로 구체화됨.
다음은 rule 단위로 조치 템플릿을 붙여야 함.

목표:
- `A_*/B_*/D_*/F_*` 등 rule family별 `action template` 구성
- `fix_plan.actions[].targets`를 pool/day/shift 뿐 아니라 rule-context로도 채움

산출물:
- rule->action mapping 문서 + 테스트 케이스

### TODO-2: /ontology 원인 중심 뷰의 혼잡도 추가 완화
현재 자동 표시는 되지만, 노드 많을 때 체감이 여전히 큼.

목표:
- 대표 원인 Top N 기본 노출 + 나머지 숨김
- 동일 signature cause의 run 빈도/최근 발생 시간 표시

### TODO-3: ICU 실패 케이스 다양화 (non-max_conseq_work)
ICU는 성공 케이스가 많아 reason 분해 검증 표본이 부족함.

목표:
- allowed/fixed/day-eve-night 수요 축에서 소폭 조정으로 실패 유형 생성
- `ELIGIBILITY`/`CARRYOVER` direct reason 실증 샘플 확보

---

## 7) 다음 세션 시작 프롬프트 (복붙용)

"`docs/NEXT_SESSION_HANDOFF_2026-05-15.md` 기준으로 이어서 작업해줘.
1) rule 단위 actionable feedback 확장(TODO-1)부터 구현,
2) /ontology 원인 중심 뷰 혼잡도 추가 완화(TODO-2),
3) ICU non-max_conseq_work 실패 케이스를 생성해 ELIGIBILITY/CARRYOVER direct reason 실증(TODO-3).
각 단계마다 endpoint/harness 테스트 결과와 리포트 파일 경로를 함께 남겨줘."
