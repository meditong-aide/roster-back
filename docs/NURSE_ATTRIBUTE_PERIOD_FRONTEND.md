# 간호사 속성 시점(period) — 프론트엔드 핸드오프

브랜치: `feat/nurse-attribute-period`
작성: 2026-06-24

간호사 4개 속성을 **시점(effective-dated)** 으로 관리하도록 백엔드를 전환했다.
대상: **전담근무(allowed_shifts)**, **등급(grade)**, **주말휴무(is_weekend_off)**, **고정근무(fixed_shift)**.

---

## 0. 한 줄 요약 (프론트 관점)

- **"지금 값 변경"은 프론트 변경 없음** — 기존 간호사 수정 화면/엔드포인트 그대로. 내부적으로
  period 테이블에 기록되고 `nurses` 컬럼은 자동 동기화(단방향 투영)된다. 읽기는 지금처럼
  `nurses` 컬럼을 보면 된다.
- **새로 생긴 것: "특정 날짜부터 적용" (미래발효) 변경** → `POST /nurse-period/change`.
  교육 케이스("이 간호사 4월부터 D·E") 같은 시점 변경을 프론트에서 쓰려면 이 API를 호출.

---

## ⚠️ BREAKING — 필드명 변경 `is_night_nurse` → `allowed_shifts`

`nurses`의 전담 필드는 사실 **허용 근무형 리스트**(`[]`=제한없음, `["N"]`=N전담, `["D","E","N"]`=세 개)
였는데 이름이 boolean 처럼 보여 혼란을 줬다. DB 컬럼·API 요청/응답·badge를 **`allowed_shifts`로 통일**.
값 **형식·의미는 불변**(JSON 리스트 그대로), **이름만** 바뀜.

| 구 이름 | 신 이름 |
|---|---|
| `nurses.is_night_nurse` (요청/응답 필드) | `allowed_shifts` |
| `as_of_is_night_nurse` (월 as-of badge) | `as_of_allowed_shifts` |

→ 프론트는 **`is_night_nurse` 키를 `allowed_shifts`로 치환**하면 끝. (`is_night_dedicated` 배지는 이름 유지)

## 1. 변경 없음 — 현재값 수정

간호사 등급/전담/주말휴무/고정근무를 **지금 시점으로 바꾸는 것**은 기존 그대로다.
- 기존 간호사 수정 API에 필드 그대로 전송(`grade`, `allowed_shifts`, `is_weekend_off`, `fixed_shift`).
- 백엔드가 period(`valid_from=오늘`)에 기록 + `nurses` 컬럼에 투영.
- 프론트는 응답/조회에서 **`nurses` 컬럼을 그대로 읽으면 됨** (값 형식 불변).

→ 이 부분은 프론트 수정 불필요.

## 2. 신규 — 미래발효 변경 `POST /nurse-period/change`

"이 날짜부터 이 값" 형태의 시점 변경. (이전 구간은 그 날 직전까지로 자동 종료 = close-before-open)

**요청**
```jsonc
{
  "attribute": "allowed_shifts",   // allowed_shifts | weekend_off | fixed_shift | grade
  "nurse_id": "141086",
  "valid_from": "2026-04-01",       // 이 날부터 적용 (YYYY-MM-DD)
  "value": ["D", "E"],              // attribute 별 형식 ↓
  "group_id": "100991122603",       // grade 일 때 필수(병동귀속). 그 외는 생략 가능
  "note": "교육 종료, 이브닝 투입"    // 선택
}
```
**attribute 별 `value` 형식**
| attribute | value | 의미 |
|---|---|---|
| `allowed_shifts` | `["N"]` / `["D","E"]` / `[]` | 허용 근무형 집합. `["N"]`=N전담, `[]`=제한없음. 메인코드(D/E/N/M)만 |
| `weekend_off` | `1` / `0` | 주말휴무 / 해제 |
| `fixed_shift` | `"D"` / `""`(또는 null) | 고정 근무 코드 / 고정 해제 |
| `grade` | `3` (int) | 등급 |

