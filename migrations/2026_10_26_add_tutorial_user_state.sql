/* Migration 2026-10-26 — 튜토리얼(사용 안내) 계정별 상태(tutorial_user_state)
 *
 * 목적
 *   온:마음 PC 웹 사용 안내(튜토리얼 오버레이)의 상태를 계정 기준으로 남긴다(기획 2-5 —
 *   병동 PC 를 여러 사람이 같이 쓰므로 브라우저가 아니라 계정 기준).
 *   1차 키: welcome_shown('1' = 첫 접속 카드 레이어를 보여 줌) · next_month_guide('1'|'0' = 끝 카드
 *   '다음 달 근무표 만들 때도 안내 보기'). 2차(자동 제안)는 같은 표에 키만 더한다 — DDL 을 다시
 *   쓰지 않으려고 키-값 모양으로 둔다. 진행 위치·완료 여부는 저장하지 않는다(기획 2-5).
 *
 * ★ 접속한 DB 에 그대로 적용된다. USE 를 쓰지 않는다 — 실행 대상은 연결/-d 로 지정할 것.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *
 * ★ 코드 배포보다 먼저 적용한다. 없으면 GET/PUT /tutorial/state 가 500 을 낸다
 *   (화면은 실패해도 안내가 동작하게 만들었지만 첫 접속 카드가 자동으로 뜨지 않는다).
 *
 * ★ collation: 문자 컬럼은 기존 roster 컬럼과 같은 Korean_Wansung_CI_AS 를 명시한다([MSSQL-09]).
 *   DB 기본은 SQL_Latin1 이라 생략하면 다른 표와 조인할 때 오류 468 이 난다.
 *
 * 적용 이력: eun_roster_dev 2026-10-27 10:42 (재실행 검증 통과 · 행 0) / eun_roster 미적용(코드 배포 전에 승인 뒤 적용)
 */

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'tutorial_user_state')
BEGIN
    CREATE TABLE dbo.tutorial_user_state (
        office_id    varchar(50)   COLLATE Korean_Wansung_CI_AS NOT NULL,  -- JWT office_id
        account_id   varchar(50)   COLLATE Korean_Wansung_CI_AS NOT NULL,  -- JWT account_id(로그인 계정)
        state_key    varchar(100)  COLLATE Korean_Wansung_CI_AS NOT NULL,  -- welcome_shown | next_month_guide | (2차) …
        state_value  nvarchar(200) COLLATE Korean_Wansung_CI_AS NOT NULL,  -- 짧은 문자열. 지우면 행을 삭제
        updated_at   datetime      NOT NULL,
        CONSTRAINT PK_tutorial_user_state PRIMARY KEY CLUSTERED (office_id, account_id, state_key)
    );
END;

/* ★ 이미 있던 테이블이 지금 정의와 정확히 같은지 확인한다. 다르면 고쳐 맞추지 않고 멈춘다
 *   (roster_change_ack·schedule_lineage 와 같은 규칙). 대조: 컬럼 집합(양방향)·형·길이·NULL·collation · PK 키 순서.
 *   멈췄다면: 비어 있으면 DROP TABLE dbo.tutorial_user_state 후 다시 실행. */
DECLARE @oid int = OBJECT_ID('dbo.tutorial_user_state');

DECLARE @expected TABLE (name sysname, data_type sysname, char_len int NULL, nullable varchar(3), collation sysname NULL);
INSERT INTO @expected (name, data_type, char_len, nullable, collation) VALUES
    ('office_id',   'varchar',  50,   'NO', 'Korean_Wansung_CI_AS'),
    ('account_id',  'varchar',  50,   'NO', 'Korean_Wansung_CI_AS'),
    ('state_key',   'varchar',  100,  'NO', 'Korean_Wansung_CI_AS'),
    ('state_value', 'nvarchar', 200,  'NO', 'Korean_Wansung_CI_AS'),
    ('updated_at',  'datetime', NULL, 'NO', NULL);

DECLARE @actual TABLE (name sysname, data_type sysname, char_len int NULL, nullable varchar(3), collation sysname NULL);
INSERT INTO @actual (name, data_type, char_len, nullable, collation)
SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLLATION_NAME
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'tutorial_user_state';

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

DECLARE @pk int, @pk_type tinyint, @pk_dis bit;
SELECT @pk = index_id, @pk_type = type, @pk_dis = is_disabled FROM sys.indexes WHERE object_id = @oid AND is_primary_key = 1;
DECLARE @pk_keys nvarchar(200) = (
    SELECT STRING_AGG(CASE WHEN is_descending_key = 1 THEN N'-' ELSE N'' END + COL_NAME(@oid, column_id), N',')
           WITHIN GROUP (ORDER BY key_ordinal)
      FROM sys.index_columns WHERE object_id = @oid AND index_id = @pk AND is_included_column = 0);

DECLARE @problem nvarchar(300) = NULL;
IF @col IS NOT NULL
    SET @problem = N'컬럼 정의가 다름(형·길이·NULL·collation·있고 없음): ' + @col;
ELSE IF @pk IS NULL OR @pk_type <> 1 OR @pk_dis = 1 OR ISNULL(@pk_keys, N'') <> N'office_id,account_id,state_key'
    SET @problem = N'PK 가 (office_id, account_id, state_key) 오름차순 활성 클러스터형이 아님: (' + ISNULL(@pk_keys, N'') + N')';

IF @problem IS NOT NULL
BEGIN
    DECLARE @msg nvarchar(500) = N'tutorial_user_state 스키마가 기대와 다릅니다: ' + @problem
        + N' — 비어 있으면 DROP TABLE dbo.tutorial_user_state 후 다시 실행.';
    THROW 50001, @msg, 1;
END;
