# Phase 1 (team) 구현 로그 — `nurse_team_period`

> 시점 모델 Phase 1: team 을 effective-dated 구간으로. 설계: `docs/TEMPORAL_NURSE_MODEL_DESIGN.md` (v3).
> ⚠ 공유 dev MySQL 직접 접속 불가(코드 환경) → **§1 DDL·§3 백필은 사용자가 실행**. 코드/테스트는 SQLite로 검증.

## 진행 상태

| 증분 | 내용 | 상태 |
|---|---|---|
| **B1** | 모델 `NurseTeamPeriod` + 리졸버(`team_period.py`) + 테스트 | ✅ 코드 완료 (SQLite 6 passed) |
| B-dev | dev 테이블 생성(DDL) + 백필 | ✅ MSSQL 적재 확인 (9B: t1=5,t2=4,t3=4) |
| **B2** | 생성기 team read 전환(engine_nurses 일괄 → `resolve_team_for_roster`, 비회귀: None이면 기존값 유지) | ✅ 코드 적용 (9B 라이브 검증 대기) |
| **B3** | 재분배 apply → `set_team_period`(close-before-open) | ⏳ |
| **B4** | 문제 A: 전출(병동이동) source 경계=start_date, 파견 분기 | ✅ 코드 적용·라이브 검증 (전출자 엔진 제외, NURSE_BLOCKED_DAYS 소멸) |
| 검증 | 9B 7월 재생성이 재분배 팀 반영 | ⏳ (테이블+백필 후) |

## 1. DDL (dev = **MSSQL** 실행)

```sql
CREATE TABLE nurse_team_period (
    id          INT IDENTITY(1,1) NOT NULL,
    nurse_id    VARCHAR(50) NOT NULL,
    group_id    VARCHAR(50) NOT NULL,
    valid_from  DATE NOT NULL,
    valid_to    DATE NULL,                 -- null = 열린(계속) 구간
    team_id     INT NULL,
    source      VARCHAR(20) NOT NULL CONSTRAINT df_ntp_source DEFAULT 'inherited',  -- inherited|edited|redistribute
    note        NVARCHAR(MAX) NULL,
    created_at  DATETIME NULL CONSTRAINT df_ntp_created DEFAULT GETDATE(),
    updated_at  DATETIME NULL,
    CONSTRAINT pk_nurse_team_period PRIMARY KEY (id),
    CONSTRAINT fk_ntp_nurse FOREIGN KEY (nurse_id) REFERENCES nurses (nurse_id),
    CONSTRAINT fk_ntp_group FOREIGN KEY (group_id) REFERENCES [groups] (group_id)
);
CREATE INDEX ix_ntp_nurse ON nurse_team_period (nurse_id, valid_from);
CREATE INDEX ix_ntp_group ON nurse_team_period (group_id, valid_from);
```
- 가산적(신규 테이블) — 기존 테이블/데이터 무영향.
- `team_id` 복합 FK `(group_id,team_id)→teams` 는 DB레벨 미적용(앱 리졸버가 무결성 보장).
- 타입: `note`=NVARCHAR(MAX)(TEXT deprecated), `id`=IDENTITY(1,1), `created_at/updated_at`=DATETIME(ORM 매핑과 일치), `[groups]` 대괄호.
- **로컬 dev 가 MySQL 인 경우(대안)**: `INT IDENTITY(1,1)`→`INTEGER AUTO_INCREMENT`, `NVARCHAR(MAX)`→`TEXT`, `GETDATE()`→`NOW()`, `[groups]`→`` `groups` ``, CONSTRAINT 인라인 생략 가능.

## 2. 백업 (실행 전 권장)

새 테이블은 비어 있어 백업 불필요(롤백=DROP). 단 이후 증분(B3에서 `nurse_assignment` write 경로 변경)에 대비해 스냅샷 권장:

```bash
mysqldump -h <host> -u <user> -p meditong_roster nurses nurse_assignment \
  > backup_phase1_team_$(date +%Y%m%d_%H%M).sql
```

## 3. 백필 (현재 `nurses.team_id` → open 구간)

