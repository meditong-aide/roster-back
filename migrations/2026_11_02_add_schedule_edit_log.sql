/* Migration 2026-11-02 — 근무표 수정 에이전트 기록(schedule_edit_log)
 *
 * 목적
 *   수정 에이전트의 제안·레버 선택·넘김·적용을 한 건씩 남긴다. 수간호사가 무엇을 보고 무엇을 골랐는지가
 *   병동별 계수(레버 비용·품질 무게) 학습의 재료이고, 적용으로 만든 새 버전의 '에이전트 출처'다.
 *
 * ★ 이 표가 없어도 수정 에이전트는 돈다 — 기록만 건너뛴다(services/schedule_edit/journal.py).
 *
 * ★ 접속한 DB 에 그대로 적용된다. USE 를 쓰지 않는다.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *
 * ★ collation: schedules.schedule_id 와 같은 varchar(50) Korean_Wansung_CI_AS 를 명시한다([MSSQL-09]).
 *
 * 적용 이력: eun_roster_dev 2026-11-02(재실행 검증 통과) / eun_roster 미적용(승인 뒤 코드 배포 전)
 */

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_edit_log'
)
BEGIN
    CREATE TABLE dbo.schedule_edit_log (
        log_id           bigint IDENTITY(1,1) NOT NULL,
        schedule_id      varchar(50)   COLLATE Korean_Wansung_CI_AS NOT NULL,  -- 수정 대상(원본) 버전
        new_schedule_id  varchar(50)   COLLATE Korean_Wansung_CI_AS NULL,      -- 적용으로 만든 새 버전
        event            varchar(10)   COLLATE Korean_Wansung_CI_AS NOT NULL,  -- propose · lever · reject · apply
        payload          nvarchar(max) COLLATE Korean_Wansung_CI_AS NOT NULL,  -- JSON
        created_by       varchar(50)   COLLATE Korean_Wansung_CI_AS NULL,      -- account_id
        created_at       datetime      NOT NULL,

        CONSTRAINT PK_schedule_edit_log PRIMARY KEY CLUSTERED (log_id)
    );
    CREATE INDEX IX_schedule_edit_log_schedule ON dbo.schedule_edit_log (schedule_id, created_at);
END;

/* ★ 이미 있던 테이블이 지금 정의와 같은지 확인한다. 다르면 멈춘다(빈 테이블이면 DROP 후 재실행). */
IF (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_edit_log'
       AND (
            (COLUMN_NAME = 'log_id'          AND DATA_TYPE = 'bigint'   AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'schedule_id'     AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 50
                                              AND IS_NULLABLE = 'NO'  AND COLLATION_NAME = 'Korean_Wansung_CI_AS')
         OR (COLUMN_NAME = 'new_schedule_id' AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 50
                                              AND IS_NULLABLE = 'YES' AND COLLATION_NAME = 'Korean_Wansung_CI_AS')
         OR (COLUMN_NAME = 'event'           AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 10
                                              AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'payload'         AND DATA_TYPE = 'nvarchar' AND CHARACTER_MAXIMUM_LENGTH = -1
                                              AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'created_by'      AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 50
                                              AND IS_NULLABLE = 'YES')
         OR (COLUMN_NAME = 'created_at'      AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO')
       )
) <> 7
OR (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_edit_log'
) <> 7
OR NOT EXISTS (
    SELECT 1 FROM sys.indexes
     WHERE object_id = OBJECT_ID('dbo.schedule_edit_log') AND name = 'PK_schedule_edit_log' AND is_primary_key = 1
)
BEGIN
    RAISERROR('schedule_edit_log 이 이 파일의 정의와 다르다 — 수동 확인 필요(빈 테이블이면 DROP 후 재실행).', 16, 1);
END;
