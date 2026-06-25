-- =============================================================================
-- Migration 2026-06-24 — nurses.is_night_nurse → allowed_shifts (컬럼 rename)
-- =============================================================================
-- nurses.is_night_nurse 컬럼은 실제로는 '허용 근무형 리스트'(JSON)였다.
--   [] = 제한없음, ["N"] = N전담, ["D","E","N"] = 세 개 다.
-- 이름이 boolean 처럼 보여 혼란을 유발 → 의미에 맞게 allowed_shifts 로 변경하고
-- nurse_allowed_shift_period.allowed_shifts(SSOT 컬럼)와 명칭을 수렴시킨다.
--
-- 데이터 보존: sp_rename 은 in-place 메타데이터 변경(값/타입 그대로).
-- 멱등: 원본(is_night_nurse) 존재 + 대상(allowed_shifts) 부재일 때만 rename.
-- 적용 전 DB 백업 권장.
-- =============================================================================

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF COL_LENGTH('dbo.nurses', 'is_night_nurse') IS NOT NULL
   AND COL_LENGTH('dbo.nurses', 'allowed_shifts') IS NULL
    EXEC sp_rename 'dbo.nurses.is_night_nurse', 'allowed_shifts', 'COLUMN';

COMMIT TRANSACTION;

-- 검증: allowed_shifts 존재(>0) + is_night_nurse 부재(NULL)
SELECT
    COL_LENGTH('dbo.nurses', 'allowed_shifts') AS allowed_shifts_col,
    COL_LENGTH('dbo.nurses', 'is_night_nurse') AS old_is_night_nurse_col;
