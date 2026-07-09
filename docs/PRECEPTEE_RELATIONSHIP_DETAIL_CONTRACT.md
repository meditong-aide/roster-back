# 프리셉티/프리셉터 관계 — 상세 응답 일원화 계약

작성: 2026-07-09 · 대상 브랜치: `dev`(백엔드, 미커밋) · SSOT: `nurse_preceptee_period`

관련 문서: [NURSE_PRECEPTEE_PERIOD_DESIGN.md](./NURSE_PRECEPTEE_PERIOD_DESIGN.md)

---

## 0. 배경 / 판단

프리셉티·프리셉터 **관계 판단 기준이 화면별로 갈라지지 않도록**, 선택 `nurse_id` 상세 응답에서도
`/nurses/preceptee-periods`와 **같은 필드·같은 필터**의 기간 정보를 내려준다.

- `current_assignment` = 파견/휴직/퇴사/병동이동 같은 **근무상태 표현 전용**.
- 프리셉티/프리셉터 **관계** = `nurse_preceptee_period`(SSOT) 기준으로 **분리**.
- 관계 데이터는 **프론트가 계산하지 않고 서버(DB/period)가 내려준다.**

> ⚠️ 관계를 전체 `/nurses` 리스트 응답에 per-nurse로 뿌리지 **않는다**. 리스트/row/만들기 화면은
> 기존 `GET /nurses/preceptee-periods`(그룹 단위 목록)를, **사이드 프로필(선택 1인)**은
> `GET /nurses/{id}` 상세 응답을 소스로 쓴다. 두 소스는 동일 계산(`list_preceptee_periods_for_month`)을 공유한다.

---

## 1. 공용 데이터 모델 — `PrecepteePeriodItem`

`/nurses/preceptee-periods`의 item과 **완전히 동일한 필드**. 방향 무관 공용.

| 필드 | 타입 | 의미 |
|---|---|---|
| `nurse_id` | string | 프리셉티(관계의 대상) |
| `preceptor_id` | string | 프리셉터 |
| `start_date` | date (ISO) | 구간 시작(`valid_from`) |
| `expected_end_date` | date (ISO) | 종료예정일 **inclusive** (`valid_to - 1day`) |

- 기간 반열림 `[valid_from, valid_to)`, `valid_to`는 배타. 응답의 `expected_end_date`는 포함형으로 변환됨.
- 겹침 판정: 그 달(`year`,`month`)의 `[1일, 말일]`과 `[start, expected_end]`이 겹치면 포함.

---

## 2. 읽기 계약

### 2-1. 그룹 목록 (기존, 무변경) — 리스트/row/만들기 화면용

```
GET /nurses/preceptee-periods?group_id={gid}&year={y}&month={m}
→ 200 { "items": [ PrecepteePeriodItem, ... ] }
```

그룹 active 간호사 중 그 달과 겹치는 모든 구간. 프론트는 `preceptor_id`로 그룹핑하면 프리셉터 방향도 파생 가능.

### 2-2. 선택 1인 상세 (신규) — 사이드 프로필용

```
GET /nurses/{nurse_id}?group_id={gid}&year={y}&month={m}
→ 200 NurseProfile {
    ...,
    "preceptee_period":  PrecepteePeriodItem | null,   // 내가 프리셉티(0..1). nurse_id == 나
    "preceptor_periods": PrecepteePeriodItem[]          // 내가 프리셉터(0..N). preceptor_id == 나
  }
```

- **필터·필드가 2-1과 동일** (`group_id`·`year`·`month`·겹침 조건, `list_preceptee_periods_for_month` 재사용).
- `year`/`month` **미동반 시** 두 필드는 채워지지 않는다(`preceptee_period=null`, `preceptor_periods=[]` 또는 리스트 경로의 as-of 값). → 사이드 프로필은 **반드시 그 달 year/month를 함께 전달**해야 관계가 채워진다.
- `group_id` 미지정 시 간호사 home 그룹으로 폴백.

**정합성 보장**: 같은 (group, year, month)에서
`GET /nurses/{457433}`의 `preceptee_period` == `GET /nurses/preceptee-periods`의 `items[nurse_id==457433]`.
(dev 검증 완료: 값 동일 True)

#### 검증 예 (dev, 중환자실 `10135857f9f9`, 2026-07)

| 대상 | `preceptee_period` | `preceptor_periods` |
|---|---|---|
| 프리셉티 457433 | `{nurse_id:457433, preceptor_id:301044, 06-01~07-14}` | `[]` |
| 프리셉터 301044 | `null` | `[{nurse_id:457433, preceptor_id:301044, ...}]` |
| 무관 간호사 | `null` | `[]` |

