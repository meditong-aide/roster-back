/* Migration 2026-10-02 — 앱 알림 발송 대기열(push_outbox)
 *
 * 목적
 *   앱 알림(그룹웨어 푸시)을 업무 저장과 **같은 트랜잭션**에 "보낼 알림"으로 기록하고,
 *   서버 안 발송기(main._push_outbox_dispatcher)가 따로 보낸다. 실패하면 1분·5분·30분 뒤
 *   다시 시도하고, 그래도 안 되면 failed 로 남긴다.
 *
 * ★ 왜 필요한가 (2026-10 실측·리뷰)
 *   예전엔 업무를 커밋한 뒤 그룹웨어에 바로 썼다. 그 사이 프로세스가 죽거나 그룹웨어가
 *   잠깐 느리면 알림이 영영 빠졌고, 다시 보낼 근거가 남지 않았다(원티드 마감은 이미 closed 라
 *   다음 실행에서도 안 잡힌다). 이제 "보낼 알림" 자체가 남으므로 언제든 다시 보낸다.
 *
 * ★ 접속한 DB 에 그대로 적용된다. USE 를 쓰지 않는다 — 하드코딩하면 dev 에 넣으려 해도
 *   운영으로 간다. 실행 대상은 연결/-d 로 지정할 것.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *
 * ★★ 코드 배포보다 **먼저** 적용해야 한다. 이 테이블이 없으면 근무표 마감·원티드 요청처럼
 *   알림을 남기는 저장이 통째로 실패한다(같은 트랜잭션이라서).
 *
 * ★ collation: 기존 roster 컬럼과 같은 Korean_Wansung_CI_AS 를 명시한다([MSSQL-09]). DB 기본은
 *   SQL_Latin1 이라 생략하면 Latin1 이 붙고, 나중에 기존 테이블과 조인하는 순간 오류 468 이 난다.
 *   문구는 한글이라 nvarchar.
 *
 * 적용 이력: eun_roster_dev 2026-10-02 13:13 적용 / eun_roster 2026-10-02 13:19 선적용(코드 배포 전 · 둘 다 재실행 검증 통과)
 */

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'push_outbox'
)
BEGIN
    CREATE TABLE dbo.push_outbox (
        id                bigint         IDENTITY(1,1) NOT NULL,

        -- 무엇을 누구에게 (그룹웨어 TB_Mobile_Push_History_Master·User·TB_FCM 에 그대로 쓰는 값)
        source            varchar(50)    COLLATE Korean_Wansung_CI_AS NOT NULL,  -- 발생 지점(roster_publish·wanted_close 등)
        push_code         varchar(10)    COLLATE Korean_Wansung_CI_AS NOT NULL,
        push_sub_code     varchar(10)    COLLATE Korean_Wansung_CI_AS NOT NULL,
        office_code       varchar(50)    COLLATE Korean_Wansung_CI_AS NOT NULL,
        sender_emp_seq_no varchar(50)    COLLATE Korean_Wansung_CI_AS NOT NULL,
        sender_member_id  varchar(100)   COLLATE Korean_Wansung_CI_AS NOT NULL,
        recipients        varchar(max)   COLLATE Korean_Wansung_CI_AS NOT NULL,  -- 콤마 구분 사번(EmpSeqNo)
        recipient_count   int            NOT NULL,
        message           nvarchar(1000) COLLATE Korean_Wansung_CI_AS NOT NULL,
        org_message       nvarchar(2000) COLLATE Korean_Wansung_CI_AS NOT NULL,
        link_url          nvarchar(500)  COLLATE Korean_Wansung_CI_AS NOT NULL CONSTRAINT DF_push_outbox_link_url DEFAULT (''),
        link_code         nvarchar(100)  COLLATE Korean_Wansung_CI_AS NOT NULL CONSTRAINT DF_push_outbox_link_code DEFAULT (''),
        -- 보내기 직전에 확인할 조건. 예: 'wanted_closed:<group_id>:<year>:<month>' — 그 사이
        -- 다시 열렸으면 cancelled 로 접는다(이미 열린 원티드에 '마감' 알림이 가지 않게).
        guard             varchar(200)   COLLATE Korean_Wansung_CI_AS NULL,

        -- 진행 상태: pending → sending → sent | skipped(운영 외 환경) | failed(재시도 소진·24시간 경과) | cancelled(guard)
        -- ★ nvarchar 인 이유: pymssql 은 문자열 파라미터를 nvarchar 로 보낸다(실측). varchar 컬럼이면
        --   비교마다 형 변환이 끼어 아래 인덱스를 제대로 못 탈 수 있다. 발송기가 10초마다 이 컬럼으로 찾는다.
        status            nvarchar(20)   COLLATE Korean_Wansung_CI_AS NOT NULL CONSTRAINT DF_push_outbox_status DEFAULT ('pending'),
        attempts          int            NOT NULL CONSTRAINT DF_push_outbox_attempts DEFAULT (0),
        next_attempt_at   datetime       NOT NULL,
        claimed_at        datetime       NULL,
        -- 보낸 결과. 그룹웨어 쓰기와 같은 트랜잭션에서 status='sent' 와 함께 채운다
        master_idx        bigint         NULL,       -- TB_Mobile_Push_History_Master.Idx
        device_count      int            NULL,       -- 실제 푸시를 넣은 기기 수(0 이면 알림함에만)
        last_error        nvarchar(1000) COLLATE Korean_Wansung_CI_AS NULL,

        created_at        datetime       NOT NULL,
        sent_at           datetime       NULL,

        CONSTRAINT PK_push_outbox PRIMARY KEY CLUSTERED (id)
    );
