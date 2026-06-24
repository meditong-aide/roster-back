/* Migration 2026-06-23 — roster_config 설정 프리셋 컬럼 (MSSQL).
 *
 * 추가 컬럼:
 *   version       INT            NULL  — 그룹(office+group)별 0부터 시작하는 프리셋 버전
 *   config_name   NVARCHAR(100)  NULL  — 프리셋 이름('새로운 설정n' 자동분 포함)
 *   config_memo   NVARCHAR(500)  NULL  — 간단 메모
 *   updated_at    DATETIME       NULL  — 마지막 저장 시각(upsert 시 앱이 갱신)
 *
 * 인덱스: ux_roster_config_group_version (version 유일성) 는 개발 마무리 후 별도 추가 예정.
 *   지금은 컬럼만 추가. (추가 시 WHERE version IS NOT NULL 필터드 유니크로 넣을 것)
 *
 * 멱등: 이미 있으면 skip. 기존 row 는 version=NULL 유지(백필 없음 = legacy 는 프리셋 아님).
 * 적용 전 DB 백업 필수.
 */

SET XACT_ABORT ON;
BEGIN TRAN;

IF COL_LENGTH('dbo.roster_config', 'version') IS NULL
    ALTER TABLE dbo.roster_config ADD version INT NULL;

IF COL_LENGTH('dbo.roster_config', 'config_name') IS NULL
    ALTER TABLE dbo.roster_config ADD config_name NVARCHAR(100) NULL;

IF COL_LENGTH('dbo.roster_config', 'config_memo') IS NULL
    ALTER TABLE dbo.roster_config ADD config_memo NVARCHAR(500) NULL;

IF COL_LENGTH('dbo.roster_config', 'updated_at') IS NULL
    ALTER TABLE dbo.roster_config ADD updated_at DATETIME NULL;

COMMIT TRAN;
GO

/* NOTE: version 유일성 필터드 유니크 인덱스(ux_roster_config_group_version,
 *   WHERE version IS NOT NULL)는 개발 마무리 후 별도로 추가 예정. 지금은 컬럼만 추가. */
