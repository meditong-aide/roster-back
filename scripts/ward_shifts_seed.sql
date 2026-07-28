-- ============================================================================
-- 병동 근무코드 보강 — 확정 근무표(엑셀) import 전 선행 실행
--
-- 대상: 인천의료원 41병동-RN (office 102243 / group 1022438ea001)
-- 목적: 엑셀 확정 근무표에 존재하나 shifts 에 없는 코드 6종 등록
--       DE 65셀 · 보수교육 11 · 상가 6 · 단축 5 · 공가 1 · 특휴 1 (7·8월 합계)
--
-- 원칙
--   - 41병동-RN 기존 12종의 속성 관행을 그대로 따른다
--     (allday=0 / off_swap_target=0 / deleteless=0 / is_weekly_off=0 / default_shift=NULL)
--   - shifts.id 는 IDENTITY 이므로 INSERT 에서 값을 지정하지 않는다
--   - 멱등: 이미 있으면 INSERT 하지 않는다 (NOT EXISTS 가드)
--
-- ※ 대안: scripts/import_finalized_roster.py 에 --seed-shifts 를 주면 같은 정의로
--   자동 등록된다(41병동은 이 경로로 이미 처리됨). 이 파일은 SQL 로 직접 다룰 때 쓴다.
--
-- DE = 수간호사 고정근무
--   auto_schedule=0, show_in_preference=0 — 자동배정/원티드 선택 대상이 아님.
--   근무시간은 병원 확인 전이라 NULL. 확인되면 UPDATE 로 채우면 된다.
--   운영상으로는 nurses.fixed_shift='DE' 로 지정해 고정 배정한다(본 스크립트 범위 밖).
--
-- 실행 전 반드시 대상 DB 확인:  USE eun_roster;  (운영)  /  USE eun_roster_dev;  (개발)
-- ============================================================================

SET NOCOUNT ON;

DECLARE @office_id VARCHAR(50) = '102243';
DECLARE @group_id  VARCHAR(50) = '1022438ea001';

-- 신규 코드 정의 (다른 병동에 재사용할 때 이 테이블 값만 교체)
DECLARE @new_shifts TABLE (
    shift_id       NVARCHAR(50),
    name           NVARCHAR(50),
    type           NVARCHAR(10),
    color          VARCHAR(10),
    auto_schedule  INT,
    show_in_pref   BIT,
    seq            INT
);

INSERT INTO @new_shifts (shift_id, name, type, color, auto_schedule, show_in_pref, seq) VALUES
    (N'DE',       N'수간호사 고정근무', N'근무', '#6b5b95', 0, 0, 13),
    (N'보수교육', N'보수교육',          N'근무', '#2e8b57', 1, 1, 14),
    (N'상가',     N'상가',              N'휴가', '#808080', 1, 1, 15),
    (N'단축',     N'단축근무',          N'휴가', '#ff8c42', 1, 1, 16),
    (N'공가',     N'공가',              N'공가', '#4a90d9', 1, 1, 17),
    (N'특휴',     N'특별휴가',          N'휴가', '#c94f7c', 1, 1, 18);

-- 적용 전 확인 --------------------------------------------------------------
SELECT ns.shift_id, ns.name, ns.type,
       CASE WHEN s.shift_id IS NULL THEN N'신규 INSERT' ELSE N'이미 존재 → SKIP' END AS action
FROM @new_shifts ns
LEFT JOIN dbo.shifts s
       ON s.group_id = @group_id AND s.shift_id = ns.shift_id
ORDER BY ns.seq;

-- INSERT --------------------------------------------------------------------
DECLARE @base_id INT = (SELECT ISNULL(MAX(id), 0) FROM dbo.shifts);

INSERT INTO dbo.shifts (
    shift_id, office_id, group_id, name, color, start_time, end_time, type,
    allday, auto_schedule, duration, sequence, id, deleteless,
    default_shift, is_weekly_off, shift_gb, show_in_preference, off_swap_target, description
)
SELECT
    ns.shift_id, @office_id, @group_id, ns.name, ns.color, NULL, NULL, ns.type,
    0, ns.auto_schedule, NULL, ns.seq,
    @base_id + ROW_NUMBER() OVER (ORDER BY ns.seq),
    0, NULL, 0, NULL, ns.show_in_pref, 0, NULL
FROM @new_shifts ns
WHERE NOT EXISTS (
    SELECT 1 FROM dbo.shifts s
    WHERE s.group_id = @group_id AND s.shift_id = ns.shift_id
);

PRINT CONCAT(N'INSERT 된 근무코드 수: ', @@ROWCOUNT);

-- 적용 후 확인 --------------------------------------------------------------
SELECT shift_id, name, type, color, allday, auto_schedule, sequence, id,
       show_in_preference, off_swap_target, start_time, end_time
FROM dbo.shifts
WHERE group_id = @group_id
ORDER BY sequence;

-- ============================================================================
-- 롤백 (신규 6종만 제거 — schedule_entries 가 참조하기 전에만 안전)
-- ============================================================================
-- DELETE FROM dbo.shifts
-- WHERE group_id = '1022438ea001'
--   AND shift_id IN (N'DE', N'보수교육', N'상가', N'단축', N'공가', N'특휴');