END;

/* 발송기가 10초마다 '보낼 차례인 대기 건'을, 1분마다 '멈춘 건·오래된 건'을 찾는다.
 * ★ 세 조회 모두 이 인덱스만 읽고 끝나게 한다(INCLUDE). 보낸 이력(sent)이 쌓여도 status 로
 *   범위가 갈려 읽는 양이 늘지 않는다. 발송기 조회는 이 인덱스를 힌트로 고정한다
 *   (`push_outbox_service._claim_next`) — `TOP 1 ... ORDER BY id` 로 두면 대기 건이 없을 때
 *   이력 전체를 id 순으로 훑을 수 있다. */
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
     WHERE name = 'IX_push_outbox_due' AND object_id = OBJECT_ID('dbo.push_outbox')
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_push_outbox_due ON dbo.push_outbox (status, next_attempt_at)
        INCLUDE (created_at, claimed_at);
END;

/* ★ 이미 있던 테이블·인덱스가 지금 정의와 **정확히** 같은지 확인한다. 다르면 고쳐 맞추지 않고 멈춘다.
 *   이름만 보고 건너뛰면 예전 모양이 조용히 남아 발송기가 형 변환·전체 훑기를 하거나 쓰기에 실패한다
 *   (Codex 리뷰 2026-10-02 · 8~10회차). 2026-10-02 기준 dev·운영 어디에도 이 테이블이 없음을 확인했다
 *   — 다른 경로 적용·복원본 대비. 멈췄다면: 비어 있으면 DROP TABLE dbo.push_outbox 후 다시 실행.
 *   대조: 컬럼 집합(양방향)·형·길이·NULL 허용·collation · id IDENTITY · PK(id, 활성 클러스터형)
 *         · IX_push_outbox_due(비클러스터형·활성·필터 없음·unique 아님·키 2개 오름차순·INCLUDE 2개).
 *   기본값 제약은 보지 않는다 — 코드(`enqueue_push`)가 모든 NOT NULL 값을 직접 넣는다. */
DECLARE @oid int = OBJECT_ID('dbo.push_outbox');
DECLARE @problem nvarchar(300) = NULL;
DECLARE @col sysname = NULL;

DECLARE @expected TABLE (name sysname, data_type sysname, char_len int NULL, nullable varchar(3), collation sysname NULL);
INSERT INTO @expected (name, data_type, char_len, nullable, collation) VALUES
    ('id',                'bigint',   NULL, 'NO',  NULL),
    ('source',            'varchar',  50,   'NO',  'Korean_Wansung_CI_AS'),
    ('push_code',         'varchar',  10,   'NO',  'Korean_Wansung_CI_AS'),
    ('push_sub_code',     'varchar',  10,   'NO',  'Korean_Wansung_CI_AS'),
    ('office_code',       'varchar',  50,   'NO',  'Korean_Wansung_CI_AS'),
    ('sender_emp_seq_no', 'varchar',  50,   'NO',  'Korean_Wansung_CI_AS'),
    ('sender_member_id',  'varchar',  100,  'NO',  'Korean_Wansung_CI_AS'),
    ('recipients',        'varchar',  -1,   'NO',  'Korean_Wansung_CI_AS'),
    ('recipient_count',   'int',      NULL, 'NO',  NULL),
    ('message',           'nvarchar', 1000, 'NO',  'Korean_Wansung_CI_AS'),
    ('org_message',       'nvarchar', 2000, 'NO',  'Korean_Wansung_CI_AS'),
    ('link_url',          'nvarchar', 500,  'NO',  'Korean_Wansung_CI_AS'),
    ('link_code',         'nvarchar', 100,  'NO',  'Korean_Wansung_CI_AS'),
    ('guard',             'varchar',  200,  'YES', 'Korean_Wansung_CI_AS'),
    ('status',            'nvarchar', 20,   'NO',  'Korean_Wansung_CI_AS'),
    ('attempts',          'int',      NULL, 'NO',  NULL),
    ('next_attempt_at',   'datetime', NULL, 'NO',  NULL),
    ('claimed_at',        'datetime', NULL, 'YES', NULL),
    ('master_idx',        'bigint',   NULL, 'YES', NULL),
    ('device_count',      'int',      NULL, 'YES', NULL),
    ('last_error',        'nvarchar', 1000, 'YES', 'Korean_Wansung_CI_AS'),
    ('created_at',        'datetime', NULL, 'NO',  NULL),
    ('sent_at',           'datetime', NULL, 'YES', NULL);

