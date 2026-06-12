-- =============================================================================
-- Migration 2026-05-29 — agent_llm_usage (병동·간호사별 LLM 토큰/비용 추적)
-- Target dialect: MSSQL (Azure SQL / SQL Server)
-- =============================================================================
-- 매 LLM 호출(turn/router/memory/preview)의 토큰·비용을 1행씩 적재.
-- usage.record_llm_usage 가 기록, usage.usage_summary 가 group/nurse/model 집계.
-- 멱등성: IF NOT EXISTS 가드. 재실행 안전.
--
-- 적용 절차: 1) DB 백업  2) 본 스크립트 실행  3) 검증 쿼리 결과 확인  4) 이상 시 ROLLBACK.
-- =============================================================================

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'agent_llm_usage')
BEGIN
    CREATE TABLE agent_llm_usage (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        conversation_id VARCHAR(64) NULL,
        group_id        VARCHAR(64) NOT NULL,
        user_id         VARCHAR(64) NULL,
        model           VARCHAR(64) NULL,
        purpose         VARCHAR(32) NULL,   -- turn | router | memory | preview
        input_tokens    INT NOT NULL DEFAULT 0,
        output_tokens   INT NOT NULL DEFAULT 0,
        cost_usd        FLOAT NOT NULL DEFAULT 0,
        timestamp       DATETIME NOT NULL DEFAULT GETUTCDATE()
    );

    CREATE INDEX ix_agent_llm_usage_group_id  ON agent_llm_usage (group_id);
    CREATE INDEX ix_agent_llm_usage_user_id   ON agent_llm_usage (user_id);
    CREATE INDEX ix_agent_llm_usage_model     ON agent_llm_usage (model);
    CREATE INDEX ix_agent_llm_usage_purpose   ON agent_llm_usage (purpose);
    CREATE INDEX ix_agent_llm_usage_timestamp ON agent_llm_usage (timestamp);
    PRINT '[create] agent_llm_usage';
END
ELSE
    PRINT '[skip] agent_llm_usage already exists';

COMMIT TRANSACTION;

-- 검증: 테이블 존재 확인 (1 이어야 함)
SELECT COUNT(*) AS agent_llm_usage_exists FROM sys.tables WHERE name = 'agent_llm_usage';