---

## 3. 쓰기 계약

`PATCH /nurses/{nurse_id}` body. 모두 `nurse_preceptee_period`에 **직접 write**(assignment 미경유).

### 3-1. 내가 프리셉티일 때 — `preceptee_period`

```jsonc
{ "preceptee_period": {
    "operation": "create" | "update" | "cancel",
    "preceptor_id": "301044",       // create/update 필수
    "start_date": "2026-06-01",     // create/update 필수
    "expected_end_date": "2026-07-14" // create/update 필수(무기한 폐지 — 없으면 400)
} }
```

### 3-2. 내가 프리셉터일 때 (N명 일괄) — `preceptor_periods` ⭐ 명칭 변경

```jsonc
{ "preceptor_periods": [           // 구 preceptees_assignment
    { "operation": "create", "target_nurse_id": "457433",
      "start_date": "2026-06-01", "expected_end_date": "2026-07-14" },
    { "operation": "cancel", "target_nurse_id": "457436" }
] }
```

- **대상 식별 = `target_nurse_id`** (프리셉티 1:1 → `assignment_id` 폐기).
- 읽기 `preceptor_periods`와 **대칭 명칭**(읽는 컬렉션을 그대로 PATCH).
- 구명 `preceptees_assignment`는 **전환기 동안 fallback 수용** → 프론트 무중단 전환 가능. 신규 코드는 `preceptor_periods` 사용.

---

## 4. Deprecated (관계 판단에서 제외)

| 대상 | 상태 | 대체 |
|---|---|---|
| `NurseProfile.preceptor_id` (루트) | deprecated | `preceptee_period.preceptor_id` |
| `NurseProfile.preceptees` (루트) | deprecated | `preceptor_periods` |
| `current_assignment`의 `reason="프리셉티"` | **제거됨** | 관계는 위 period 필드로만 |
| PATCH `preceptees_assignment` | deprecated(fallback 수용) | `preceptor_periods` |

- `current_assignment`는 이제 **휴직/퇴사/파견/병동이동** 근무상태만 담는다
  (`_STATUS_DISPLAY_REASONS`·`_ASSIGNMENT_PRIORITY`에서 프리셉티 제거).
- 루트 `preceptor_id`는 `nurses.preceptor_id`의 **as-of-오늘 단방향 캐시**로 유지(관계 SSOT 아님).

---

## 5. 백엔드 변경 파일 (미커밋)

| 파일 | 변경 |
|---|---|
| `app/services/preceptee_period.py` | `resolve_relationship_for_detail(db, group_id, nurse_id, year, month)` 추가 — `list_preceptee_periods_for_month` 재사용, 방향 분해 |
| `app/routers/nurses.py` | `get_nurse_by_id`에 `year`/`month` 파라미터 + 상세 응답에 `preceptee_period`/`preceptor_periods` 부착 (리스트엔 미주입) |
| `app/schemas/roster_schema.py` | `PrecepteePeriodItem` 신설, `NurseProfile.preceptee_period`(타입 교체)·`preceptor_periods` 추가, 루트 `preceptor_id`·`preceptees` deprecated, `NurseProfileUpdate.preceptor_periods`(구 `preceptees_assignment`) |
| `app/services/nurse_service.py` | `current_assignment`에서 프리셉티 제거, write `preceptor_periods` pop(구명 fallback) |

**테스트**: 프리셉티/배정 관련 103 passed(무관 WIP 2건만 실패 — `test_coverage_gaps_preceptee.py`, 엔진 커버리지·스키마 무관). **prod DB(`eun_roster`) 미접촉, dev(`eun_roster_dev`)만.**

---

## 6. 프론트 전환 (남은 작업, 사용자 결정으로 보류)

완료 기준:
- [ ] 사이드 프로필 상세 fetch(`useNurseDetail`)에 **그 달 year/month 전달** → `/nurses/{id}?group_id&year&month`.
- [ ] 프리셉터 방향 표시를 `detail.preceptor_periods`로 읽기(루트 `preceptees`·별도 `usePrecepteePeriodsQuery` 매핑 제거).
- [ ] 프리셉티 방향은 기존 `detail.preceptee_period` 유지(필드 동일, `nurse_id` 추가됨).
- [ ] write 필드명 `preceptees_assignment` → `preceptor_periods`.
- [ ] 사이드 프로필의 detail fallback 로직 제거.

관건: **사이드 프로필에 현재 조회 월(year/month) 컨텍스트를 주입**해야 상세 응답의 관계 필드가 채워진다.
현재 `useNurseDetail(nurseId)`는 nurseId만 받으므로 시그니처 확장 필요.
