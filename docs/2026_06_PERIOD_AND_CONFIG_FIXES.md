# 2026-06 속성 Period SSOT 정합 + 근무표 Config 로딩 버그 수정

본 문서는 2026-06 작업 사이클에서 진행한 두 영역의 버그 수정·정합 작업을 기록한다.

- **A. 간호사 시점 속성(effective-dated period) 쓰기 불변식 + 캐시 우회 정합**
- **B. 근무표 생성 Config 로딩(생성 버튼 비활성 + 404)**

대상 브랜치: 백엔드 `feat/nurse-attribute-period`, 프론트 `feat/wanted-setting`.

관련 설계 문서: `docs/NURSE_ATTRIBUTE_PERIOD_DESIGN.md`, `docs/NURSE_GROUP_CHANGE_MODEL.md`.

---

## 배경: 속성 Period 모델 (SSOT)

4개 간호사 속성(`grade`, `allowed_shifts`, `fixed_shift`, `is_weekend_off`)을 시점 기반(effective-dated)으로 관리한다.

- **진실(SSOT)** = period 테이블 (`nurse_grade_period`, `nurse_allowed_shift_period`, `nurse_weekendoff_period`, `nurse_team_period`)
- **캐시** = `nurses` 컬럼. period → 컬럼으로 **단방향 투영**만 한다(앱이 컬럼 직접 쓰기 금지).
- 구간 = `[valid_from, valid_to)` 반열림, **겹침 금지**, 변경 = close-before-open(옛 구간 닫고 새 구간 open, 삭제 안 함), gap 허용.
- 읽기 = `fetch_periods()` + `resolve_asof()` (`app/services/nurse_period_resolver.py`).

핵심 헬퍼 `upsert_period(...)`: `valid_from`부터 값 변경(close-before-open) + 캐시 단방향 투영.

---

## A. 속성 Period 쓰기 정합

### A-1. 쓰기 불변식 3종 버그 (커밋 `0e69d5a`)

세 가지 별개 버그가 같은 "겹침/중복 금지" 불변식을 깨고 있었다.

#### (1) 사이드프로필 source 분기 `valid_from` 누락 — 전월 소실

- **증상**: 6월에 주말휴무 ON → 8월에 OFF 저장 시, 6·7월이 보존돼야 하는데 **6월 row가 제자리 갱신**되어 사라짐.
- **원인**: `update_nurse_profile_service`의 저장 분기는 호출자 역할로 3개로 갈린다 — admin / target(파견 inbound) / **source(수간호사 본인 병동)**. 이 중 **source 분기만** `_persist_profile_period_change`에 `valid_from`을 안 넘겨, 내부 폴백으로 **당월(today)** 이 됨 → 8월 저장이 6월 valid_from으로 들어가 in-place 갱신.
- **거짓 그린**: 기존 테스트가 전부 `master_admin`이라 admin 분기(정상)만 탔다. 실제 사용자(수간호사)가 타는 source 분기는 한 번도 검증 안 됨.
- **수정**:
  - `nurse_service.py` source 분기에 `valid_from=_eff_vf` 추가.
  - `_persist_profile_period_change`의 `valid_from`을 **필수 인자화**(default 제거 + 내부 당월 폴백 제거) → 누락 시 `TypeError`로 즉시 실패(조용한 오작동 차단). "당월 vs 선택월" 결정을 `_eff_vf` 단일 지점으로 일원화.
  - 테스트: `tests/test_sideprofile_effective_month.py` 를 admin/source/target **3역할 parametrize**로 재작성.

#### (2) 중간시점 분할 겹침 — 뒤 변경점 삼킴

- **증상**: 6월 ON → 8월 OFF → 7월 OFF 후, 8월을 다시 ON 해도 반영 안 됨(7월 값이 8월까지 계속 이김).
- **원인**: `upsert_period`가 구간 분할 시 새 구간을 **항상 `valid_to=None`(열린 구간)** 으로 삽입. `[6/1,8/1)` 중간(7/1)을 편집하면 새 `[7/1, None)` 이 기존 `[8/1, ...)` 을 덮어 **열린 구간 2개 겹침** 발생 → `resolve_asof`가 앞 구간을 먼저 반환.
- **수정** (`nurse_period_resolver.py`):
  - 분할 시 새 구간이 **기존 구간의 끝(`cur.valid_to`)을 승계** → 뒤 변경점 보존.
  - gap(앞쪽 미지정월) 편집 시 `_next_span_start()`로 **다음 변경점까지만** 구간을 연다.
  - 테스트: `tests/test_period_midmonth_split.py` (6T→8F→7F→8T 시나리오 + gap 편집).

#### (3) `set_team_period` flush 누락 — 완전중복 행

