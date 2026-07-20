/* Migration 2026-07-20 — roster_config.max_conseq_off 컬럼 (MSSQL).
 *
 * 추가 컬럼:
 *   max_conseq_off  INT  NULL  — 연속 OFF 최대 개수(soft 상한). NULL=앱 기본 3 적용.
 *     설정(k) 시 (k+1)연속 OFF 마다 고weight 벌점 → hard처럼 억제하되, OFF 과잉/하드
 *     제약 충돌 시 양보(soft라 절대 infeasible 유발 안 함). 솔버 dataclass 동명 필드로 매핑.
 *     ※ 기존 enforce_4o_hard(window=4 고정, 진짜 hard, 월경계 포함)와는 별개 메커니즘.
 *
 * 멱등: 이미 있으면 skip. 기존 row 는 NULL 유지(읽을 때 앱이 NULL→3 해석, 무회귀).
 * 적용 전 DB 백업 필수.
 */

SET XACT_ABORT ON;
BEGIN TRAN;

IF COL_LENGTH('dbo.roster_config', 'max_conseq_off') IS NULL
    ALTER TABLE dbo.roster_config ADD max_conseq_off INT NULL;

COMMIT TRAN;
GO
