/* Migration 2026-10-02 — 재마감 변경 확인 기록(roster_change_ack)
 *
 * 목적 (성남 요청 ② · 설계 9-28 "근무표 변경 확인·버전 관리 설계")
 *   재마감으로 근무표가 바뀌면 확인 대상자 1명당 1행을 미리 만들고, [확인]을 누른 시각을 남긴다.
 *   "N명 중 M명"·미확인자 명단·확인 증빙이 이 테이블에서 나온다.
 *
 * ★ 이 테이블 하나만 새로 만든다(10-02 사용자 결정). 알림 = 바뀐 칸이 있는 재마감의 마감 스냅샷이고,
 *   비교 기준·요약은 `issued_roster_snapshot.meta_json.change` 에, 바뀐 칸은 두 스냅샷을 비교해
 *   그때그때 계산한다. 알림 상태(확인 중·완료·대체됨)도 저장하지 않고 스냅샷 활성 여부와 이 표로 판정한다.
 *
 * ★ 접속한 DB 에 그대로 적용된다. USE 를 쓰지 않는다 — 실행 대상은 연결/-d 로 지정할 것.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *
 * ★ 코드 배포보다 먼저 적용한다. 없으면 재마감 때 변경 알림만 빠진다(마감 자체는 SAVEPOINT 로 살아남고
 *   기존 재마감 푸시로 대신 — 오류 로그가 남는다).
 *
 * ★ collation: 문자 컬럼은 기존 roster 컬럼과 같은 Korean_Wansung_CI_AS 를 명시한다([MSSQL-09]).
 *
 * 적용 이력: eun_roster_dev 2026-10-02 16:42 / eun_roster 2026-10-02 16:42 선적용(코드 배포 전 · 둘 다 재실행 검증 통과 · 행 0)
 */

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'roster_change_ack')
BEGIN
    CREATE TABLE dbo.roster_change_ack (
        snapshot_id       int           NOT NULL,   -- issued_roster_snapshot.snapshot_id (= 알림 번호)
        nurse_id          varchar(50)   COLLATE Korean_Wansung_CI_AS NOT NULL,
        my_changed_cells  int           NOT NULL,   -- 이 사람 칸이 바뀐 수(명단 변경 포함) — 푸시 문구·홈 배너
        acked_at          datetime      NULL,       -- 미확인이면 NULL
        ack_via           varchar(10)   COLLATE Korean_Wansung_CI_AS NULL,  -- app | web
        CONSTRAINT PK_roster_change_ack PRIMARY KEY CLUSTERED (snapshot_id, nurse_id)
    );
END;

-- "내가 아직 확인 안 한 알림"(앱 홈 배너)을 찾는다.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_roster_change_ack_nurse' AND object_id = OBJECT_ID('dbo.roster_change_ack'))
    CREATE NONCLUSTERED INDEX IX_roster_change_ack_nurse ON dbo.roster_change_ack (nurse_id) INCLUDE (acked_at);

/* ★ 이미 있던 테이블·인덱스가 지금 정의와 **정확히** 같은지 확인한다. 다르면 고쳐 맞추지 않고 멈춘다
 *   (push_outbox 와 같은 규칙). 대조: 컬럼 집합(양방향)·형·길이·NULL·collation · PK 키 순서 ·
 *   보조 인덱스(비클러스터형·활성·필터 없음·unique 아님·키·INCLUDE). 멈췄다면: 비어 있으면 DROP 후 다시 실행. */
DECLARE @oid int = OBJECT_ID('dbo.roster_change_ack');

DECLARE @expected TABLE (name sysname, data_type sysname, char_len int NULL, nullable varchar(3), collation sysname NULL);
INSERT INTO @expected (name, data_type, char_len, nullable, collation) VALUES
    ('snapshot_id',      'int',      NULL, 'NO',  NULL),
    ('nurse_id',         'varchar',  50,   'NO',  'Korean_Wansung_CI_AS'),
    ('my_changed_cells', 'int',      NULL, 'NO',  NULL),
    ('acked_at',         'datetime', NULL, 'YES', NULL),
    ('ack_via',          'varchar',  10,   'YES', 'Korean_Wansung_CI_AS');