- **증상**: `nurse_team_period`에 (nurse, group, valid_from)이 동일한 **완전 복제 행**이 다수.
- **원인**: 프로덕션 `SessionLocal`은 `autoflush=False`. `apply_team_ops`가 한 txn에서 같은 nurse를 두 번 처리하면(payload 중복 item 등), 두 번째 `set_team_period`의 `same` 쿼리가 첫 INSERT(미flush)를 못 봐 **동일 행을 또 INSERT**. `upsert_period`엔 있던 `db.flush()`가 `set_team_period`엔 빠져 있었다.
- **수정**: `team_period.py::set_team_period` 시작에 `db.flush()` 추가. 테스트: `tests/test_team_period_no_dup.py` (prod 재현 위해 `db.no_autoflush` 사용 — 기본 테스트 세션은 `autoflush=True`라 끄지 않으면 재현 불가).

#### 손상 데이터 정리 (1회성)

dev DB에 이미 쌓인 손상 데이터 복구:

- `nurse_weekendoff_period`: 겹친 열린 구간을 다음 변경점까지 클립.
- `nurse_team_period`: 완전중복 행 30개 제거(그룹별 최소 id 1개만 유지).

**무결성 점검 쿼리**(주기적 확인 권장):
```sql
-- 겹침: 열린 구간이 2개 이상
SELECT nurse_id[, group_id] FROM <period_table>
WHERE valid_to IS NULL GROUP BY nurse_id[, group_id] HAVING COUNT(*) > 1;
-- 완전중복: 모든 컬럼 동일
SELECT ..., COUNT(*) FROM <period_table> GROUP BY <all> HAVING COUNT(*) > 1;
```
두 쿼리 모두 0건이어야 한다.

### A-2. 캐시 우회 → period as-of 일원화 (커밋 `89db816`, P2/P4/P5)

생성기는 `allowed_shifts`/`fixed_shift`를 period as-of로 읽는데, 세 곳이 캐시(오늘값)를 직접 읽거나(검증·추천) 직접 쓰며(엑셀) period(SSOT)를 우회 → 생성과 불일치. 모두 **무회귀**(구간 있으면 period, gap이면 캐시 폴백)로 수정.

| 위치 | 문제 | 수정 |
|---|---|---|
| **P2** `excel_service.py` | 엑셀 업로드가 `existing.allowed_shifts=...` 캐시 직접 대입 | `_excel_upsert_allowed()` — `upsert_period` 경유(valid_from=today, 캐시 단방향 투영). 신규·기존 모두 |
| **P4** `nurse_monthly_limit_service.py` | 월한도 검증이 구 resolver(`get_nurse_effective_attr` = `assignment.target_*`→캐시)로 읽어 생성과 불일치 | `_period_asof_overrides()` — home 간호사를 period as-of로. 파견 inbound은 생성기와 동일하게 `target_*` 유지 |
| **P5** `replacement_recommend_service.py` | 대체 추천의 야간가능 판정이 캐시 읽음(날짜 기준 아님) | 후보 로드 직후 `_overlay_candidate_capability_asof()` — 대상 스케줄 월 as-of 오버레이(생성기 `_overlay_home_profile_asof`와 동일 `__dict__` 방식) |

테스트: `tests/test_excel_allowed_period.py`, `tests/test_monthly_limit_period_asof.py`, `tests/test_replacement_capability_asof.py`.

**오진 항목(보고됐으나 버그 아님)**:

- **P1** `roster_service.py:106` — use_mid OFF 시 M strip은 **중복(redundant)**. `use_mid` off면 `base_keys = ["D","E","N"]`(생성기)에서 M이 demand 단계에서 제외되어 솔버에 M 수요 0. 캐시 strip은 생성 결과에 무영향. 또한 "upsert_period 경유" 제안은 **틀림**(use_mid는 per-config 토글이지 per-nurse 시간속성이 아님).
- **P3** 주휴 그룹 일괄 변경 — `weekly_off_enabled`(주휴 대상)는 `is_weekend_off`(period 속성)와 **다른 컬럼**. 생성시점 config 그룹동기화로 설계대로.
- **P6** 표시·조회용 캐시 직접 읽기 — 현재상태 표시엔 캐시(=오늘 투영값)가 정답.

---

## B. 근무표 생성 Config 로딩 버그

증상: 근무표 생성 모달은 숫자가 정상으로 떠 있는데 **"근무표 생성" 버튼이 비활성**, 그리고 이를 풀자 `GET /config/version/544` **404**.

### B-1. 생성 버튼 비활성 (프론트 커밋 `a96eb88e`)

