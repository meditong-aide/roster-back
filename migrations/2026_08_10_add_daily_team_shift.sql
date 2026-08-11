-- =============================================================================
-- Migration 2026-08-10 — daily_team_shift (일자별 가동 팀 + 팀별 인원) (MSSQL)
-- =============================================================================
-- 배경:
--   지금은 팀별 최소 커버리지가 teams.min_shift 하나뿐이라 **월 전체 고정**이다.
--   "주중엔 4개 팀, 주말엔 4·3·2팀만" 처럼 날짜마다 도는 팀이 달라지는 병동을
--   표현할 수 없다.
--
--   기존 add_team_min_constraints 는 (일, 시프트)마다
--       target = min(그날 필요 인원, 팀 수)
--   개의 '서로 다른 팀'이 각자 몫을 채우게 한다. 즉 **팀 수는 이미 일자별로
--   줄어들지만 어느 팀인지는 솔버가 고른다.** 이 테이블이 그 선택을 사람이
--   지정하게 만든다.
--
-- 동작 규칙 (읽는 쪽 계약 — 이 세 줄이 전부다):
--   1) 어떤 날짜에 행이 **하나라도 있으면** → 그 행들의 team_id 만 그날 가동.
--      나머지 팀 인원은 그날 강제 OFF(structural_off_cells 합류).
--   2) 행이 **하나도 없으면** → 미설정으로 보고 **전 팀 가동**(현행 유지).
--      ★ 이 규칙이 없으면 기존 그룹 전부가 매일 전원 OFF 가 된다. 반드시 지킬 것.
--      ★ 따라서 "그날 아무 팀도 안 돔"은 이 구조로 표현할 수 없다(행 0개가 곧
--        미설정). 저장 API 가 빈 목록을 거부한다. 그날 병동을 비우려면
--        daily_shift 의 요구 인원을 0 으로 두면 된다.
--   3) d/e/n/m_count 가 0 이면 그 시프트는 '인원 미지정' → 팀별 강제 없이
--      기존 min(need, 가동팀수) 규칙을 그대로 탄다. 양수면 그 팀이 그날 그
--      시프트에 최소 그만큼 서야 한다.
--
--   use_mid=False 인 그룹은 m_count 를 무시한다(shift_types 에 M 이 없으므로
--   읽는 쪽에서 걸러진다).
--
-- ⚠️ 기존 period 테이블들과 같은 방침으로 FK 제약·인덱스는 두지 않는다.
--    (PK 는 조회 키이자 upsert 키라 유지)
-- 멱등: IF NOT EXISTS 가드. 적용 전 DB 백업 권장.
-- 미적용 시 동작: 행이 없는 것과 같으므로 규칙 2)에 따라 **현행 그대로** 돈다.
--    즉 이 마이그레이션 없이 코드만 배포돼도 회귀가 없다.
-- =============================================================================

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'daily_team_shift')
    CREATE TABLE daily_team_shift (
        office_id   VARCHAR(50)  NOT NULL,
        group_id    VARCHAR(50)  NOT NULL,
        year        SMALLINT     NOT NULL,
        month       TINYINT      NOT NULL,
        day         TINYINT      NOT NULL,
        team_id     INT          NOT NULL,
        -- 팀별 그날 최소 인원. 0 = 미지정(팀 수 규칙에 위임).
        d_count     SMALLINT     NOT NULL DEFAULT 0,
        e_count     SMALLINT     NOT NULL DEFAULT 0,
        n_count     SMALLINT     NOT NULL DEFAULT 0,
        m_count     SMALLINT     NOT NULL DEFAULT 0,
        created_at  DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        updated_at  DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT pk_daily_team_shift
            PRIMARY KEY (office_id, group_id, year, month, day, team_id)
    );

COMMIT TRANSACTION;

-- 검증: 테이블 1 + 컬럼 12 기대
SELECT
    (SELECT COUNT(*) FROM sys.tables
       WHERE name = 'daily_team_shift')                          AS created_table,
    (SELECT COUNT(*) FROM sys.columns
       WHERE object_id = OBJECT_ID('dbo.daily_team_shift'))      AS column_count;
