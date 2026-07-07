-- =============================================================================
-- Migration 2026-06-22 — 간호사 속성 시점(effective-dated) 테이블 4종
-- Target dialect: MSSQL (Azure SQL / SQL Server)
-- 설계        : docs/NURSE_ATTRIBUTE_PERIOD_DESIGN.md (P0)
-- =============================================================================
-- 신규 4 테이블 CREATE. 멱등성: IF NOT EXISTS 가드. 재실행 안전.
--   nurse_grade_period, nurse_allowed_shift_period,
--   nurse_weekendoff_period, nurse_fixedshift_period
--
-- 공통 컬럼(시점 규칙): valid_from~valid_to 반열림 [from, to), null=계속.
-- 진실=이 테이블, nurses 컬럼=단방향 투영.
--
-- 적용 절차:
--   1. DB 백업 필수.
--   2. 본 스크립트 실행(트랜잭션).
--   3. 검증 쿼리(끝) 결과 4 확인.
-- =============================================================================

SET XACT_ABORT ON;
BEGIN TRANSACTION;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1) nurse_grade_period — grade 시점(병동귀속)
-- ─────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'nurse_grade_period')
BEGIN
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
        updated_at  DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_ngp_nurse FOREIGN KEY (nurse_id) REFERENCES nurses(nurse_id),
        CONSTRAINT fk_ngp_group FOREIGN KEY (group_id) REFERENCES groups(group_id)
    );
    CREATE INDEX ix_ngp_nurse ON nurse_grade_period (nurse_id, valid_from);
    CREATE INDEX ix_ngp_group ON nurse_grade_period (group_id, valid_from);
    PRINT '[create] nurse_grade_period';
END
ELSE
    PRINT '[skip] nurse_grade_period already exists';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2) nurse_allowed_shift_period — 허용 근무형(전담 포함, 간호사귀속)
-- ─────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'nurse_allowed_shift_period')
BEGIN
    CREATE TABLE nurse_allowed_shift_period (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        nurse_id        VARCHAR(50)   NOT NULL,
        valid_from      DATE          NOT NULL,
        valid_to        DATE          NULL,
        allowed_shifts  NVARCHAR(MAX) NOT NULL,   -- JSON: ["D"] / ["D","E"] / ["N"]
        source          VARCHAR(20)   NOT NULL DEFAULT 'edited',
        note            TEXT          NULL,
        created_at      DATETIME      NOT NULL DEFAULT GETUTCDATE(),
        updated_at      DATETIME      NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_nasp_nurse FOREIGN KEY (nurse_id) REFERENCES nurses(nurse_id)
    );
    CREATE INDEX ix_nasp_nurse ON nurse_allowed_shift_period (nurse_id, valid_from);
    PRINT '[create] nurse_allowed_shift_period';
END
ELSE
    PRINT '[skip] nurse_allowed_shift_period already exists';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3) nurse_weekendoff_period — 주말휴무(간호사귀속)
-- ─────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'nurse_weekendoff_period')
BEGIN
    CREATE TABLE nurse_weekendoff_period (
        id           INT IDENTITY(1,1) PRIMARY KEY,
        nurse_id     VARCHAR(50)  NOT NULL,
        valid_from   DATE         NOT NULL,
        valid_to     DATE         NULL,
        weekend_off  TINYINT      NULL,
        source       VARCHAR(20)  NOT NULL DEFAULT 'edited',
        note         TEXT         NULL,
        created_at   DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        updated_at   DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_nwop_nurse FOREIGN KEY (nurse_id) REFERENCES nurses(nurse_id)
    );
    CREATE INDEX ix_nwop_nurse ON nurse_weekendoff_period (nurse_id, valid_from);
    PRINT '[create] nurse_weekendoff_period';
END
ELSE
    PRINT '[skip] nurse_weekendoff_period already exists';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4) nurse_fixedshift_period — 고정 근무형(간호사귀속)
-- ─────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'nurse_fixedshift_period')
BEGIN
    CREATE TABLE nurse_fixedshift_period (
        id           INT IDENTITY(1,1) PRIMARY KEY,
        nurse_id     VARCHAR(50)  NOT NULL,
        valid_from   DATE         NOT NULL,
        valid_to     DATE         NULL,
        fixed_shift  VARCHAR(20)  NULL,
        source       VARCHAR(20)  NOT NULL DEFAULT 'edited',
        note         TEXT         NULL,
        created_at   DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        updated_at   DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_nfsp_nurse FOREIGN KEY (nurse_id) REFERENCES nurses(nurse_id)
    );
    CREATE INDEX ix_nfsp_nurse ON nurse_fixedshift_period (nurse_id, valid_from);
    PRINT '[create] nurse_fixedshift_period';
END
ELSE
    PRINT '[skip] nurse_fixedshift_period already exists';

COMMIT TRANSACTION;

-- ─────────────────────────────────────────────────────────────────────────────
-- 검증 (4 기대)
-- ─────────────────────────────────────────────────────────────────────────────
SELECT COUNT(*) AS created_tables
FROM sys.tables
WHERE name IN ('nurse_grade_period', 'nurse_allowed_shift_period',
               'nurse_weekendoff_period', 'nurse_fixedshift_period');
