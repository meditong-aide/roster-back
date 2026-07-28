-- ============================================================================
-- 확정 근무표 import 롤백
--
-- import_finalized_roster.py --apply 가 출력한 schedule_id 를 넣고 실행한다.
-- 삭제 순서: snapshot → issued_roster → schedule_entries → schedules
--            (FK 참조 방향 역순)
--
-- ★ 주의
--   - status='issued' 로 넣었다면, import 시 같은 연월의 기존 issued 를 draft 로
--     내렸다. 그 원복은 자동이 아니므로 아래 §3 에서 수동으로 되돌린다.
--   - shifts 신규 6종은 여기서 지우지 않는다(다른 근무표가 참조할 수 있음).
--     제거하려면 ward_shifts_seed.sql 하단의 DELETE 를 쓴다.
--
-- 실행 전 대상 DB 확인:  USE eun_roster;  /  USE eun_roster_dev;
-- ============================================================================

SET NOCOUNT ON;

DECLARE @schedule_id CHAR(12) = '';   -- ← 여기에 import 가 출력한 schedule_id

IF @schedule_id = ''
BEGIN
    RAISERROR('schedule_id 를 지정하세요.', 16, 1);
    RETURN;
END

-- §0 삭제 대상 확인 -----------------------------------------------------------
SELECT s.schedule_id, s.group_id, s.year, s.month, s.version, s.status, s.name,
       (SELECT COUNT(*) FROM dbo.schedule_entries e WHERE e.schedule_id = s.schedule_id) AS entries,
       (SELECT COUNT(*) FROM dbo.issued_roster i WHERE i.schedule_id = s.schedule_id) AS issued_rows,
       (SELECT COUNT(*) FROM dbo.issued_roster_snapshot n WHERE n.schedule_id = s.schedule_id) AS snapshots
FROM dbo.schedules s
WHERE s.schedule_id = @schedule_id;

-- §1 삭제 --------------------------------------------------------------------
BEGIN TRAN;

DELETE FROM dbo.issued_roster_snapshot WHERE schedule_id = @schedule_id;
PRINT CONCAT(N'snapshot 삭제: ', @@ROWCOUNT);

DELETE FROM dbo.issued_roster        WHERE schedule_id = @schedule_id;
PRINT CONCAT(N'issued_roster 삭제: ', @@ROWCOUNT);

DELETE FROM dbo.schedule_entries     WHERE schedule_id = @schedule_id;
PRINT CONCAT(N'schedule_entries 삭제: ', @@ROWCOUNT);

DELETE FROM dbo.schedules            WHERE schedule_id = @schedule_id;
PRINT CONCAT(N'schedules 삭제: ', @@ROWCOUNT);

-- 확인 후 COMMIT / 문제가 있으면 ROLLBACK
COMMIT TRAN;
-- ROLLBACK TRAN;

-- §2 남은 근무표 확인 ---------------------------------------------------------
-- SELECT schedule_id, year, month, version, status, dropped, name
-- FROM dbo.schedules WHERE group_id = '1022438ea001' ORDER BY year, month, version;

-- §3 (필요 시) import 가 draft 로 내린 기존 issued 원복 -------------------------
-- import 직전 스냅샷을 떠 두지 않았다면, 어떤 row 가 원래 issued 였는지는
-- issued_roster 의 is_active / issued_at 으로 추정해야 한다.
-- UPDATE dbo.schedules SET status = 'issued'
-- WHERE schedule_id = '<원래 issued 였던 schedule_id>';