```sql
-- MSSQL (id 는 IDENTITY 라 미지정)
INSERT INTO nurse_team_period
  (nurse_id, group_id, valid_from, valid_to, team_id, source, created_at, updated_at)
SELECT nurse_id, group_id, '2000-01-01', NULL, team_id, 'inherited', GETDATE(), GETDATE()
FROM nurses
WHERE active = 1 AND team_id IS NOT NULL;
-- (로컬 MySQL 이면 GETDATE() → NOW())
```
- **의미**: 현재 팀을 "기록 이래(2000-01-01) 계속(open)"으로 깐다. 이후 변경은 `set_team_period`가 **close-before-open**으로 닫아 미래 history 가 정확해진다.
- **과거**: 과거 실제 근무표는 `IssuedRosterSnapshot`(격자)에 보존되므로, 백필이 과거를 '현재 팀'으로 칠해도 발행본 표시에는 영향 없음. (resolve 폴백은 백필 후엔 거의 쓰이지 않음 — 구간이 모든 날을 덮음)
- **검증**:
  ```sql
  SELECT COUNT(*) FROM nurse_team_period;                          -- = 활성·team 보유 간호사 수
  SELECT COUNT(*) FROM nurses WHERE active=1 AND team_id IS NOT NULL;  -- 위와 일치해야
  ```

## 4. 롤백

```sql
DROP TABLE nurse_team_period;   -- 가산적이라 이것만으로 원복
```
코드 롤백 = B1 커밋 revert (모델/리졸버/conftest/테스트).

## 5. 코드 변경 (B1, 커밋 동봉)

- `app/db/models.py`: `NurseMonthProfile`(month-grain, 폐기) → **`NurseTeamPeriod`**(구간).
- `app/services/team_period.py` (신규): `resolve_team`(ward-aware 폴백), `resolve_team_for_roster`(월 단일값, gap만), `set_team_period`(close-before-open), `get_team_period_on`.
- `app/services/month_profile.py` 삭제, `tests/test_month_profile.py` 삭제.
- `tests/test_team_period.py` (신규, 6): 폴백 ward-aware / 구간 override / close-before-open 과거보존 / gap 미지정 / 중간합류 단일값 / 동일시작일 갱신.
- `tests/conftest.py`: 테이블 등록 교체.

## 6. B2/B3 정밀 추적 및 편집안 (적용 보류 — dev 테이블+백필 후 함께)

### 메인 team 피드 = 단일 지점
생성기의 실제 team 피드는 **베이스 `nurses.team_id`(캐시) + 인바운드 override**:
- `roster_create_service.py:4689` → `d['team_id'] = _a.target_team_id` (인바운드는 assignment.target_team_id 로 덮음).
- 이게 `engine_nurses` → `nurse_data['team_id']`(`cp_sat_basic.py:786`) → 팀 제약(`team_min_by_team`, teams 2641~2674)으로 흐름.
- `5474`(precheck)·`5811`(진단맵)은 부차적(같이 바꾸면 일관).

### B2 (read 전환) — engine_nurses 조립 후 일괄 [적용됨, 비회귀 변형]
`_inbound_nurses` 추가까지 끝난 뒤(`active_range_candidates` 직전, ≈4714), **모든 engine_nurses 를 한 번에** resolve:
```python
from services.team_period import resolve_team_for_roster
for _en in engine_nurses:
    _rt = resolve_team_for_roster(db, str(_en.nurse_id), current_user.group_id, req.year, req.month)
    if _rt is not None:
        _en.__dict__['team_id'] = _rt
```
- **비회귀 결정**: 원안은 `4689` 의 `d['team_id'] = _a.target_team_id` 를 제거하는 것이었으나, B3(인바운드 period 쓰기) 전에는 인바운드 team 이 사라진다. → **`4689` 유지 + period None 이면 기존값 보존**으로 변경. period 가 덮으면 그 값으로 확정.
- 홈 그룹: 백필로 period 가 모든 날 덮음 → 캐시와 동일값(현행 유지) + SSOT 가 period 로 이동.
- 인바운드(group≠home): 9B period 없음 → None → `4689` 의 `target_team_id` 유지. **B3 가 9B period 를 쓰면 그때 period 우선**.

### B3 (재분배 apply → period)
`ward_redistribute_service` apply 의 팀 변경: `create_permanent_change(target_team_id=…)` / `create_assignment(target_team_id=…)` 대신
```python
set_team_period(db, nurse_id=…, group_id=대상병동, valid_from=발효일, team_id=새팀, source="redistribute")
```
병동 변경 자체(존재)는 그대로 `nurse_assignment(병동이동)`. **team 만 period 로**.

