-- =============================================================================
-- Migration 2026-06-24 — 누락 간호사 속성 시점 테이블 + fixed_shift 통합 (MSSQL, lean)
-- =============================================================================
-- dev 에 nurse_allowed_shift_period 만 적용돼 있어 나머지를 마저 정리한다.
--   (1) nurse_grade_period          신규 생성
--   (2) nurse_weekendoff_period     신규 생성
--   (3) nurse_allowed_shift_period  fixed_shift 컬럼 추가
--       └ fixed_shift 는 allowed_shifts 와 결합 속성이라 별도 테이블 대신 같은 satellite 로 통합
--         (NurseFixedShiftPeriod 폐기).
--
-- ⚠️ 요청대로 FK 제약·인덱스는 제외. (id PRIMARY KEY 는 IDENTITY/ORM 동작에 필수라 유지)
-- 멱등: IF NOT EXISTS / COL_LENGTH 가드. 적용 전 DB 백업 권장.
-- =============================================================================

SET XACT_ABORT ON;
BEGIN TRANSACTION;

-- 1) nurse_grade_period — grade 시점(병동귀속, group_id 보유)
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'nurse_grade_period')
    CREATE TABLE nurse_grade_period (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        nurse_id    VARCHAR(50)  NOT NULL,
        group_id    VARCHAR(50)  NOT NULL,
        valid_from  DATE         NOT NULL,
        valid_to    DATE         NULL,
        grade       INT          NULL,
        source      VARCHAR(20)  NOT NULL DEFAULT 'edited',
        note        TEXT         NULL,
        created_at  DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        updated_at  DATETIME     NOT NULL DEFAULT GETUTCDATE()
    );

-- 2) nurse_weekendoff_period — 주말휴무(간호사귀속)
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'nurse_weekendoff_period')
    CREATE TABLE nurse_weekendoff_period (
        id           INT IDENTITY(1,1) PRIMARY KEY,
        nurse_id     VARCHAR(50)  NOT NULL,
        valid_from   DATE         NOT NULL,
        valid_to     DATE         NULL,
        weekend_off  TINYINT      NULL,
        source       VARCHAR(20)  NOT NULL DEFAULT 'edited',
        note         TEXT         NULL,
        created_at   DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        updated_at   DATETIME     NOT NULL DEFAULT GETUTCDATE()
    );

-- 3) nurse_allowed_shift_period 에 fixed_shift 컬럼 추가 (별도 테이블 폐기)
IF COL_LENGTH('dbo.nurse_allowed_shift_period', 'fixed_shift') IS NULL
    ALTER TABLE dbo.nurse_allowed_shift_period ADD fixed_shift VARCHAR(20) NULL;

COMMIT TRANSACTION;

-- 검증: 테이블 2 + 컬럼 1 기대
SELECT
    (SELECT COUNT(*) FROM sys.tables
       WHERE name IN ('nurse_grade_period', 'nurse_weekendoff_period')) AS created_tables,
    COL_LENGTH('dbo.nurse_allowed_shift_period', 'fixed_shift')          AS fixed_shift_col;
