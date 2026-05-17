# Explainable Validator Upgrade Plan

## 목적

현재 validator는 생성 결과를 안전하게 차단하는 역할은 수행하지만, 일부 케이스에서
`NO_ASSIGNMENT` 같은 결과 라벨만 남아 원인 해석력이 부족하다.

이 문서는 validator를 제거하지 않고, **설명형(explainable) validator**로 업그레이드해
"왜 실패했는지"와 "무엇부터 소폭 조정해야 하는지"를 직접 근거 기반으로 제공하는 설계를 정리한다.

---

## 배경 / 현재 한계

### 현재 동작 요약
- 위치: `app/services/roster_create_service.py` `_validate_generated_roster(...)`
- 역할:
  1. 실근무 0건(`NO_ASSIGNMENT`) 차단
  2. 총량/야간 용량 산술 불가능성 보조 진단
  3. grade hard 충돌 보조 probe
  4. 일단위 커버리지 붕괴 검사

### 현재 한계
- 일부 validator 실패 경로에서 아래 현상이 발생:
  - `violated_constraints`에 `NO_ASSIGNMENT`만 존재
  - `conflict_cores=[]`
  - `fix_plan.reason_source=inferred`, `no_assignment_breakdown=[]`
- 결과적으로 사용자에게는 “실패”는 보이지만 “직접 원인 축”이 약하게 전달됨.

---

## 업그레이드 목표

### 목표 1: direct reason 우선
validator 실패라도 가능한 경우 반드시 아래 세부 reason을 직접 emit:

- `NO_ASSIGNMENT_CAPACITY`
- `NO_ASSIGNMENT_ELIGIBILITY`
- `NO_ASSIGNMENT_FIXED`
- `NO_ASSIGNMENT_CARRYOVER`

### 목표 2: 근거 데이터 동반
각 direct reason은 사람이 납득 가능한 근거(evidence)를 함께 반환:

- 어떤 날짜/교대가 비었는지
- 유효 후보가 왜 0인지(allowed/fixed/blocked)
- 월경계(carryover) 충돌 위치
- 최소요구 대비 결손량

### 목표 3: 소폭 조정 가능한 안내
해결안은 과격한 일괄 완화가 아니라,
`1~2 step 변경 -> 재실행` 원칙으로 액션 가이드 제공.

---

## 제안 아키텍처

## 1) Validator Evidence Layer 추가

`_validate_generated_roster` 내부에 설명용 측정 레이어를 추가:

### A. Assignment Feasibility Snapshot
- 단위: `(day, shift)`
- 수집값:
  - required_count
  - assigned_count
  - eligible_candidate_count
  - blocked_by_fixed_count
  - blocked_by_allowed_mask_count
  - blocked_by_carryover_count

### B. Nurse Blocking Snapshot
- 단위: `(nurse, day)`
- 수집값:
  - blocked_reason family (`allowed`, `fixed`, `carryover`, `other`)
  - 해당 일자 배정 가능 시프트 수

### C. Team/Grade Coverage Snapshot
- 단위: `(team, shift)`, `(grade, shift)`
- 수집값:
  - min requirement
  - feasible cap
  - unmet amount

> 구현 주의: 성능 보호를 위해 전체 기간 풀스캔 대신, 실패 관련 day/shift 우선 샘플링 + 상한(예: 50개 셀) 적용.

---

## 2) Direct Reason Deriver 분리

현재 validator 문자열 기반 추론을 보강하여,
별도 함수에서 evidence 기반 direct reason을 계산한다.

예시 함수(신규):

```python
def derive_no_assignment_direct_reasons(
    *,
    assignment_snapshot: dict,
    nurse_blocking_snapshot: dict,
    team_grade_snapshot: dict,
    existing_reason_codes: list[str],
) -> list[dict]:
    ...
```

반환 형식:

```json
[
  {
    "reason_code": "NO_ASSIGNMENT_ELIGIBILITY",
    "evidence": {
      "top_failed_cells": [...],
      "blocked_by_allowed_mask_count": 12
    },
    "human_message_ko": "유효 후보가 0인 교대가 다수 발생했습니다."
  }
]
```

---

## 3) Payload Contract 확장