### B4 (문제 A — 전출 source 경계) [적용됨]
**증상**: 9B 7월 생성 시 전출(병동이동, src=9B) 7명이 엔진에 남아 **"31일 전체 차단"**(NURSE_BLOCKED_DAYS)으로 잔류 → 팀/등급 풀 유효 인력 왜곡 → 커버리지 구조적 infeasible.
**원인**: 엔진은 active_range 클리핑을 **휴직/퇴사만** 수행(`roster_create_service.py` ~4740). 병동이동 source-side 는 day-block(파견 복귀용 로직)으로만 처리돼 영구 전출도 "차단된 채 잔류".
**수정**: 휴직/퇴사 클리핑 직후, **reason=="병동이동" & source==현재그룹 & target!=현재그룹** 인 전출 assignment 를 `_clip_active_range_for_leaves` 로 start_date 경계 클리핑 → 월초 이전 시작이면 active_range=None → 엔진에서 제외. **파견은 제외 안 함**(복귀, 기존 day-block 유지).
**검증**(라이브 9B 2026-07): NURSE_BLOCKED_DAYS 7건 소멸, 엔진 22→15명, team dist {None:4,t2:3,t1:6,t3:2} 정상화.

### B4 잔여 — 별개의 데이터 이슈 (코드 아님)
B4 적용 후에도 9B 7월은 `NO_ASSIGNMENT` 잔존. 단일 conflict core = **박지연(303196)**:
- N전담(`is_night_nurse=['N']`) + `nurse_monthly_limit(2026-7).n_exact=2` → "N=2" vs OFF상한 유도 "N≥15" 모순 → 모델 UNSAT.
- 6월은 monthly_limit 행이 없어 정상 생성(6월 OK/7월 NO 의 직접 원인).
- **조치 = 데이터**: 박지연 7월 n_exact 제거/조정 또는 N전담 해제 또는 (실제 2일만 근무면) 제한가용/휴직 표기. → 사용자 결정.

## 부수 수정 (이 세션 디버깅 중 발견 — 시점 모델과 별개)

### S1. monthly-limits 엔드포인트 관리그룹 권한 (403 → 관리그룹 허용)
**증상**: 9B HN(전도연, DB home=9A·`original_group_id`)이 9B 간호사 고급설정의 월간 한도를 **조회/수정 모두 403** → 프론트에 한 줄도 안 보임(박지연 7월 n_exact 도 안 보였던 원인).
**원인**: `GET/PUT /nurses/monthly-limits` 권한 체크가 **홈 그룹 한정**(`group_id != resolve_home_group_id`)이라 HN multi-group(관리 병동)을 거부.
**수정**(`nurse_monthly_limit_service.py`): 홈-한정 체크를 **`assert_caller_can_access_group`**(홈 + original + 관리그룹 허용)으로 교체 — GET·PUT 동일. schedule_id·사이드프로필과 같은 group-scope 패턴, 이 엔드포인트만 누락돼 있었음.
**검증**: 403 → 200, 5월·7월 행 정상 반환.

### S2. N전담 + 낮은 N 한도 = 저장 시점 soft 경고
**목적**: 박지연형(N전담인데 n_exact/n_max 낮음)을 **설정 시점에** 안내(생성 때 가서야 infeasible 발견하는 일 방지).
**구현**:
- `precheck/monthly_limit_validator.py`: `warn_night_dedicated_low_n()` — N전담 & N한도 ≤ 가용일×0.5 면 경고 dict 반환.
- `nurse_monthly_limit_service.py`(PUT): 하드 차단(`issues_all`)과 **분리**한 `soft_warnings`로 수집 → 저장은 허용, 응답 `warnings`에 병합. 프론트(`useNurseMonthlyLimits.ts`)가 onSuccess 에서 `warnings`를 `toast.info`로 이미 표시 → 프론트 수정 불필요.
- 하드 차단 status `500 → 422`(사용자 데이터 모순).
**설계 결정**: 이 모순은 *항상* infeasible이 아님(다른 간호사가 야간 채우면 가능, 커버리지 의존) → **하드 차단 아닌 경고**. 명백한 산술 모순(N전담에 D/E 양수 등)은 기존대로 하드 차단. 최종 하드 게이트는 생성 precheck.

### 검증 (dev 테이블+백필 후)
1. 9B 7월 `/roster_create/generate` → infeasibility 의 team None 해소 확인.
2. 재분배 apply → `nurse_team_period` 에 close-before-open 으로 기록되는지.
3. 회귀: 이동 없는 그룹 생성 결과 동일(폴백=현행).

> **적용 순서**: dev `CREATE TABLE`+백필(§1·§3) → B2+B3 코드 동시 적용 → 9B 검증 → B4.
