# Phase 1 (team) 구현 로그 — `nurse_team_period`

> 시점 모델 Phase 1: team 을 effective-dated 구간으로. 설계: `docs/TEMPORAL_NURSE_MODEL_DESIGN.md` (v3).
> ⚠ 공유 dev MySQL 직접 접속 불가(코드 환경) → **§1 DDL·§3 백필은 사용자가 실행**. 코드/테스트는 SQLite로 검증.

## 진행 상태

| 증분 | 내용 | 상태 |
|---|---|---|
| **B1** | 모델 `NurseTeamPeriod` + 리졸버(`team_period.py`) + 테스트 | ✅ 코드 완료 (SQLite 6 passed) |
| B-dev | dev 테이블 생성(DDL) + 백필 | ⏳ 사용자 실행 (§1·§3) |
| **B2** | 생성기 team read 전환(`roster_create_service.py:5474,5811` → `resolve_team_for_roster`) | ⏳ |
| **B3** | 재분배 apply → `set_team_period`(close-before-open) | ⏳ |
| **B4** | 문제 A: 전출(병동이동) source 경계=start_date, 파견 분기 | ⏳ |
| 검증 | 9B 7월 재생성이 재분배 팀 반영 | ⏳ (테이블+백필 후) |

## 1. DDL (dev MySQL 실행)

```sql
CREATE TABLE nurse_team_period (
    id          INTEGER NOT NULL AUTO_INCREMENT,
    nurse_id    VARCHAR(50) NOT NULL,
    group_id    VARCHAR(50) NOT NULL,
    valid_from  DATE NOT NULL,
    valid_to    DATE NULL,                 -- null = 열린(계속) 구간
    team_id     INTEGER NULL,
    source      VARCHAR(20) NOT NULL DEFAULT 'inherited',  -- inherited|edited|redistribute
    note        TEXT NULL,
    created_at  DATETIME,
    updated_at  DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY (nurse_id) REFERENCES nurses (nurse_id),
    FOREIGN KEY (group_id) REFERENCES `groups` (group_id)
);
CREATE INDEX ix_ntp_nurse ON nurse_team_period (nurse_id, valid_from);
CREATE INDEX ix_ntp_group ON nurse_team_period (group_id, valid_from);
```
- 가산적(신규 테이블) — 기존 테이블/데이터 무영향.
- `team_id` 복합 FK `(group_id,team_id)→teams` 는 DB레벨 미적용(앱 리졸버가 무결성 보장).
- MSSQL: `JSON` 없음(여긴 미사용), `TEXT→NVARCHAR(MAX)`, `AUTO_INCREMENT→IDENTITY(1,1)`, `` `groups` ``→`[groups]`.

## 2. 백업 (실행 전 권장)

새 테이블은 비어 있어 백업 불필요(롤백=DROP). 단 이후 증분(B3에서 `nurse_assignment` write 경로 변경)에 대비해 스냅샷 권장:

```bash
mysqldump -h <host> -u <user> -p meditong_roster nurses nurse_assignment \
  > backup_phase1_team_$(date +%Y%m%d_%H%M).sql
```

## 3. 백필 (현재 `nurses.team_id` → open 구간)

```sql
INSERT INTO nurse_team_period
  (nurse_id, group_id, valid_from, valid_to, team_id, source, created_at, updated_at)
SELECT nurse_id, group_id, '2000-01-01', NULL, team_id, 'inherited', NOW(), NOW()
FROM nurses
WHERE active = 1 AND team_id IS NOT NULL;
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

### B2 (read 전환) — engine_nurses 조립 후 일괄
`_inbound_nurses` 추가까지 끝난 뒤(≈4697 이후), **모든 engine_nurses 를 한 번에**:
```python
for n in engine_nurses:
    n.__dict__['team_id'] = resolve_team_for_roster(
        db, str(n.nurse_id), current_user.group_id, req.year, req.month
    )
```
그리고 **`4689` 의 `d['team_id'] = _a.target_team_id` 제거**(team 은 이제 period 가 진실). `target_grade` 등 나머지 override 는 grade Phase 까지 유지.
- period 없으면 `resolve_team_for_roster` 가 ward-aware 폴백 → 홈 그룹 간호사는 **현행과 동일**(`nurses.team_id`).
- 인바운드(group≠home)는 period 없으면 None → **B3로 period 가 세팅돼야 팀이 잡힘**(그래서 B2·B3 동시).

### B3 (재분배 apply → period)
`ward_redistribute_service` apply 의 팀 변경: `create_permanent_change(target_team_id=…)` / `create_assignment(target_team_id=…)` 대신
```python
set_team_period(db, nurse_id=…, group_id=대상병동, valid_from=발효일, team_id=새팀, source="redistribute")
```
병동 변경 자체(존재)는 그대로 `nurse_assignment(병동이동)`. **team 만 period 로**.

### B4 (문제 A — 전출 source 경계)
`get_active_assignments_for_month`(`assignment_service.py:888`)는 effective_end 기준 → 영구 이동의 옛 병동에 무기한 잔류. **전출(병동이동) source-side 는 start_date 경계**로 끊고, **파견은 source 유지**(복귀) — reason 분기.

### 검증 (dev 테이블+백필 후)
1. 9B 7월 `/roster_create/generate` → infeasibility 의 team None 해소 확인.
2. 재분배 apply → `nurse_team_period` 에 close-before-open 으로 기록되는지.
3. 회귀: 이동 없는 그룹 생성 결과 동일(폴백=현행).

> **적용 순서**: dev `CREATE TABLE`+백필(§1·§3) → B2+B3 코드 동시 적용 → 9B 검증 → B4.
