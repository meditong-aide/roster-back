-- shift_manage UNIQUE 제약 추가 (DB팀 실행용, MSSQL)
-- ★ 반드시 scripts/dedup_shift_manage.py --apply 로 중복 정리를 먼저 끝낸 뒤 실행할 것.
--   (잔여 중복이 있으면 ALTER 가 실패한다)
--
-- 유일성 키 = (office_id, group_id, nurse_class, shift_slot)  [사용자 결정: 글로벌·nurse_class 포함]
-- 참고: SQL Server 비교는 trailing-space 무시라 'RN' 과 'RN   ' 는 동일 키로 취급된다(의도된 동작).

-- 0) 사전 확인: 아래 쿼리가 0행이어야 UNIQUE 추가 가능 (남으면 dedup 먼저)
SELECT office_id, group_id, LTRIM(RTRIM(nurse_class)) AS nclass, shift_slot, COUNT(*) AS cnt
FROM dbo.shift_manage
GROUP BY office_id, group_id, LTRIM(RTRIM(nurse_class)), shift_slot
HAVING COUNT(*) > 1
ORDER BY cnt DESC;

-- 1) junk 클래스 잔여 확인(선택). dedup 스크립트가 이미 'save' 등을 삭제했어야 한다.
SELECT * FROM dbo.shift_manage
WHERE LTRIM(RTRIM(nurse_class)) NOT IN (N'RN', N'AN', N'보조');

-- 2) UNIQUE 제약 추가
ALTER TABLE dbo.shift_manage
  ADD CONSTRAINT UQ_shift_manage_slot UNIQUE (office_id, group_id, nurse_class, shift_slot);

-- 3) 사후 확인: 제약이 추가되었는지
SELECT kc.name, kc.type_desc
FROM sys.key_constraints kc
WHERE kc.parent_object_id = OBJECT_ID('dbo.shift_manage');

-- (선택) codes 컬럼은 현재 nvarchar(100). 현 데이터 최대 길이는 63 으로 여유 있으나,
-- 코드가 많은 병동을 대비해 확장하려면 아래를 실행(절단 위험 예방):
-- ALTER TABLE dbo.shift_manage ALTER COLUMN codes nvarchar(255) NOT NULL;
