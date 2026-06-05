-- =============================================================================
-- Migration 2026-05-19 — Agent memory tables + AgentMemoryAudit.who NOT NULL
-- Target dialect: MSSQL (Azure SQL / SQL Server)
-- Author        : agent ralph sprint
-- =============================================================================
-- 신규 5 테이블 CREATE + agent_memory_audit.who NULL → NOT NULL.
-- 멱등성: IF NOT EXISTS 가드. 동일 스크립트 재실행 안전.
--
-- 적용 절차:
--   1. DB 백업 필수.
--   2. 본 스크립트 실행 (트랜잭션 안에서 진행).
--   3. 검증 쿼리 (스크립트 끝) 결과 0 확인.
--   4. 이상 시 ROLLBACK 가능.
-- =============================================================================

SET XACT_ABORT ON;
BEGIN TRANSACTION;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1) agent_conversation — Tier 1 세션 메타
-- ─────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'agent_conversation')
BEGIN
    CREATE TABLE agent_conversation (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        session_id      VARCHAR(64) NOT NULL UNIQUE,
        user_id         VARCHAR(64) NOT NULL,
        group_id        VARCHAR(64) NOT NULL,
        vm_json         NVARCHAR(MAX) NULL,
        created_at      DATETIME NOT NULL DEFAULT GETUTCDATE(),
        last_active_at  DATETIME NOT NULL DEFAULT GETUTCDATE(),
        ttl_until       DATETIME NULL
    );

    CREATE INDEX ix_agent_conversation_user_id  ON agent_conversation (user_id);
    CREATE INDEX ix_agent_conversation_group_id ON agent_conversation (group_id);
    PRINT '[create] agent_conversation';
END
ELSE
    PRINT '[skip] agent_conversation already exists';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2) agent_conversation_message — Tier 1 메시지 (영구 보존)
-- ─────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'agent_conversation_message')
BEGIN
    CREATE TABLE agent_conversation_message (
        id               INT IDENTITY(1,1) PRIMARY KEY,
        session_id       VARCHAR(64) NOT NULL,
        turn_idx         INT NOT NULL,
        role             VARCHAR(32) NOT NULL,        -- system | user | assistant | tool
        content          NVARCHAR(MAX) NULL,
        tool_calls_json  NVARCHAR(MAX) NULL,
        created_at       DATETIME NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_msg_session
            FOREIGN KEY (session_id) REFERENCES agent_conversation (session_id)
    );

    CREATE INDEX ix_agent_msg_session_id ON agent_conversation_message (session_id, turn_idx);
    PRINT '[create] agent_conversation_message';
END
ELSE
    PRINT '[skip] agent_conversation_message already exists';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3) agent_user_memory — Tier 2 fact (Zep식 temporal validity)
-- ─────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'agent_user_memory')
BEGIN
    CREATE TABLE agent_user_memory (
        id                   INT IDENTITY(1,1) PRIMARY KEY,
        user_id              VARCHAR(64) NOT NULL,
        group_id             VARCHAR(64) NOT NULL,
        fact_type            VARCHAR(64) NOT NULL,
        fact_text            NVARCHAR(MAX) NOT NULL,
        source               VARCHAR(32) NOT NULL,    -- USER_STATED | AGENT_INFERRED | SYSTEM_DERIVED
        valid_from           DATETIME NOT NULL DEFAULT GETUTCDATE(),
        valid_to             DATETIME NULL,           -- NULL = currently valid
        confidence           FLOAT NULL DEFAULT 1.0,
        evidence_session_id  VARCHAR(64) NULL,
        created_at           DATETIME NOT NULL DEFAULT GETUTCDATE()
    );

    CREATE INDEX ix_agent_user_memory_user_id   ON agent_user_memory (user_id);
    CREATE INDEX ix_agent_user_memory_group_id  ON agent_user_memory (group_id);
    CREATE INDEX ix_agent_user_memory_fact_type ON agent_user_memory (fact_type);
    PRINT '[create] agent_user_memory';
END
ELSE
    PRINT '[skip] agent_user_memory already exists';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4) agent_memory_audit — 메모리 변경 감사
--    who: 의료 audit 요건상 NOT NULL (Architect SUGGESTION).
-- ─────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'agent_memory_audit')
BEGIN
    CREATE TABLE agent_memory_audit (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        action        VARCHAR(16) NOT NULL,    -- READ | WRITE | UPDATE | DELETE | EXPIRE
        tier          VARCHAR(16) NOT NULL,    -- SESSION | USER | PROCEDURAL
        row_id        INT NULL,
        who           VARCHAR(64) NOT NULL,    -- user_id (NOT NULL — 의료 audit)
        why           NVARCHAR(MAX) NULL,
        agent_run_id  VARCHAR(64) NULL,
        [timestamp]   DATETIME NOT NULL DEFAULT GETUTCDATE()
    );

    CREATE INDEX ix_agent_memory_audit_timestamp ON agent_memory_audit ([timestamp]);
    CREATE INDEX ix_agent_memory_audit_who       ON agent_memory_audit (who);
    PRINT '[create] agent_memory_audit (who NOT NULL)';
