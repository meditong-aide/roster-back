-- ============================================================================
-- 死/inert 컬럼 DROP DDL — ★dev DB(eun_roster_dev) 실조사(2026-07-21) 기준 검증본
-- 대상: 이번 브랜치(feat/roster-parameter)에서 ORM 제거한 컬럼만. dev+prod 양쪽 실행.
-- ★사용자 직접 실행(Claude는 DDL 미실행).
--
-- ★★DROP 금지(DB−ORM diff엔 나오지만 내 제거분 아님 — 실조사로 판별):
--   - roster_config.max_conseq_off      → origin/dev 가 ORM 매핑(models.py 신규기능). merge 시 유입. 절대 DROP 금지.
--   - roster_config.surplus_overrides_json / surplus_policy_preset / surplus_policy_version
--     / surplus_smoothing / grade_strategy → 어느 ORM도 미매핑(orphan). raw SQL 사용가능성 → 별도조사 후 결정. 여기 제외.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────
-- STEP 1 (배포 전, dev 에만): preceptor_gauge 를 nullable 로 완화.
--   dev preceptor_gauge = NOT NULL + DB디폴트 없음 → ORM 제거 배포~DROP 사이 창에서
--   config 저장(INSERT) 시 컬럼 생략 → IntegrityError. 미리 nullable 로 풀어 창 안전.
--   (prod preceptor_gauge 는 DEFAULT((5)) 있어 배포창 안전.)
ALTER TABLE eun_roster_dev.dbo.roster_config ALTER COLUMN preceptor_gauge INT NULL;

-- STEP 2: 새 코드(ORM 컬럼 제거) 배포

-- ─────────────────────────────────────────────────────────────────────────
-- STEP 3: DEFAULT 제약 DROP → 컬럼 DROP. dev/prod 각각의 DB context 에서 실행
--   (USE eun_roster_dev; / USE eun_roster; 로 대상 지정 후 아래 블록 실행).
--   off_placement_mode(=((0))), team_balance_mode(=('balanced')) 는 DEFAULT 제약 有 → 동적 제거.
--   나머지는 nullable·무디폴트라 바로 DROP.
-- ─────────────────────────────────────────────────────────────────────────
DECLARE @sql NVARCHAR(MAX) = N'';

-- (a) roster_config — 내 제거분 10개 (ban_night_before_fixed_off 포함)
--   ★ban_night_before_fixed_off: dev/prod 둘 다 NOT NULL + DEFAULT((1)) → 배포창 안전(STEP1 불요).
--     ORM 매핑만 제거(models.py)했고, solver 는 roster_config.py 기본값(True)로 동일 동작 유지.
--     DEFAULT((1)) 제약은 아래 동적 default-constraint-drop 블록이 자동 제거.
DECLARE @cols_rc TABLE (name SYSNAME);
INSERT INTO @cols_rc(name) VALUES
    (N'config_version'), (N'weekend_shift_ratio'), (N'patient_amount'),
    (N'even_nights'), (N'preceptor_gauge'), (N'off_placement_mode'),
    (N'team_balance_enable'), (N'team_balance_gauge'), (N'team_balance_mode'),
    (N'ban_night_before_fixed_off');

SELECT @sql = @sql + N'ALTER TABLE dbo.roster_config DROP CONSTRAINT ' + QUOTENAME(dc.name) + N';' + CHAR(10)
FROM sys.default_constraints dc
JOIN sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
JOIN @cols_rc x ON x.name = c.name
WHERE dc.parent_object_id = OBJECT_ID(N'dbo.roster_config');

SELECT @sql = @sql + N'ALTER TABLE dbo.roster_config DROP COLUMN ' + QUOTENAME(x.name) + N';' + CHAR(10)
FROM @cols_rc x
WHERE EXISTS (SELECT 1 FROM sys.columns c WHERE c.object_id = OBJECT_ID(N'dbo.roster_config') AND c.name = x.name);

-- (b) fixed_wanted_entries.head_nurse_memo (nullable·무디폴트)
SELECT @sql = @sql + N'ALTER TABLE dbo.fixed_wanted_entries DROP COLUMN head_nurse_memo;' + CHAR(10)
WHERE EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'dbo.fixed_wanted_entries') AND name = 'head_nurse_memo');

-- (c) schedules.parameter (ORM 미매핑 orphan·nullable·무디폴트)
SELECT @sql = @sql + N'ALTER TABLE dbo.schedules DROP COLUMN parameter;' + CHAR(10)
WHERE EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'dbo.schedules') AND name = 'parameter');

PRINT @sql;              -- 먼저 검토
EXEC sp_executesql @sql; -- 확인 후 실행

-- ─────────────────────────────────────────────────────────────────────────
-- STEP 4 (별건 orphan 정리 — 내 브랜치 무관·실조사로 확인된 死. 원치 않으면 이 블록 생략):
--   surplus_* : dev 전용(prod 없음)·코드참조 0(미완성 abandoned) → dev 에서만 DROP.
--   grade_strategy : dev+prod 존재하나 roster_config 컬럼 미참조(code 는 request/param COMBINED 사용)·orphan.
--   ★EXISTS 가드로 "있는 것만" DROP → prod 실행 시 surplus_* 는 자동 skip(없으니).
-- ─────────────────────────────────────────────────────────────────────────
DECLARE @sql2 NVARCHAR(MAX) = N'';
DECLARE @orphans TABLE (name SYSNAME);
INSERT INTO @orphans(name) VALUES
    (N'surplus_overrides_json'), (N'surplus_policy_preset'), (N'surplus_policy_version'),
    (N'surplus_smoothing'), (N'grade_strategy');
SELECT @sql2 = @sql2 + N'ALTER TABLE dbo.roster_config DROP CONSTRAINT ' + QUOTENAME(dc.name) + N';' + CHAR(10)
FROM sys.default_constraints dc
JOIN sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
JOIN @orphans x ON x.name = c.name
WHERE dc.parent_object_id = OBJECT_ID(N'dbo.roster_config');
SELECT @sql2 = @sql2 + N'ALTER TABLE dbo.roster_config DROP COLUMN ' + QUOTENAME(x.name) + N';' + CHAR(10)
FROM @orphans x
WHERE EXISTS (SELECT 1 FROM sys.columns c WHERE c.object_id = OBJECT_ID(N'dbo.roster_config') AND c.name = x.name);
PRINT @sql2;
EXEC sp_executesql @sql2;

-- ★보존(절대 DROP 금지): version(INT 프리셋)·config_name/config_memo·ShiftManage.config_version(다른 테이블)·
--   max_conseq_off(dev+prod·origin/dev ORM 신규기능).
