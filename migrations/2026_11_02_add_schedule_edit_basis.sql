/* Migration 2026-11-02 — 근무표 수정 판정 기준 사본(schedule_edit_basis)
 *
 * 목적
 *   근무표 수정 에이전트가 '그 근무표를 만들 때의 값'으로 판정하도록, 생성 직후 판정 재료를 근무표별로
 *   한 벌 남긴다 — 일별 최소·최대 인원(daily_shift), 등급·팀 최소, 간호사 속성·월 한도, 원티드,
 *   생성 당시 전월 꼬리, 일자별 배정 상태, 가로축 분배 지표.
 *   daily_shift · roster_grade_config · teams 는 덮어쓰기라 이력이 없어 따로 남겨야 한다.
 *   [새 버전으로 저장]으로 만든 버전은 원본 사본을 그대로 이어받는다.
 *
 * ★ schedules 에 컬럼을 더하지 않고 따로 둔다(schedule_lineage 와 같은 이유) — 이 표가 없어도
 *   근무표 화면·생성은 그대로 돈다(생성 쪽 사본 쓰기는 표가 없으면 건너뛴다).
 *
 * ★ 접속한 DB 에 그대로 적용된다. USE 를 쓰지 않는다 — 하드코딩하면 dev 에 넣으려 해도 운영으로 간다.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *
 * ★ collation: schedules.schedule_id 와 같은 varchar(50) Korean_Wansung_CI_AS 를 명시한다([MSSQL-09]).
 *
 * 적용 이력: eun_roster_dev 2026-11-02(재실행 검증 통과) / eun_roster 미적용(승인 뒤 코드 배포 전)
 */

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_edit_basis'
)
BEGIN
    CREATE TABLE dbo.schedule_edit_basis (
        schedule_id         varchar(50)   COLLATE Korean_Wansung_CI_AS NOT NULL,
        source_schedule_id  varchar(50)   COLLATE Korean_Wansung_CI_AS NULL,      -- 이어받았으면 원본 버전
        basis_version       int           NOT NULL,                               -- payload 형식 번호
        payload             nvarchar(max) COLLATE Korean_Wansung_CI_AS NOT NULL,  -- JSON
        created_at          datetime      NOT NULL,

        CONSTRAINT PK_schedule_edit_basis PRIMARY KEY CLUSTERED (schedule_id)
    );
END;

/* ★ 이미 있던 테이블이 지금 정의와 같은지 확인한다. 다르면 고쳐 맞추지 않고 멈춘다.
 *   멈췄다면: 비어 있으면 DROP TABLE dbo.schedule_edit_basis 후 다시 실행. */
IF (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_edit_basis'
       AND (
            (COLUMN_NAME = 'schedule_id'        AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 50
                                                 AND IS_NULLABLE = 'NO'  AND COLLATION_NAME = 'Korean_Wansung_CI_AS')
         OR (COLUMN_NAME = 'source_schedule_id' AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 50
                                                 AND IS_NULLABLE = 'YES' AND COLLATION_NAME = 'Korean_Wansung_CI_AS')
         OR (COLUMN_NAME = 'basis_version'      AND DATA_TYPE = 'int'      AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'payload'            AND DATA_TYPE = 'nvarchar' AND CHARACTER_MAXIMUM_LENGTH = -1
                                                 AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'created_at'         AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO')
       )
) <> 5
OR (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_edit_basis'
) <> 5
OR NOT EXISTS (
    SELECT 1 FROM sys.indexes
     WHERE object_id = OBJECT_ID('dbo.schedule_edit_basis') AND name = 'PK_schedule_edit_basis' AND is_primary_key = 1
)
BEGIN
    RAISERROR('schedule_edit_basis 가 이 파일의 정의와 다르다 — 수동 확인 필요(빈 테이블이면 DROP 후 재실행).', 16, 1);
END;