`infeasibility`에 아래 필드 추가:

- `validator_evidence_summary`
  - direct reason별 핵심 근거 요약
- `validator_evidence_cells` (샘플)
  - day/shift 근거 샘플(상한 적용)
- `reason_source`
  - `direct | inferred`

`fix_plan`과의 연결:
- direct reason 존재 시 `fix_plan.reason_source=direct`
- breakdown은 direct reason 우선, inferred는 fallback.

---

## 4) Ontology UI 반영

`/ontology/conflict_summary` 렌더에서 아래 추가:

1. `NO_ASSIGNMENT` 분해 배지
   - CAPACITY / ELIGIBILITY / FIXED / CARRYOVER
2. 근거 셀 테이블(샘플)
   - day, shift, required, eligible, blocked reason
3. action-link 강조
   - `fix_plan_links`로 액션 ↔ 풀 노드 연결 표시

---

## 구현 단계 (다음 세션 실행용)

### Phase 1 — Evidence 수집 (백엔드)
1. `_validate_generated_roster`에 snapshot 수집 유틸 추가
2. 성능 상한(샘플 개수) 적용
3. 기존 오류 문자열 반환은 유지

### Phase 2 — Direct reason derivation
1. `derive_no_assignment_direct_reasons` 추가
2. `_extract_unrecoverable_violated_constraints`에서 호출
3. `NO_ASSIGNMENT_*` direct reason + evidence 병합

### Phase 3 — Payload & API
1. `precheck/payload.py`에 evidence summary 필드 연결
2. `/ontology/conflict_summary` 응답에 evidence 노출

### Phase 4 — UI
1. conflict summary 카드에 evidence 섹션 추가
2. breakdown 배지/링크 강조 적용

### Phase 5 — 테스트
1. 단위 테스트:
   - reason derivation 규칙 테스트
   - evidence 샘플 형식 테스트
2. 회귀 테스트:
   - 기존 `NO_ASSIGNMENT` matrix + direct precedence 유지
3. E2E 테스트:
   - validator 실패 케이스에서 direct reason + evidence 노출 확인

---

## 테스트 케이스 체크리스트

## 필수
- [ ] `NO_ASSIGNMENT` + eligible=0 셀 다수 -> `NO_ASSIGNMENT_ELIGIBILITY`
- [ ] fixed slot 충돌 우세 -> `NO_ASSIGNMENT_FIXED`
- [ ] carryover 경계 충돌 우세 -> `NO_ASSIGNMENT_CARRYOVER`
- [ ] 총량/커버리지 결손 우세 -> `NO_ASSIGNMENT_CAPACITY`

## 복합
- [ ] eligibility + fixed 동시
- [ ] capacity + carryover 동시
- [ ] 4축 동시 발생 시 우선순위/병기 규칙 확인

## 안전장치
- [ ] evidence 부족 시 `reason_source=inferred` fallback 동작
- [ ] 성능 상한 초과 시 샘플 truncation 메타 표시

---

## 비기능 요구사항

- 성능: validator 추가 측정으로 생성 시간 급증 금지(샘플링/상한)
- 가독성: 사용자 메시지에서 전문용어 최소화
- 안전성: 대규모 규칙 해제 유도 금지(소폭 조정 원칙 유지)

---

## 완료 기준(Definition of Done)

1. validator 실패 케이스에서 `NO_ASSIGNMENT_*` direct reason이 안정적으로 노출됨
2. `fix_plan.reason_source=direct`가 실제 데이터 케이스에서 확인됨
3. `/ontology/conflict_summary`에서 근거 셀 + 조치 링크가 시각적으로 확인됨
4. 관련 테스트(단위/회귀/E2E) 통과

---

## 다음 세션 시작 프롬프트(복붙용)

"`docs/EXPLAINABLE_VALIDATOR_UPGRADE_PLAN.md` 기준으로 Phase 1~3부터 구현해줘. 
목표는 validator 실패에서도 `NO_ASSIGNMENT_*` direct reason + evidence를 payload와 `/ontology/conflict_summary`에 노출하는 것. 
기존 테스트를 유지하면서 신규 테스트를 추가하고, 마지막에 E2E로 validator 실패 케이스 1건 실증까지 해줘."
