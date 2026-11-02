/* Migration 2026-11-02 — 근무표 수정 에이전트 기록(schedule_edit_log)
 *
 * 목적
 *   수정 에이전트 대화의 모든 턴을 한 줄씩 남긴다 — 사용자 원문·LLM 해석·확정된 요청·결과(후보·레버·이유)·
 *   사용자 행동(고름·넘김·레버·적용)·걸린 시간. 대화 id 하나로 그 대화 전체를 순서대로 다시 볼 수 있고,
 *   병동별 계수(레버 비용·품질 무게) 학습·응답 시간 분석의 재료다. 적용으로 만든 새 버전의 '에이전트 출처'다.
 *   서버가 재기동돼도 이 기록으로 대화 상태(요청·허용 레버·넘긴 묶음)를 다시 세운다.
 *
 * ★ 이 표가 없어도 수정 에이전트는 돈다 — 기록만 건너뛴다(services/schedule_edit/journal.py).
 *
 * ★ 접속한 DB 에 그대로 적용된다. USE 를 쓰지 않는다.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *
 * ★ collation: schedules.schedule_id 와 같은 varchar(50) Korean_Wansung_CI_AS 를 명시한다([MSSQL-09]).
 *
 * 적용 이력: eun_roster_dev 2026-11-02(대화 칸 추가판으로 다시 만듦 · 재실행 검증 통과) / eun_roster 미적용(승인 뒤 코드 배포 전)
 */

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_edit_log'
)
BEGIN
    CREATE TABLE dbo.schedule_edit_log (
        log_id           bigint IDENTITY(1,1) NOT NULL,
        conversation_id  varchar(64)   COLLATE Korean_Wansung_CI_AS NULL,      -- 대화 id(UUID · AIDE 와 같은 형식)
        turn_no          int           NULL,                                   -- 대화 안 순서(1부터)
        schedule_id      varchar(50)   COLLATE Korean_Wansung_CI_AS NOT NULL,  -- 수정 대상(원본) 버전
        new_schedule_id  varchar(50)   COLLATE Korean_Wansung_CI_AS NULL,      -- 적용으로 만든 새 버전
        event            varchar(10)   COLLATE Korean_Wansung_CI_AS NOT NULL,  -- message · propose · clarify · lever · reject · keep · apply · error
        payload          nvarchar(max) COLLATE Korean_Wansung_CI_AS NOT NULL,  -- JSON
        elapsed_ms       int           NULL,                                   -- 이 턴 처리 시간
        created_by       varchar(50)   COLLATE Korean_Wansung_CI_AS NULL,      -- account_id
        created_at       datetime      NOT NULL,

        CONSTRAINT PK_schedule_edit_log PRIMARY KEY CLUSTERED (log_id)
    );
    CREATE INDEX IX_schedule_edit_log_schedule ON dbo.schedule_edit_log (schedule_id, created_at);
    CREATE INDEX IX_schedule_edit_log_conversation ON dbo.schedule_edit_log (conversation_id, log_id);
END;

/* ★ 이미 있던 테이블이 지금 정의와 같은지 확인한다. 다르면 멈춘다(빈 테이블이면 DROP 후 재실행). */
IF (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_edit_log'
       AND (
            (COLUMN_NAME = 'log_id'          AND DATA_TYPE = 'bigint'   AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'conversation_id' AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 64
                                              AND IS_NULLABLE = 'YES' AND COLLATION_NAME = 'Korean_Wansung_CI_AS')
         OR (COLUMN_NAME = 'turn_no'         AND DATA_TYPE = 'int'      AND IS_NULLABLE = 'YES')
         OR (COLUMN_NAME = 'schedule_id'     AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 50
                                              AND IS_NULLABLE = 'NO'  AND COLLATION_NAME = 'Korean_Wansung_CI_AS')
         OR (COLUMN_NAME = 'new_schedule_id' AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 50
                                              AND IS_NULLABLE = 'YES' AND COLLATION_NAME = 'Korean_Wansung_CI_AS')
         OR (COLUMN_NAME = 'event'           AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 10
                                              AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'payload'         AND DATA_TYPE = 'nvarchar' AND CHARACTER_MAXIMUM_LENGTH = -1
                                              AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'elapsed_ms'      AND DATA_TYPE = 'int'      AND IS_NULLABLE = 'YES')
         OR (COLUMN_NAME = 'created_by'      AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 50
                                              AND IS_NULLABLE = 'YES')
         OR (COLUMN_NAME = 'created_at'      AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO')
       )
) <> 10
OR (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_edit_log'
) <> 10
OR NOT EXISTS (
    SELECT 1 FROM sys.indexes
     WHERE object_id = OBJECT_ID('dbo.schedule_edit_log') AND name = 'PK_schedule_edit_log' AND is_primary_key = 1
)
OR NOT EXISTS (
    SELECT 1 FROM sys.indexes
     WHERE object_id = OBJECT_ID('dbo.schedule_edit_log') AND name = 'IX_schedule_edit_log_conversation'
)
BEGIN
    RAISERROR('schedule_edit_log 이 이 파일의 정의와 다르다 — 수동 확인 필요(빈 테이블이면 DROP 후 재실행).', 16, 1);
END;
