-- =============================================================================
-- Migration 2026-06-30 — nurse_preceptee_period (프리셉터↔프리셉티 시점 SSOT)
-- Target dialect: MSSQL (Azure SQL / SQL Server)
-- 설계        : docs/NURSE_PRECEPTEE_PERIOD_DESIGN.md
-- =============================================================================
-- 신규 1 테이블 CREATE + 기존 active 프리셉티 백필. 멱등성: IF NOT EXISTS 가드.
--
-- 시점 규칙: [valid_from, valid_to) 반열림(valid_to 배타), null=계속(무기한).
--   진실=이 테이블, nurses.preceptor_id=단방향 투영.
-- 경계: assignment.expected_end_date 는 포함(inclusive) → valid_to = +1day(배타) 로 변환.
--   → 종료월 다음달 as-of 에서 자연 제외(프리셉티 follow 누수 버그 구조적 방지).
--
-- 적용 절차: 1) DB 백업  2) 본 스크립트 실행  3) 검증 쿼리(끝) 확인.
-- =============================================================================

SET XACT_ABORT ON;
BEGIN TRANSACTION;

-- ── 1) 테이블 생성 ────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'nurse_preceptee_period')
BEGIN
    CREATE TABLE nurse_preceptee_period (
        id                   INT IDENTITY(1,1) PRIMARY KEY,
        nurse_id             VARCHAR(50)  NOT NULL,   -- 프리셉티 본인
        preceptor_id         VARCHAR(50)  NOT NULL,   -- WHO (따라갈 대상)
        office_id            VARCHAR(50)  NOT NULL,
        valid_from           DATE         NOT NULL,
        valid_to             DATE         NULL,        -- null = 무기한
        end_reason           VARCHAR(30)  NULL,        -- expired|cancelled|released|preceptor_transfer
        source               VARCHAR(20)  NOT NULL DEFAULT 'edited',
        source_assignment_id INT          NULL,
        note                 TEXT         NULL,
        created_at           DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        updated_at           DATETIME     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_npp_nurse     FOREIGN KEY (nurse_id)     REFERENCES nurses(nurse_id),
        CONSTRAINT fk_npp_preceptor FOREIGN KEY (preceptor_id) REFERENCES nurses(nurse_id),
        CONSTRAINT fk_npp_office    FOREIGN KEY (office_id)     REFERENCES offices(office_id)
    );
    CREATE INDEX ix_npp_nurse     ON nurse_preceptee_period (nurse_id, valid_from);
    CREATE INDEX ix_npp_preceptor ON nurse_preceptee_period (preceptor_id, valid_to);
    -- 1:1: 간호사당 동시점 open(valid_to NULL) 구간 1개 (filtered unique).
    CREATE UNIQUE INDEX uq_npp_open_per_nurse ON nurse_preceptee_period (nurse_id)
        WHERE valid_to IS NULL;
    PRINT '[create] nurse_preceptee_period';
END
ELSE
    PRINT '[skip] nurse_preceptee_period already exists';

-- ── 2) 백필: active 프리셉티 assignment + nurses.preceptor_id ─────────────────
--   멱등: 이미 open 구간이 있는 간호사는 제외.
--   cache-only 비대칭(preceptor_id set인데 active 프리셉티 assignment 없음)은
--   valid_from 미상이라 생성하지 않음(수동 검토 — flush/orphan-cleanup 이 캐시 정리).
--   더티 방지: 한 간호사에 active 프리셉티 assignment 가 2개면 ROW_NUMBER 로 1개만 채택
--   (set-based INSERT 는 같은 문 안에서 self-guard 불가 → CTE dedup 필요).
;WITH src AS (
    SELECT a.id AS aid, a.nurse_id, n.preceptor_id, a.office_id,
           a.start_date, a.expected_end_date,
           ROW_NUMBER() OVER (PARTITION BY a.nurse_id ORDER BY a.start_date, a.id) AS rn
    FROM nurse_assignment a
    JOIN nurses n ON n.nurse_id = a.nurse_id
    WHERE a.reason = N'프리셉티'
      AND a.status = 'active'
      AND n.preceptor_id IS NOT NULL
)
INSERT INTO nurse_preceptee_period
    (nurse_id, preceptor_id, office_id, valid_from, valid_to, source, source_assignment_id, created_at, updated_at)
SELECT src.nurse_id,
       src.preceptor_id,
       src.office_id,
       src.start_date,
       CASE WHEN src.expected_end_date IS NULL THEN NULL
            ELSE DATEADD(day, 1, src.expected_end_date) END,   -- inclusive → exclusive
       'backfill',
       src.aid,
       GETUTCDATE(), GETUTCDATE()
FROM src
WHERE src.rn = 1                                    -- 간호사당 1건만
  AND NOT EXISTS (
      SELECT 1 FROM nurse_preceptee_period p
      WHERE p.source_assignment_id = src.aid        -- 멱등: 이 assignment 로 이미 생성됨
  )
  AND NOT EXISTS (
      SELECT 1 FROM nurse_preceptee_period p2        -- 1:1: 이미 open 구간 보유 시 skip
      WHERE p2.nurse_id = src.nurse_id AND p2.valid_to IS NULL
  );
PRINT '[backfill] active 프리셉티 assignment → period';

COMMIT TRANSACTION;

-- ── 검증 ──────────────────────────────────────────────────────────────────────
SELECT COUNT(*) AS table_exists FROM sys.tables WHERE name = 'nurse_preceptee_period';
SELECT COUNT(*) AS backfilled_rows FROM nurse_preceptee_period;
-- cache-only 비대칭(백필 누락) 점검:
SELECT n.nurse_id, n.preceptor_id
FROM nurses n
WHERE n.preceptor_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM nurse_preceptee_period p
                  WHERE p.nurse_id = n.nurse_id AND p.valid_to IS NULL);