DECLARE @actual TABLE (name sysname, data_type sysname, char_len int NULL, nullable varchar(3), collation sysname NULL);
INSERT INTO @actual (name, data_type, char_len, nullable, collation)
SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLLATION_NAME
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'roster_change_ack';

DECLARE @col sysname = NULL;
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

-- 키·INCLUDE 를 순서대로 이어 붙여 비교한다(내림차순 키는 '-' 를 붙여 다르게 보이게).
DECLARE @pk int, @pk_type tinyint, @pk_dis bit;
SELECT @pk = index_id, @pk_type = type, @pk_dis = is_disabled FROM sys.indexes WHERE object_id = @oid AND is_primary_key = 1;
DECLARE @pk_keys nvarchar(200) = (
    SELECT STRING_AGG(CASE WHEN is_descending_key = 1 THEN N'-' ELSE N'' END + COL_NAME(@oid, column_id), N',')
           WITHIN GROUP (ORDER BY key_ordinal)
      FROM sys.index_columns WHERE object_id = @oid AND index_id = @pk AND is_included_column = 0);

DECLARE @ix int, @ix_type tinyint, @ix_dis bit, @ix_filt bit, @ix_uniq bit;
SELECT @ix = index_id, @ix_type = type, @ix_dis = is_disabled, @ix_filt = has_filter, @ix_uniq = is_unique
  FROM sys.indexes WHERE object_id = @oid AND name = 'IX_roster_change_ack_nurse';
DECLARE @ix_keys nvarchar(200) = (
    SELECT STRING_AGG(CASE WHEN is_descending_key = 1 THEN N'-' ELSE N'' END + COL_NAME(@oid, column_id), N',')
           WITHIN GROUP (ORDER BY key_ordinal)
      FROM sys.index_columns WHERE object_id = @oid AND index_id = @ix AND is_included_column = 0);
DECLARE @ix_incs nvarchar(200) = (
    SELECT STRING_AGG(COL_NAME(@oid, column_id), N',') WITHIN GROUP (ORDER BY COL_NAME(@oid, column_id))
      FROM sys.index_columns WHERE object_id = @oid AND index_id = @ix AND is_included_column = 1);

DECLARE @problem nvarchar(300) = NULL;
IF @col IS NOT NULL
    SET @problem = N'컬럼 정의가 다름(형·길이·NULL·collation·있고 없음): ' + @col;
ELSE IF @pk IS NULL OR @pk_type <> 1 OR @pk_dis = 1 OR ISNULL(@pk_keys, N'') <> N'snapshot_id,nurse_id'
    SET @problem = N'PK 가 (snapshot_id, nurse_id) 오름차순 활성 클러스터형이 아님: (' + ISNULL(@pk_keys, N'') + N')';
ELSE IF @ix IS NULL
    SET @problem = N'IX_roster_change_ack_nurse 없음';
ELSE IF @ix_type <> 2 OR @ix_dis = 1 OR @ix_filt = 1 OR @ix_uniq = 1
    SET @problem = N'IX_roster_change_ack_nurse 가 비클러스터형·활성·필터 없음·unique 아님 이 아님';
ELSE IF ISNULL(@ix_keys, N'') <> N'nurse_id' OR ISNULL(@ix_incs, N'') <> N'acked_at'
    SET @problem = N'IX_roster_change_ack_nurse 키/INCLUDE 가 다름: (' + ISNULL(@ix_keys, N'') + N') INCLUDE (' + ISNULL(@ix_incs, N'') + N')';

IF @problem IS NOT NULL
BEGIN
    DECLARE @msg nvarchar(500) = N'roster_change_ack 스키마가 기대와 다릅니다: ' + @problem
        + N' — 비어 있으면 DROP TABLE dbo.roster_change_ack 후 다시 실행.';
    THROW 50001, @msg, 1;
END;
