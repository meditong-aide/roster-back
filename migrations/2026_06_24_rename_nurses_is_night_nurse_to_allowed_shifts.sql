-- =============================================================================
-- Migration 2026-06-24 — nurses.is_night_nurse → allowed_shifts (컬럼 rename)
-- ⚠️ DEFERRED — 지금 적용하지 말 것. 전 브랜치 머지 후 조정해서 1회 실행.
-- =============================================================================
-- nurses.is_night_nurse 컬럼은 실제로는 '허용 근무형 리스트'(JSON)다.
--   [] = 제한없음, ["N"] = N전담, ["D","E","N"] = 세 개 다.
-- 이름이 boolean 처럼 보여 혼란 → 코드는 allowed_shifts 로 수렴 완료.
--
-- 【현재 상태(중요)】
--   물리 컬럼명은 **is_night_nurse 그대로 유지**한다. 공유 dev DB 에서 컬럼을
--   물리 rename 하면 아직 is_night_nurse 를 참조하는 **다른 브랜치가 깨지기** 때문.
--   대신 ORM 에서만 매핑한다(app/db/models.py):
--       allowed_shifts = Column(JSON, name="is_night_nurse", ...)
--   → Python 속성 = allowed_shifts, 물리 컬럼 = is_night_nurse. 둘 다 만족.
--
-- 【이 SQL 을 실행할 시점】
--   모든 브랜치가 allowed_shifts 속성으로 수렴(= 코드에서 is_night_nurse 물리명
--   직접 참조 0)된 뒤, 이 rename 을 적용하고 models.py 의 name= 매핑을 제거한다.
--   그 전까지는 ORM 매핑으로 충분하므로 적용 불필요.
--
-- 데이터 보존: sp_rename 은 in-place 메타데이터 변경(값/타입 그대로).
-- 멱등: 원본(is_night_nurse) 존재 + 대상(allowed_shifts) 부재일 때만 rename.
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