- **메커니즘**: 모달 생성 버튼은 `disabled = ... || !canGenerate`, `canGenerate = Boolean(serverConfig)` (dev `021d145a "솔버 보호 플로우"`에서 신규 도입). 그런데 **모달에 보이는 숫자와 `serverConfig`는 출처가 다르다** — 숫자는 manpower props(`initialMonthly`/`initialDaily`)에서, `serverConfig`는 별도 `useRosterConfig()` fetch에서. 그래서 "숫자는 뜨는데 버튼만 죽음".
- **근본 원인**: `useRosterConfig`가 `useRosterConfigVersionsQuery`(=`/config/versions`, **저장 프리셋 `version!=NULL`만** 반환)에서 `latestVer`를 잡는다. **ad-hoc 설정(version NULL)만 쓰는 그룹**은 versions가 `[]` → `latestVer=undefined` → config 쿼리 `enabled=false` → `serverConfig=null` → 버튼 영구 비활성. (프리셋 한 번도 저장 안 한 대부분 그룹에 광범위 영향.)
- **수정**: 프리셋 있으면 그 최신본, 없으면 **`'latest'` sentinel** 로 폴백.
  ```ts
  const versionSelector = latestVer ?? "latest";
  // enabled 에서 `latestVer != null` 조건 제거(versions 로딩 완료만 대기)
  ```
  백엔드 `/config/version/{sentinel}`은 이미 "대상 그룹 최신 config(없으면 DEFAULT) 반환"(생성기 `_fetch_latest_config`와 동일 기준)을 지원하므로, 한 줄 폴백으로 해결. 이 수정은 **버튼 비활성 + 모달 설정 토글이 기본값으로 뜨던 문제(숨은 버그)** 를 동시에 해결한다(토글이 실제 config 값으로 로드 → 생성 시 기본값으로 덮어쓰는 사고 방지).

### B-2. `/config/version/{int}` 404 (백엔드 커밋 `1ac5ad6`)

- **메커니즘**: B-1 수정 후 `serverConfig.config_id=544`가 로드되는데, 프론트 다수 경로가 이 **config_id를 version 자리에 넘긴다**.
  - `Roster_create.tsx`: `setCurrentConfigVer(response.configs.config_id)` — 변수명은 `ConfigVer`인데 **값은 config_id**.
  - `useGenerateRosterMutation.ts`: `getConfigIdForVersion({ configVersion: currentConfigVer })` → `GET /config/version/544`.
  - 엔드포인트는 정수를 **version**으로 조회(`WHERE version=544`). 이 그룹은 config가 전부 `version=NULL`(ad-hoc) → 매칭 0건 → **404**.
- **수정**: `/config/version/{int}` 엔드포인트에서 **version 매칭 실패 시 같은 group 스코프의 `config_id`로 폴백** 조회. 안전(현재 404나는 자리에만 폴백 추가, group 필터로 타 그룹 오해석 없음). 프론트 여러 경로가 config_id/version을 혼용하므로 **백엔드 단일 폴백**이 가장 견고.

  ```python
  # WHERE version == _ver_int 실패 시
  pconf = (db.query(RosterConfigModel)
             .filter(office_id==..., group_id==..., config_id == _ver_int).first())
  ```

> **근본 cleanup(별도 권장)**: 프론트 `currentConfigVer`의 네이밍/conflation(config_id vs version)을 정리하면 백엔드 폴백 의존을 줄일 수 있다. 다만 conflation이 여러 경로에 퍼져 있어 이번엔 백엔드 폴백으로 동작 정상화.

---

## 커밋 인덱스

| 영역 | 커밋 | 요약 |
|---|---|---|
| 백 | `1781692` | bulk 저장 valid_from=선택월 |
| 백 | `306b15b` | 생성기 grade/weekend/fixed 대상월 period as-of |
| 백 | `d05d990` | 사이드프로필 PATCH 선택월 발효 + weekend period 일원화 |
| 백 | `0e69d5a` | 속성 period 쓰기 불변식 3종 버그(겹침·중복·분기누락) |
| 백 | `89db816` | allowed/fixed 캐시 우회 3곳 → period as-of (P2/P4/P5) |
| 백 | `1ac5ad6` | /config/version/{int} version 미스 시 config_id 폴백(404) |
| 프론트 | `a7046cb2` | 근무자관리 속성 저장 선택월(as-of) 발효 배선 |
| 프론트 | `a96eb88e` | useRosterConfig 프리셋 없으면 'latest' 폴백(생성버튼 비활성) |

## 교훈

1. **역할 분기 서비스는 실제 호출자 역할로 테스트한다.** admin-only 테스트는 거짓 그린(source 분기 `valid_from` 누락 버그를 못 잡음).
2. **불변식은 코드 구조로 강제한다.** `valid_from` 필수 인자화처럼, 조용한 오작동을 시끄러운 실패로 바꾼다.
3. **`autoflush=False` 세션에서 period 쓰기 함수는 시작에 `db.flush()`.** 같은 txn 반복 쓰기의 중복 INSERT 차단.
4. **표시 데이터와 게이트 데이터의 출처가 다르면** "보이는데 막힘" 류 버그가 난다(serverConfig vs manpower props).
5. **config_id ≠ version.** 두 식별자 공간을 혼용하지 말 것. 엔드포인트가 정수 path param을 어느 쪽으로 해석하는지 항상 명시.