END
ELSE
BEGIN
    -- 이미 있는 경우 who NULL → NOT NULL ALTER 진행 (Architect SUGGESTION)
    DECLARE @is_nullable BIT;
    SELECT @is_nullable = is_nullable
    FROM sys.columns
    WHERE object_id = OBJECT_ID('agent_memory_audit') AND name = 'who';

    IF @is_nullable = 1
    BEGIN
        UPDATE agent_memory_audit
        SET who = 'SYSTEM'
        WHERE who IS NULL;
        PRINT '[backfill] agent_memory_audit.who NULL → ''SYSTEM''';

        ALTER TABLE agent_memory_audit ALTER COLUMN who VARCHAR(64) NOT NULL;
        PRINT '[alter] agent_memory_audit.who NOT NULL';
    END
    ELSE
        PRINT '[skip] agent_memory_audit.who already NOT NULL';
END

-- ─────────────────────────────────────────────────────────────────────────────
-- 5) agent_skill_invocation — Skill 호출 audit (tool history)
-- ─────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'agent_skill_invocation')
BEGIN
    CREATE TABLE agent_skill_invocation (
        id             INT IDENTITY(1,1) PRIMARY KEY,
        agent_run_id   VARCHAR(64) NULL,
        session_id     VARCHAR(64) NULL,
        user_id        VARCHAR(64) NULL,
        group_id       VARCHAR(64) NOT NULL,    -- RBAC 추적 — NOT NULL
        skill_name     VARCHAR(64) NOT NULL,
        args_json      NVARCHAR(MAX) NULL,      -- 2000자 truncate (middleware 보호)
        status         VARCHAR(32) NOT NULL,    -- SUCCESS | DENIED | CLARIFICATION_NEEDED | ERROR | BLOCKED
        error_message  NVARCHAR(MAX) NULL,
        latency_ms     FLOAT NULL,
        [timestamp]    DATETIME NOT NULL DEFAULT GETUTCDATE()
    );

    CREATE INDEX ix_skill_inv_agent_run  ON agent_skill_invocation (agent_run_id);
    CREATE INDEX ix_skill_inv_session    ON agent_skill_invocation (session_id);
    CREATE INDEX ix_skill_inv_user       ON agent_skill_invocation (user_id);
    CREATE INDEX ix_skill_inv_group      ON agent_skill_invocation (group_id);
    CREATE INDEX ix_skill_inv_skill_name ON agent_skill_invocation (skill_name);
    CREATE INDEX ix_skill_inv_status     ON agent_skill_invocation (status);
    CREATE INDEX ix_skill_inv_timestamp  ON agent_skill_invocation ([timestamp]);
    PRINT '[create] agent_skill_invocation';
END
ELSE
    PRINT '[skip] agent_skill_invocation already exists';

COMMIT TRANSACTION;
PRINT '[done] migration 2026-05-19 committed';
GO

-- =============================================================================
-- 검증 쿼리 — 운영팀 실행 후 결과 확인
-- =============================================================================
-- 1) 5 테이블 모두 생성됨 (결과: 5)
SELECT COUNT(*) AS created_tables
FROM sys.tables
WHERE name IN (
    'agent_conversation',
    'agent_conversation_message',
    'agent_user_memory',
    'agent_memory_audit',
    'agent_skill_invocation'
);

-- 2) agent_memory_audit.who NOT NULL 확인 (결과: is_nullable = 0)
SELECT name, is_nullable
FROM sys.columns
WHERE object_id = OBJECT_ID('agent_memory_audit') AND name = 'who';

-- 3) 기존 NULL row 잔재 없음 (결과: 0)
SELECT COUNT(*) AS null_who_rows
FROM agent_memory_audit
WHERE who IS NULL;

-- =============================================================================
-- 롤백 (긴급 시) — 데이터 손실 주의
-- =============================================================================
/*
BEGIN TRANSACTION;
DROP TABLE IF EXISTS agent_skill_invocation;
DROP TABLE IF EXISTS agent_memory_audit;
DROP TABLE IF EXISTS agent_user_memory;
DROP TABLE IF EXISTS agent_conversation_message;  -- FK 먼저
DROP TABLE IF EXISTS agent_conversation;
COMMIT TRANSACTION;
*/