DECLARE @actual TABLE (name sysname, data_type sysname, char_len int NULL, nullable varchar(3), collation sysname NULL);
INSERT INTO @actual (name, data_type, char_len, nullable, collation)
SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLLATION_NAME
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'push_outbox';

-- ★ 방향마다 따로 감싼다. EXCEPT·UNION 은 왼쪽부터 묶여서 한 줄로 이으면 ((기대−실제)∪실제)−기대 가
--   되어 **빠진 컬럼을 놓친다**.
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
DECLARE @ix int, @ix_type tinyint, @ix_disabled bit, @ix_filter bit, @ix_unique bit;
SELECT @ix = index_id, @ix_type = type, @ix_disabled = is_disabled, @ix_filter = has_filter, @ix_unique = is_unique
  FROM sys.indexes WHERE object_id = @oid AND name = 'IX_push_outbox_due';

IF @col IS NOT NULL
    SET @problem = N'컬럼 정의가 다름(형·길이·NULL·collation·있고 없음): ' + @col;
ELSE IF ISNULL(COLUMNPROPERTY(@oid, 'id', 'IsIdentity'), 0) <> 1
    SET @problem = N'id 가 IDENTITY 가 아님';
ELSE IF @pk IS NULL
     OR (SELECT type FROM sys.indexes WHERE object_id = @oid AND index_id = @pk) <> 1
     OR (SELECT is_disabled FROM sys.indexes WHERE object_id = @oid AND index_id = @pk) = 1   -- 비활성 클러스터형 = 테이블 사용 불가
     OR (SELECT COUNT(*) FROM sys.index_columns WHERE object_id = @oid AND index_id = @pk AND is_included_column = 0) <> 1
     OR NOT EXISTS (SELECT 1 FROM sys.index_columns
                     WHERE object_id = @oid AND index_id = @pk AND key_ordinal = 1 AND COL_NAME(@oid, column_id) = 'id')
    SET @problem = N'PK 가 id 하나의 활성 클러스터형이 아님';
ELSE IF @ix IS NULL
    SET @problem = N'IX_push_outbox_due 없음';
ELSE IF @ix_type <> 2 OR @ix_disabled = 1 OR @ix_filter = 1 OR @ix_unique = 1
    SET @problem = N'IX_push_outbox_due 가 비클러스터형·활성·필터 없음·unique 아님 이 아님';
ELSE IF (SELECT COUNT(*) FROM sys.index_columns WHERE object_id = @oid AND index_id = @ix AND is_included_column = 0) <> 2
     OR (SELECT COUNT(*) FROM sys.index_columns
          WHERE object_id = @oid AND index_id = @ix AND is_included_column = 0 AND is_descending_key = 0
            AND ((key_ordinal = 1 AND COL_NAME(@oid, column_id) = 'status')
              OR (key_ordinal = 2 AND COL_NAME(@oid, column_id) = 'next_attempt_at'))) <> 2
    SET @problem = N'IX_push_outbox_due 키가 정확히 (status, next_attempt_at) 오름차순이 아님';
ELSE IF (SELECT COUNT(*) FROM sys.index_columns WHERE object_id = @oid AND index_id = @ix AND is_included_column = 1) <> 2
     OR (SELECT COUNT(*) FROM sys.index_columns
          WHERE object_id = @oid AND index_id = @ix AND is_included_column = 1
            AND COL_NAME(@oid, column_id) IN ('created_at', 'claimed_at')) <> 2
    SET @problem = N'IX_push_outbox_due INCLUDE 가 정확히 (created_at, claimed_at) 이 아님';

IF @problem IS NOT NULL
BEGIN
    DECLARE @msg nvarchar(500) = N'push_outbox 스키마가 기대와 다릅니다: ' + @problem
        + N' — 비어 있으면 DROP TABLE dbo.push_outbox 후 다시 실행.';
    THROW 50001, @msg, 1;
END;