**응답**
```jsonc
{ "attribute":"allowed_shifts", "nurse_id":"141086", "valid_from":"2026-04-01",
  "value":["D","E"], "today_value":["N"] }   // today_value = 오늘 기준 현재 적용값(투영 확인용)
```
**오류**: `422` — 허용 근무형이 월 한도/고정근무와 모순일 때(`allowed_shifts` 저장 시 교차검증).
```jsonc
{ "detail": { "message":"허용 근무형이 월 한도/고정근무 설정과 모순됩니다.", "issues":[ ... ] } }
```

## 3. 관리용 — 베이스라인 시드 `POST /nurse-period/backfill`

운영/초기 1회. 그룹 소속 간호사의 현재값을 period 첫 구간으로 심음(멱등).
```jsonc
// 요청
{ "group_id":"100991122603", "valid_from":null, "attributes":null }
// valid_from 생략=오늘, attributes 생략=4종 전체
// 응답
{ "group_id":"...", "valid_from":"2026-06-24", "nurse_count":16, "rows":{"allowed_shifts":16, ...} }
```
프론트는 보통 노출 불필요(관리 액션). 필요 시 관리자 버튼으로.

## 4. 주의 — fixed_shift ↔ allowed_shifts 결합

- `fixed_shift` 와 `allowed_shifts` 는 **한 테이블(satellite)** 로 통합됨. 고정 간호사는
  "그 코드로 고정(솔버 우회·평일=코드/주말=OFF)"이고 allowed 와 같이 변하는 결합 속성.
- 기존 커플링 유지: **`fixed_shift` 코드 설정 → `is_weekend_off` 자동 True**, 해제 → False.
  프론트가 fixed 설정 시 주말휴무 토글도 따라 바뀜을 예상할 것.

## 5. 근무자관리 월 셀렉터 — 속성이 그 달 값으로 보임 (읽기 계약 변경)

`GET /nurses` 와 `GET /members` 를 **`year`/`month` 와 함께** 호출하면, 응답의 **base 속성
필드가 그 달 기준 as-of 값으로 내려간다**(캐시=오늘값이 아니라 period as-of). 즉 월 셀렉터를
8월로 옮기면 8월 발효값이 그대로 그 필드들에 보인다.

월 as-of 로 덮어쓰는 필드(year/month 동반 시):
| 필드 | 의미 |
|---|---|
| `grade` | 그 달 등급 |
| `allowed_shifts` | 그 달 허용 근무형(전담) — `["N"]`=N전담 등 |
| `is_weekend_off` | 그 달 주말휴무 |
| `fixed_shift` | 그 달 고정근무 |
| `team_id` / `as_of_team` | 그 달 팀 |
| `is_night_dedicated` | 그 달 N전담 여부(배지) |

- **프론트는 필드명 그대로 읽으면 됨** — 월만 같이 보내면 값이 그 달로 바뀐다. 별도 `as_of_*`
  필드도 함께 옴(`as_of_grade`/`as_of_weekend_off`/`as_of_fixed_shift`/`as_of_allowed_shifts`).
- **`year`/`month` 미동반 시**: 기존대로 캐시(오늘값). (변경 없음)
- gap(그 달 구간 없음) → 캐시 폴백(무회귀).

**예외 — 주휴(weekly_off)는 월 as-of 아님**: `weekly_off_enabled` / `weekly_off_weekday` 는
period 가 없고 그룹 설정(주휴 적용 토글)에 구동되므로 **항상 캐시값**이다. is_weekend_off(주말휴무)
와 다른 속성임에 주의(이건 월 as-of 됨).

## 6. ⚠️ 남은 갭 — 속성 타임라인 조회(GET)

월 "한 시점"의 값은 §5 로 보이지만, **한 간호사의 변경 이력 전체(구간 목록)를 조회하는
GET 엔드포인트는 아직 없다**. "3월까지 D, 4월부터 D·E" 같은 **타임라인 UI** 를 그리려면
`GET /nurse-period?nurse_id=&attribute=` (구간 목록) 신설이 필요. 필요하면 알려주세요.

---

## 부록 — 설정 모달(별건)

근무표 생성 설정 프리셋 모달의 프론트 계약은 **`docs/ROSTER_CONFIG_PRESET_FLOW.md` §3** 참고
(저장/목록/로드/생성 materialize + `materialized_config` 반환 계약). 본 문서와 별개 작업.
