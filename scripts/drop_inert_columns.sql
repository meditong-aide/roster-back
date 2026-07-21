-- ============================================================================
-- roster_config inert 컬럼 제거 DDL (MSSQL) — dev(eun_roster_dev) + prod(eun_roster) 양쪽 실행
-- ★ 사용자가 직접 실행. Claude는 DDL 미실행(MCP read-only).
-- ★ 순서: (1) dev preceptor_gauge nullable 선처리 → (2) 코드 배포 → (3) 아래 DROP 실행
--    이유: dev preceptor_gauge 는 NOT NULL + DB디폴트 없음 → ORM 제거 후 config 저장(INSERT)이
--    컬럼 생략 시 IntegrityError. nullable 로 먼저 풀면 배포~DROP 사이 창에서도 안전.
--    (prod preceptor_gauge 는 DEFAULT((5)) 있어 안전. 나머지 8개는 nullable 또는 DB디폴트 有 → 안전.)
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────
-- STEP 1 (배포 전, dev 에만): preceptor_gauge 를 nullable 로 완화 (구코드 무영향)
--   ALTER TABLE eun_roster_dev.dbo.roster_config ALTER COLUMN preceptor_gauge INT NULL;
-- ─────────────────────────────────────────────────────────────────────────

-- STEP 2: 새 코드(ORM 컬럼 제거) 배포

-- ─────────────────────────────────────────────────────────────────────────
-- STEP 3 (배포 후): 기본값 제약 DROP → 컬럼 DROP.
--   MSSQL 은 DEFAULT 제약이 걸린 컬럼을 바로 DROP 못 함 → 제약 먼저 제거(이름 자동생성이라 동적).
--   아래 블록을 dev/prod 각각의 DB context 에서 실행 (USE 문 또는 3-part 로 대상 지정).
-- ─────────────────────────────────────────────────────────────────────────
DECLARE @tbl SYSNAME = N'roster_config';
DECLARE @cols TABLE (name SYSNAME);
INSERT INTO @cols(name) VALUES
    (N'config_version'), (N'weekend_shift_ratio'), (N'patient_amount'),
    (N'even_nights'), (N'preceptor_gauge'), (N'off_placement_mode'),
    (N'team_balance_enable'), (N'team_balance_gauge'), (N'team_balance_mode');

DECLARE @sql NVARCHAR(MAX) = N'';

-- (a) 각 컬럼에 걸린 DEFAULT 제약 DROP
SELECT @sql = @sql + N'ALTER TABLE dbo.' + QUOTENAME(@tbl)
              + N' DROP CONSTRAINT ' + QUOTENAME(dc.name) + N';' + CHAR(10)
FROM sys.default_constraints dc
JOIN sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
JOIN @cols x ON x.name = c.name
WHERE dc.parent_object_id = OBJECT_ID(N'dbo.' + @tbl);

-- (b) 컬럼 DROP (존재하는 것만)
SELECT @sql = @sql + N'ALTER TABLE dbo.' + QUOTENAME(@tbl)
              + N' DROP COLUMN ' + QUOTENAME(x.name) + N';' + CHAR(10)
FROM @cols x
WHERE EXISTS (SELECT 1 FROM sys.columns c
              WHERE c.object_id = OBJECT_ID(N'dbo.' + @tbl) AND c.name = x.name);

PRINT @sql;              -- 먼저 검토
EXEC sp_executesql @sql; -- 확인 후 실행

-- ★ version(INT 프리셋버전) / config_name / config_memo 는 보존 — 절대 DROP 금지.
-- ★ ShiftManage.config_version(다른 테이블) 은 무관 — 건드리지 않음.
