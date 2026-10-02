/* Migration 2026-10-02 — 근무표 버전 출처(schedule_lineage)
 *
 * 목적
 *   [새 버전으로 저장](`POST /roster/save-as`)으로 만든 근무표가 **어느 버전에서 왔는지**를 남긴다.
 *   버전 목록에 "VER3에서 수정 · 홍길동 · 10/12 14:03" 을 보여 주는 근거다(성남 요청 ③).
 *   새 버전 1개 = 1행. 원본 대비 바뀐 칸 자체는 `schedule_entry_log`(source='save_as')에 남는다.
 *
 * ★ `schedules` 에 컬럼을 더하지 않고 따로 둔 이유 — 모델에 컬럼을 더하면 모든 근무표 조회가 그
 *   컬럼을 읽는다. DDL 이 빠진 채 배포되면 근무표 화면 전체가 멈춘다. 따로 두면 영향이
 *   새 버전 저장·버전 목록의 출처 표시로 한정된다(버전 목록은 이 테이블이 없어도 출처만 비운다).
 *
 * ★ 접속한 DB 에 그대로 적용된다. USE 를 쓰지 않는다 — 하드코딩하면 dev 에 넣으려 해도
 *   운영으로 간다. 실행 대상은 연결/-d 로 지정할 것.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *
 * ★★ 코드 배포보다 **먼저** 적용한다. 없으면 [새 버전으로 저장]이 실패한다(같은 트랜잭션).
 *
 * ★ collation: `schedules.schedule_id` 와 같은 varchar(50) Korean_Wansung_CI_AS 를 명시한다([MSSQL-09]).
 *   DB 기본은 SQL_Latin1 이라 생략하면 schedules 와 조인하는 순간 오류 468 이 난다.
 *
 * 적용 이력: eun_roster_dev 2026-10-02 16:21 / eun_roster 2026-10-02 16:21 선적용(코드 배포 전 · 둘 다 재실행 검증 통과 · 행 0)
 */

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_lineage'
)
BEGIN
    CREATE TABLE dbo.schedule_lineage (
        schedule_id         varchar(50)  COLLATE Korean_Wansung_CI_AS NOT NULL,  -- 새로 만든 버전
        source_schedule_id  varchar(50)  COLLATE Korean_Wansung_CI_AS NOT NULL,  -- 원본 버전
        source_version      bigint       NULL,                                   -- 저장 시점 원본 VER(원본이 지워져도 표시용)
        kind                varchar(10)  COLLATE Korean_Wansung_CI_AS NOT NULL,  -- 'save_as'
        -- 원본 대비 요약 — /roster/compare 의 summary 와 같은 기준(근무코드 비교 · 명단 변경은 따로)
        changed_cells       int          NOT NULL,                               -- 양쪽 명단에 다 있는 간호사의 바뀐 칸 수
        added_nurses        int          NOT NULL,                               -- 새 버전에만 있는 간호사 수
        removed_nurses      int          NOT NULL,                               -- 원본에만 있던 간호사 수
        created_by          varchar(50)  COLLATE Korean_Wansung_CI_AS NULL,      -- 저장한 사람 account_id(schedules.created_by 와 같은 기준)
        created_at          datetime     NOT NULL,

        CONSTRAINT PK_schedule_lineage PRIMARY KEY CLUSTERED (schedule_id)
    );
END;

/* ★ 이미 있던 테이블이 지금 정의와 **정확히** 같은지 확인한다. 다르면 고쳐 맞추지 않고 멈춘다
 *   (push_outbox 와 같은 규칙 — 이름만 보고 건너뛰면 예전 모양이 조용히 남는다).
 *   대조: 컬럼 집합(양방향)·형·길이·NULL 허용·collation · PK(schedule_id 하나, 활성 클러스터형).
 *   멈췄다면: 비어 있으면 DROP TABLE dbo.schedule_lineage 후 다시 실행. */
DECLARE @oid int = OBJECT_ID('dbo.schedule_lineage');
DECLARE @problem nvarchar(300) = NULL;
DECLARE @col sysname = NULL;

DECLARE @expected TABLE (name sysname, data_type sysname, char_len int NULL, nullable varchar(3), collation sysname NULL);
INSERT INTO @expected (name, data_type, char_len, nullable, collation) VALUES
    ('schedule_id',        'varchar',  50,   'NO',  'Korean_Wansung_CI_AS'),
    ('source_schedule_id', 'varchar',  50,   'NO',  'Korean_Wansung_CI_AS'),
    ('source_version',     'bigint',   NULL, 'YES', NULL),
    ('kind',               'varchar',  10,   'NO',  'Korean_Wansung_CI_AS'),
    ('changed_cells',      'int',      NULL, 'NO',  NULL),
    ('added_nurses',       'int',      NULL, 'NO',  NULL),
    ('removed_nurses',     'int',      NULL, 'NO',  NULL),
    ('created_by',         'varchar',  50,   'YES', 'Korean_Wansung_CI_AS'),
    ('created_at',         'datetime', NULL, 'NO',  NULL);

DECLARE @actual TABLE (name sysname, data_type sysname, char_len int NULL, nullable varchar(3), collation sysname NULL);
INSERT INTO @actual (name, data_type, char_len, nullable, collation)
SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLLATION_NAME
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_lineage';

-- ★ 방향마다 따로 감싼다. EXCEPT·UNION 은 왼쪽부터 묶여서 한 줄로 이으면 빠진 컬럼을 놓친다.
SELECT TOP 1 @col = name FROM (
    SELECT name FROM (
        SELECT name, data_type, char_len, nullable, collation FROM @expected
        EXCEPT SELECT name, data_type, char_len, nullable, collation FROM @actual
    ) AS missing_or_changed
    UNION ALL
    SELECT name FROM (
        SELECT name, data_type, char_len, nullable, collation FROM @actual
        EXCEPT SELECT name, data_type, char_len, nullable, collation FROM @expected
    ) AS extra_or_changed
) AS diff ORDER BY name;

DECLARE @pk int = (SELECT index_id FROM sys.indexes WHERE object_id = @oid AND is_primary_key = 1);

IF @col IS NOT NULL
    SET @problem = N'컬럼 정의가 다름(형·길이·NULL·collation·있고 없음): ' + @col;
ELSE IF @pk IS NULL
     OR (SELECT type FROM sys.indexes WHERE object_id = @oid AND index_id = @pk) <> 1
     OR (SELECT is_disabled FROM sys.indexes WHERE object_id = @oid AND index_id = @pk) = 1
     OR (SELECT COUNT(*) FROM sys.index_columns WHERE object_id = @oid AND index_id = @pk AND is_included_column = 0) <> 1
     OR NOT EXISTS (SELECT 1 FROM sys.index_columns
                     WHERE object_id = @oid AND index_id = @pk AND key_ordinal = 1
                       AND COL_NAME(@oid, column_id) = 'schedule_id')
    SET @problem = N'PK 가 schedule_id 하나의 활성 클러스터형이 아님';

IF @problem IS NOT NULL
BEGIN
    DECLARE @msg nvarchar(500) = N'schedule_lineage 스키마가 기대와 다릅니다: ' + @problem
        + N' — 비어 있으면 DROP TABLE dbo.schedule_lineage 후 다시 실행.';
    THROW 50001, @msg, 1;
END;
