/* Migration 2026-10-29 — 팀별 일자별 원티드 신청 한도(wanted_team_limit)
 *
 * 목적
 *   병원 요청: 팀마다 날짜별로 휴가·휴무 계열 원티드 신청 인원을 제한한다.
 *   한 달 일괄(팀 기본값, target_date NULL) + 특정 날짜 예외(target_date 지정, 기본값보다 우선).
 *   행이 없으면 제한 없음. 병동 전체 일자별 제한(wanted_config)과 함께 적용한다.
 *
 * ★ wanted_config 를 넓히지 않은 이유 — 그 저장(POST /wanted/config)은 한 달 통째 교체라
 *   팀 행을 섞으면 기존 화면이 저장할 때 팀 설정이 지워진다.
 *
 * ★ 접속한 DB 에 그대로 적용된다. USE 를 쓰지 않는다 — 하드코딩하면 dev 에 넣으려 해도
 *   운영으로 간다. 실행 대상은 연결/-d 로 지정할 것.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *
 * ★ 코드 배포보다 먼저 적용한다. 없으면 팀 한도 설정 API 는 실패하고, 제출 검사·조회는
 *   팀 한도를 건너뛴다(병동 전체 한도만 검사 — 저장소 준비 확인 후 경고 로그).
 *
 * ★ collation: group_id 는 groups.group_id 와 같은 varchar(50) Korean_Wansung_CI_AS 로 명시한다([MSSQL-09]).
 *   DB 기본은 SQL_Latin1 이라 생략하면 groups 와 조인하는 순간 오류 468 이 난다.
 *
 * ★ 고유키에 target_date(NULL 허용)가 들어간다. SQL Server 의 UNIQUE 제약은 NULL 을 같은 값으로
 *   보므로 (병동·월·팀) 당 기본값 행은 하나만 들어간다 — 의도한 동작이다.
 *
 * 적용 이력: eun_roster_dev 2026-10-29 14:35(재실행 검증 통과 · 행 0) / eun_roster 미적용(승인 뒤 코드 배포 전)
 */

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'wanted_team_limit'
)
BEGIN
    CREATE TABLE dbo.wanted_team_limit (
        id            int IDENTITY(1,1) NOT NULL,
        group_id      varchar(50)  COLLATE Korean_Wansung_CI_AS NOT NULL,
        year          smallint     NOT NULL,
        month         tinyint      NOT NULL,
        team_id       int          NOT NULL,   -- teams.team_id (병동 안 로컬 식별자)
        target_date   date         NULL,       -- NULL = 그 달 팀 기본값
        max_requests  int          NOT NULL,   -- 0 = 그 팀 그날 신청 불가
        created_at    datetime     NOT NULL CONSTRAINT DF_wanted_team_limit_created_at DEFAULT (GETDATE()),
        updated_at    datetime     NOT NULL CONSTRAINT DF_wanted_team_limit_updated_at DEFAULT (GETDATE()),

        CONSTRAINT PK_wanted_team_limit PRIMARY KEY CLUSTERED (id),
        CONSTRAINT ux_wanted_team_limit UNIQUE (group_id, year, month, team_id, target_date),
        CONSTRAINT CK_wanted_team_limit_max CHECK (max_requests >= 0),
        CONSTRAINT CK_wanted_team_limit_month CHECK (month BETWEEN 1 AND 12)
    );
END;

/* ★ 이미 있던 테이블이 지금 정의와 같은지 확인한다. 다르면 고쳐 맞추지 않고 멈춘다
 *   (이름만 보고 건너뛰면 예전 모양이 조용히 남는다). 멈췄다면: 비어 있으면
 *   DROP TABLE dbo.wanted_team_limit 후 다시 실행. */
IF (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'wanted_team_limit'
       AND (
            (COLUMN_NAME = 'id'           AND DATA_TYPE = 'int'      AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'group_id'     AND DATA_TYPE = 'varchar'  AND CHARACTER_MAXIMUM_LENGTH = 50
                                           AND IS_NULLABLE = 'NO' AND COLLATION_NAME = 'Korean_Wansung_CI_AS')
         OR (COLUMN_NAME = 'year'         AND DATA_TYPE = 'smallint' AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'month'        AND DATA_TYPE = 'tinyint'  AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'team_id'      AND DATA_TYPE = 'int'      AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'target_date'  AND DATA_TYPE = 'date'     AND IS_NULLABLE = 'YES')
         OR (COLUMN_NAME = 'max_requests' AND DATA_TYPE = 'int'      AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'created_at'   AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO')
         OR (COLUMN_NAME = 'updated_at'   AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO')
       )
) <> 9
OR (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'wanted_team_limit'
) <> 9
OR NOT EXISTS (
    SELECT 1 FROM sys.indexes
     WHERE object_id = OBJECT_ID('dbo.wanted_team_limit') AND name = 'ux_wanted_team_limit' AND is_unique = 1
)
BEGIN
    RAISERROR('wanted_team_limit 이 이 파일의 정의와 다르다 — 수동 확인 필요(빈 테이블이면 DROP 후 재실행).', 16, 1);
END;
