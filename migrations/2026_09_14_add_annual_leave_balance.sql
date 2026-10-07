-- 연차 잔여 장부
--   ① shifts.annual_leave_unit          — 어떤 근무코드가 연차를 **얼마나** 차감하는가
--   ② nurse_annual_leave_balance        — 간호사 × 월 잔여 스냅샷
--
-- 배경(2026-09-14, 성남시의료원 중환자실): 병원이 쓰는 근무표 시트에 이미
--   `전월잔여 / 사용 / 당월잔여` 3열이 있다. 검산 결과 `당월잔여 = 전월잔여 − 그 달 연차 셀 수`
--   가 20명 전원에서 성립했다. 이걸 제품이 계산해 엑셀에 실어 준다.
--
-- ★ 배포 순서 — **이 마이그레이션이 코드보다 먼저 전 환경에 들어가야 한다.**
--   `shifts.annual_leave_unit` 은 SQLAlchemy 매핑 컬럼이라 `db.query(Shift)` 가 전부
--   SELECT 목록에 넣는다. 컬럼이 없는 환경에 코드가 먼저 가면 **Shift 조회 전체가**
--   Invalid column name 으로 죽는다. `rebuild_leave_balance_from` 을 감싼 try/except 는
--   못 막는다 — 거기 닿기 전에 죽는 경로가 있다.
--   (2026-09-11 dev 가 prod DDL 을 놓쳤던 이력이 있어 특히 주의)
--
-- ★★ 왜 불리언 표식이 아니라 **값**인가
--   같은 파일의 `health_leave_target`·`sleep_off_target` 은 "그룹당 하나를 지목" 하는
--   불리언인데, 연차는 **얼마를 차감하는지**가 코드마다 다르다. 마스터에
--   `VYH 반연차+HALF`(6개 그룹)·`V2/V3 오후반차`·`VA1 당직휴가(반)` 가 실재한다.
--   담을 자리가 없다 — `duration` 은 전사 1712행 전부 NULL 이고 `allday` 도
--   휴가 계열에서 0 고정이다.
--
-- ★★ `type='휴가'` 로 가르면 안 된다 (실증)
--   전사 98종·한 그룹 최대 38개이고 출산휴가·병가·교육·육아기단축근로까지 섞인다.
--   중환자실만 봐도 `연`·`특별`·`분만` 셋이 `휴가` 다. 정지효는 9월잔여 0 인데 10월에
--   `특별` 1건이 있어, `휴가` 로 세면 잔여가 **-1** 이 된다.
--
-- ★ 그룹웨어 정렬 — **연동은 아직 하지 않는다** (2026-09-14 방침)
--   `eun_gw.bizwiz20db.vacation_tbl (OfficeCode char(6), EmpSeqNo char(6), vca_year char(4))`
--   에 `vca_countall / vca_countspend / vca_countremain DECIMAL(6,3)` 이 이미 있고
--   `vacation_sync_log` 가 델타를 받는 구조다. 나중에 붙일 때 재설계가 없도록
--   **정밀도(6,3)와 키 모양만** 맞춘다. 성립해야 하는 롤업 등식:
--       SUM(used) over 그 해   ==  vca_countspend
--       그 해 마지막 달 closing ==  vca_countremain
--   `gw_synced_at` 은 **예약 컬럼** — 지금은 아무도 읽지도 쓰지도 않는다.
--   ★ 연동 시 함정: 그쪽 키가 `char(6)` 고정폭이라 공백 패딩된다. SQL 조인은 collation 이
--     맞춰 주지만 파이썬 딕셔너리 조회는 조용히 빗나간다.
--
-- ★ COLLATE 를 명시하는 이유 — `eun_roster` 의 DB 기본은 SQL_Latin1_General_CP1_CI_AS 인데
--   기존 컬럼은 Korean_Wansung_CI_AS 다. 생략하면 나중에 `nurses`·`schedules` 와 조인할 때
--   오류 468(Cannot resolve the collation conflict)이 난다. 2026-09-11 에 실제로 밟았다.

-- ★ 대상 DB 를 여기서 고르지 않는다. 실행기가 붙은 DB 에 적용된다.
--   (저장소의 다른 마이그레이션에도 USE 가 없다. 박아 두면 dev 커넥션으로 돌려도
--    운영에 적용돼, dev 에는 스키마가 안 생긴 채 운영만 바뀐다.)
--   dev(eun_roster_dev) · 운영(eun_roster) 양쪽에 각각 돌린다.

-- ① 연차 차감 단위.
--   NULL = 차감 대상 아님(기본 — 기존 전 행이 그대로 이 값이다)
--   1.000 종일 · 0.500 반차 · 0.250 반반차
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'shifts'
       AND COLUMN_NAME = 'annual_leave_unit'
)
    ALTER TABLE dbo.shifts ADD annual_leave_unit DECIMAL(6,3) NULL;

