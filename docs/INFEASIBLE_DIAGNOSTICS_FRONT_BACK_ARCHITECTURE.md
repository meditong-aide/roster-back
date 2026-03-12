# Purpose / Why this plan is needed
- 프론트에서 생성 결과를 볼 때 `CP-SAT 실패 → fallback 성공`인지, `CP-SAT 자체 성공`인지, 실패 원인이 무엇인지 일관되게 설명할 수 있는 표준 구조가 필요합니다.
- 기존 메시지는 결과론적 문구(예: 저배정률) 중심이라 운영자가 실제 수정 포인트를 파악하기 어렵습니다.

# Goal
- 생성 결과 응답과 스냅샷에 동일한 `diagnostics` 구조를 저장한다.
- 실패/부분성공 상황에서 `reason_code`와 `recommended_actions`를 항상 제공한다.
- 프론트는 진단 구조를 그대로 렌더링하고 문구는 최소한으로 가공한다.

# Backend architecture (proposed)
- 공통 진단 객체를 생성 파이프라인에 추가한다.
- 진단은 3단계로 구성한다.
  - `precheck`: 총량 기반 불가능 판정
  - `primary_cp_sat`: 1차 CP-SAT 결과
  - `fallback`: 폴백 실행 여부/결과/개선량

## 1) Precheck stage (always-on, cheap)
- 실행 시점: `generate_roster_service`에서 엔진 호출 직전.
- 계산 항목:
  - `CAPACITY_TOTAL_SHORTAGE`: 월 총 요구 슬롯 > 월 공급 상한
  - `N_CAPACITY_SHORTAGE`: 월 N 요구 > 월 N 상한 용량
- 출력 항목:
  - `reason_code`
  - `evidence` (need/capacity, nurse_count, off_days 등)
  - `severity=hard`

## 2) Primary CP-SAT stage
- 실행 시점: `_run_cp_sat_basic` 내부.
- 기록 항목:
  - `status`: `OPTIMAL | FEASIBLE | INFEASIBLE | UNKNOWN`
  - `hard_violation_count`
  - `violation_summary` (type별 count, 상위 deficit)
  - `reason_codes` (precheck 코드 + solve 중 코드)

## 3) Fallback stage
- 실행 조건: primary가 `INFEASIBLE`이거나 hard violation 잔존.
- 기록 항목:
  - `status`: `NOT_RUN | RUN_SUCCESS | RUN_FAILED`
  - `trigger`: `PRIMARY_INFEASIBLE | PRIMARY_HARD_VIOLATIONS | PRIMARY_TIMEOUT`
  - `before`: `{ violation_count, top_types }`
  - `after`: `{ violation_count, top_types }`
  - `fixes`: 감소한 위반 타입 요약
  - `remaining_reason_codes`

# Response/DB contract
- 응답과 스냅샷(`violations_json` 또는 동등 필드)에 동일하게 저장.

```json
{
  "diagnostics": {
    "version": "v1",
    "precheck": {
      "reason_codes": ["CAPACITY_TOTAL_SHORTAGE"],
      "evidence": {
        "required_total": 600,
        "capacity_total": 483,
        "nurse_count": 23,
        "num_days": 30,
        "off_days": 9
      }
    },
    "primary_cp_sat": {
      "status": "INFEASIBLE",
      "hard_violation_count": 0,
      "violation_summary": {
        "shift_requirement": 90
      },
      "reason_codes": ["CAPACITY_TOTAL_SHORTAGE"]
    },
    "fallback": {
      "status": "RUN_SUCCESS",
      "trigger": "PRIMARY_INFEASIBLE",
      "before": {"violation_count": 2162},
      "after": {"violation_count": 98},
      "fixes": ["shift_requirement_reduced"],
      "remaining_reason_codes": ["CAPACITY_TOTAL_SHORTAGE"]
    },
    "final_result": "PARTIAL_SUCCESS",
    "recommended_actions": [
      {
        "code": "CAPACITY_TOTAL_SHORTAGE",
        "message": "일 요구 인원(D/E/N) 합을 낮추거나 가용 인력을 늘리세요."
      }
    ]
  }
}
```

# Frontend rendering plan
- 생성 결과 상세에 `진단` 패널 추가.
- 상단 status chip 3개:
  - `Primary CP-SAT`
  - `Fallback`
  - `Final result`
- 타임라인 뷰:
  - `Precheck -> Primary -> Fallback -> Final`
- 액션 카드:
  - `reason_code`별 `message`
  - 증거 숫자(evidence) 함께 노출

## Front behavior rules
- `final_result=SUCCESS`: 진단 패널 접기 기본
- `final_result=PARTIAL_SUCCESS`: 진단 패널 펼침 기본
- `final_result=FAILED`: reason/action 카드 최상단 고정

# reason_code catalog (initial)
- `CAPACITY_TOTAL_SHORTAGE`
- `N_CAPACITY_SHORTAGE`
- `NO_ASSIGNMENT`
- `DAY_ZERO_COVERAGE`
- `M_OVERSUPPLY`
- `PRIMARY_INFEASIBLE`
- `FALLBACK_FAILED`
- `PRIMARY_TIMEOUT`

# Implementation phases
- Phase 1 (빠른 적용)
  - 현재 validate 메시지 `reason_code` 유지
  - precheck evidence를 `diagnostics.precheck`로 구조화
- Phase 2
  - primary/fallback 상태 및 before/after 위반 요약 저장
  - `recommended_actions` 매핑 테이블 도입
- Phase 3
  - (선택) INFEASIBLE 전용 짧은 probe를 추가해 제약군 충돌 후보 코드 제공

# Non-goals
- 모든 하드 제약의 완전한 UNSAT core 계산은 이번 범위에서 제외
- 프론트에서 원인 추론 로직을 별도로 구현하지 않음 (백엔드 진단 객체 신뢰)

# Validation checklist
- [ ] `diagnostics`가 sync/async 응답 모두에서 동일 구조로 제공되는지
- [ ] `final_result` 상태와 프론트 배지 표기가 일치하는지
- [ ] `reason_code` 누락 없이 최소 1개 이상 제공되는지 (실패/부분성공 시)
- [ ] 기존 응답 필드와 호환성 깨지지 않는지
