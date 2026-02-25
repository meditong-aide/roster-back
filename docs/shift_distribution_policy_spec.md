# Shift Distribution Policy (Tech Spec)

## 목적
- **oversupply(여유 인원)**가 발생할 때 `O(OFF)`로 도망가지 않고, **D/E/N으로 분산 배정**되도록 유도한다.
- 특히 **D로만 쏠리는 현상**을 줄이기 위해 **일별 기준의 균등 분배**를 목표로 한다.
- 선호는 2계층으로 정의한다.
  - **강한 선호(날짜 지정형)**: Wanted/원티드 기반(기존 로직). 특정 날짜/교대 선호이므로 강하게 반영.
  - **약한 선호(월 단위)**: 개인 입력값. 잔여 자유도에서 “가능하면 선호 교대로” 유도.
- UI 미구현 기간에는 **req로 파라미터를 주입**해 조절하고, 실행 초기에 항상 적용값을 print한다.

## 적용 위치(엔드포인트)
- `POST /roster_create/generate` (요청 모델: `RosterRequest`)

## req 파라미터(임시 UI 대체)
- **distribution_mode**: `"hybrid" | "balanced" | "preference" | "off"` (기본 `"hybrid"`)
  - `hybrid`: 일별 균등 + 월단위 선호를 함께 반영(기본)
  - `balanced`: 일별 균등만 강화(월선호는 끔)
  - `preference`: 월선호를 상대적으로 강화, 단 “D 쏠림 방지”를 위한 최소 균등은 유지
  - `off`: 분배 정책 항을 끔(디버깅/레거시)
- **oversupply_balance_gauge**: `0~10` (기본 `6`)
  - 일별 oversupply 균등화 강도(쏠림 방지)
- **monthly_preference_gauge**: `0~10` (기본 `3`)
  - 월단위 선호(약한 선호) 유도 강도
- **monthly_shift_preferences**: `dict`
  - 형태: `{ "<nurse_id>": {"shift": "D|E|N", "strength": 0~10}, ... }`

## 내부 매핑(게이지 → weight)
- 게이지는 수간호사/생성 요청자가 조절하는 “운영 파라미터”로 보고,
  런타임에서 weight로 변환하여 엔진에 전달한다.
- 기본 매핑(안전 캡 포함):
  - `oversupply_equalize_weight = round(220 * (g/10)^1.7)`
  - `monthly_preference_weight = round(140 * (g/10)^1.7)`

## 목적함수(개념)
- **일별 균등 분배(oversupply 균등)**:
  - 일자 d에서 `over_{d,D}, over_{d,E}, over_{d,N}`의 차이를 최소화하여 특정 교대 쏠림을 완화
- **월단위 선호(개인 입력) 유도**:
  - 간호사 n의 월선호 교대가 `S*`일 때, 해당 교대 배정에 보너스 부여
  - 보너스 강도: `monthly_preference_weight * (strength/10)`

## 실행 초반 로그(필수)
- `roster_create_service._run_cp_sat_basic`에서 아래 형태로 1줄 출력(유지보수 안전장치):
  - `[ShiftDistributionPolicy] mode=..., oversupply_gauge=..., monthly_pref_gauge=..., oversupply_equalize=(enable,weight), monthly_pref_weight=..., monthly_pref_cnt=..., ...`

## 예시 요청(JSON)

```json
{
  "year": 2026,
  "month": 1,
  "distribution_mode": "hybrid",
  "oversupply_balance_gauge": 7,
  "monthly_preference_gauge": 3,
  "monthly_shift_preferences": {
    "441172": {"shift": "D", "strength": 8},
    "441173": {"shift": "E", "strength": 5}
  }
}
```

