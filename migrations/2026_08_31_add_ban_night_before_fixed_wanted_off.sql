/* Migration 2026-08-31 — roster_config.ban_night_before_fixed_wanted_off
 *
 * 확정 원티드로 굳힌 O 직전일에 N 배치를 금지하는 설정. 병동이 켜고 끈다.
 *
 * ★★ **이 마이그레이션은 코드 배포보다 먼저 실행해야 한다.**
 *   기존 oncall/holiday 컬럼과 달리 이 컬럼은 **ORM 에 매핑돼 있다**
 *   (db/models.py RosterConfig). 컬럼이 없으면 모든 RosterConfig SELECT 가
 *   unknown-column 으로 실패한다 — 근무표 설정 화면과 생성이 통째로 멈춘다.
 *
 * ★ 접속한 DB 에 그대로 적용된다. `USE` 를 쓰지 않는다 —
 *   하드코딩하면 `sqlcmd -d eun_roster_dev` 로 dev 에 넣으려 해도 운영으로 간다.
 *   실행 대상은 **연결 문자열/`-d` 로 지정**할 것.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *   prod→dev 마이그레이션은 DDL 을 옮기지 않으므로 **양쪽에 각각** 돌린다.
 *
 * ★ NULL = 미설정(= 꺼짐). 기존 row 를 건드리지 않으려 nullable 로 둔다.
 *   기존 ban_night_before_fixed_off 는 NULL→True 규약이지만 이 설정은 **반대**다.
 *   판정이 bool(getattr(cfg, ..., False)) 라 None 이 False 로 떨어진다.
 *
 * 적용 이력: eun_roster_dev 2026-08-31 / eun_roster 미적용
 */

/* ★ 존재 검사에 TABLE_SCHEMA 를 건다. ALTER 는 dbo 로 고정인데 검사만 스키마를 안 보면,
     같은 이름의 테이블이 다른 스키마에 있을 때 "이미 있다" 로 읽고 dbo 에는 컬럼을 안 넣는다.
     그러면 ORM 이 매핑한 컬럼이 없는 채로 코드가 올라가 모든 RosterConfig SELECT 가 죽는다.
     검사 대상과 변경 대상은 반드시 같은 스키마를 가리켜야 한다. */
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo'
       AND TABLE_NAME = 'roster_config'
       AND COLUMN_NAME = 'ban_night_before_fixed_wanted_off'
)
BEGIN
    ALTER TABLE dbo.roster_config
        ADD ban_night_before_fixed_wanted_off BIT NULL;
    PRINT '[added] dbo.roster_config.ban_night_before_fixed_wanted_off';
END
ELSE
    PRINT '[skip] 이미 존재';
GO

/* 검증 — 접속 DB 기준. 위 DDL 과 같은 스키마(dbo)를 봐야 검증이 성립한다. */
SELECT DB_NAME() AS applied_to, TABLE_SCHEMA, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo'
   AND TABLE_NAME = 'roster_config'
   AND COLUMN_NAME LIKE 'ban_night%';
GO

/* 롤백
     ALTER TABLE dbo.roster_config DROP COLUMN ban_night_before_fixed_wanted_off;
   ★ ORM 매핑(db/models.py)을 **먼저** 되돌린 뒤에 할 것.
     순서가 뒤바뀌면 그 사이 모든 설정 조회가 실패한다.
*/