-- ② 월별 잔여 스냅샷.
--   `used`/`closing` 은 **마감(issued) 근무표 기준**으로만 채운다. 미마감이면 NULL 이고,
--   그건 "다음 달 시작 잔여를 아직 모른다" 는 뜻이다.
--   ★ 엑셀에 보이는 '사용' 은 이 컬럼이 아니라 **내려받는 그 근무표에서 실시간 계산**한다
--     (draft 를 받아도 "이대로 마감하면 잔여가 얼마" 가 보여야 하기 때문).
--   ★ 행이 없는 간호사는 집계하지 않는다 — 전월 기록이 없으면 차감할 근거가 없다.
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'nurse_annual_leave_balance'
)
BEGIN
    CREATE TABLE dbo.nurse_annual_leave_balance (
        id        INT IDENTITY(1,1) NOT NULL,
        office_id VARCHAR(50) COLLATE Korean_Wansung_CI_AS NOT NULL,  -- = vacation_tbl.OfficeCode
        group_id  VARCHAR(50) COLLATE Korean_Wansung_CI_AS NOT NULL,  -- 그룹웨어엔 없는 우리 스코프
        nurse_id  VARCHAR(50) COLLATE Korean_Wansung_CI_AS NOT NULL,  -- = vacation_tbl.EmpSeqNo
        year      SMALLINT NOT NULL,                                  -- = vacation_tbl.vca_year
        month     TINYINT  NOT NULL,

        opening   DECIMAL(6,3) NOT NULL,   -- 그 달 시작 잔여 (전월 closing 승계 · 최초는 시드)
        used      DECIMAL(6,3) NULL,       -- 마감본의 annual_leave_unit 합. 미마감이면 NULL
        closing   DECIMAL(6,3) NULL,       -- opening - used

        source_schedule_id VARCHAR(50)  COLLATE Korean_Wansung_CI_AS NULL,
        -- `opening` 의 **출처**: 'seed'(사람이 넣은 최초값) | 'carried'(전월 closing 승계).
        --   ★ 마감 여부를 여기 담으면 안 된다 — 재발행 때 seed 행이 덮여 앵커 자격을
        --     잃고 그 달 장부가 지워진다. 마감 여부는 source_schedule_id 로 안다.
        source    VARCHAR(20)  COLLATE Korean_Wansung_CI_AS NULL,
        gw_synced_at DATETIME NULL,                                   -- 예약 — 미사용
        note      VARCHAR(200) COLLATE Korean_Wansung_CI_AS NULL,
        created_at DATETIME NULL,
        updated_at DATETIME NULL,
        CONSTRAINT PK_nurse_annual_leave_balance PRIMARY KEY (id)
    );
END

-- 셀 유일성. `night_cycle` 과 같은 (스코프 × 연월) 규약.
IF NOT EXISTS (SELECT 1 FROM sys.indexes
                WHERE object_id = OBJECT_ID('dbo.nurse_annual_leave_balance')
                  AND name = 'UX_nalb_cell')
BEGIN
    CREATE UNIQUE NONCLUSTERED INDEX UX_nalb_cell
        ON dbo.nurse_annual_leave_balance (group_id, nurse_id, year, month);
    PRINT '[added] UX_nalb_cell';
END
ELSE
    PRINT '[skip] UX_nalb_cell 이미 존재';

-- 그룹웨어 롤업 대조용 — (office, nurse, year) 로 한 해를 훑는다. 연동 시 쓴다.
IF NOT EXISTS (SELECT 1 FROM sys.indexes
                WHERE object_id = OBJECT_ID('dbo.nurse_annual_leave_balance')
                  AND name = 'IX_nalb_gw')
BEGIN
    CREATE NONCLUSTERED INDEX IX_nalb_gw
        ON dbo.nurse_annual_leave_balance (office_id, nurse_id, year)
        INCLUDE (month, used, closing);
    PRINT '[added] IX_nalb_gw';
END
ELSE
    PRINT '[skip] IX_nalb_gw 이미 존재';

-- 확인
SELECT DB_NAME() AS applied_to, COLUMN_NAME, DATA_TYPE,
       NUMERIC_PRECISION, NUMERIC_SCALE, COLLATION_NAME
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo'
   AND (TABLE_NAME = 'nurse_annual_leave_balance'
        OR (TABLE_NAME = 'shifts' AND COLUMN_NAME = 'annual_leave_unit'))
 ORDER BY TABLE_NAME, ORDINAL_POSITION;

-- ★ 적용 이력: 2026-09-14 eun_roster_dev · eun_roster 양쪽 적용 완료.
--   같은 날 성남시의료원 중환자실 시드도 함께 넣었다(연차 코드 `연` = 1.000,
--   24명 opening — 합계 105). 시드는 병동별 1회성이라 이 파일에 넣지 않는다.
