/* Migration 2026-09-01 — 근무표 셀 수정 이력(schedule_entry_log) + schedule_entries 인덱스
 *
 * 목적
 *   근무표를 사람이 고칠 때마다 어떤 칸이 무엇에서 무엇으로 바뀌었는지 로우 단위로 남긴다.
 *   `schedule_entries` 는 **현재 상태만** 유지하고, 이력은 이 로그 테이블에 쌓인다.
 *   생성·재생성은 남기지 않는다(한 번에 수백 행이 바뀌어 "내가 고친 이력" 과 성격이 다르다).
 *
 * ★★ 이 파일은 **dev 최종 스키마를 그대로 재현**한다.
 *   dev 에서는 만들며 ALTER·DROP·재생성을 거쳤지만 여기서는 처음부터 최종형으로 만든다.
 *   (실제로 초판이 `is_latest` 와 UNIQUE 제약을 빠뜨려 코드와 어긋났다 — 2026-09-01 리뷰 적발)
 *
 * ★ 접속한 DB 에 그대로 적용된다. USE 를 쓰지 않는다 — 하드코딩하면 dev 에 넣으려 해도
 *   운영으로 간다. 실행 대상은 연결/-d 로 지정할 것. DBeaver 는 좌측 DB 선택을 확인할 것.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *
 * ★ 코드 배포와 순서 의존이 없다. 다만 이력 적재 코드가 배포되기 전에 [1] 은 있어야 한다.
 *
 * 적용 이력: eun_roster_dev 2026-09-01 적용 / eun_roster 미적용
 */

/* ─────────────────────────────────────────────────────────────
 * [1] 이력 테이블
 *
 * 타입은 기존 테이블과 정확히 맞췄다. 다르면 조인·비교에서 암시적 변환이 일어나
 * 인덱스를 못 타거나 collation 이 충돌한다.
 *   schedule_id varchar(50) ← schedules   ·  nurse_id varchar(50) ← schedule_entries
 *   *_shift_id  nvarchar(10) ← shifts.shift_id  ★한글 코드('주'·'경조'·'반반반')가 있어 nvarchar 필수
 *   *_color     varchar(10)  ← shifts.color     ·  *_id int ← shifts.id
 *
 * ★ 왜 id 와 코드 문자열과 색을 **모두** 남기는가
 *   - shifts 는 PK·인덱스가 없고 shift_id 코드가 실제로 교체된다.
 *     실측: 같은 shifts.id 에 서로 다른 코드가 쌓인 사례 5건 (N→N1, O→OFF, D→Dㅇ, D→DD, N→Nn)
 *   - 그렇다고 id 만으로도 부족하다. shifts.id 는 1643행 중 3건이 **다른 병동에서 중복**된다
 *     (id=1874 가 동탄시티 'OFF' 와 시화 '반반반' 양쪽에 존재).
 *   → 안정 참조(id) + 그 시점 표시값(코드·색) + 병동(group_id)을 함께 남겨야
 *     나중에 코드나 색이 바뀌어도 이력을 그대로 재현할 수 있다.
 */
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_entry_log'
)
BEGIN
    CREATE TABLE dbo.schedule_entry_log (
        log_id          bigint        IDENTITY(1,1) NOT NULL,

        -- 어느 칸인가
        schedule_id     varchar(50)   NOT NULL,
        nurse_id        varchar(50)   NOT NULL,
        work_date       date          NOT NULL,
        seq             int           NOT NULL,   -- 그 칸의 몇 번째 변경인지(1부터). 앱이 채운다

        -- 변경 전 (신규 배정이면 전부 NULL)
        before_id       int           NULL,
        before_shift_id nvarchar(10)  NULL,
        before_color    varchar(10)   NULL,

        -- 변경 후 (삭제면 전부 NULL)
        after_id        int           NULL,
        after_shift_id  nvarchar(10)  NULL,
        after_color     varchar(10)   NULL,

        -- 맥락
        group_id        varchar(50)   NULL,       -- (group_id, shifts.id) 라야 shift 가 유일
        action          varchar(10)   NOT NULL,   -- 'update' | 'insert' | 'delete'
        source          varchar(10)   NOT NULL,   -- 'manual' | 'generate'
        changed_by      varchar(50)   NULL,       -- 수정자 nurse_id
        changed_at      datetime2     NOT NULL,

        -- ★ 그 칸의 **마지막 변경 기록**인가. 칸당 1행만 1 이어야 한다(아래 UX_sel_latest 가 강제).
        --   "현재값" 과는 다르다 — 재생성되면 근무표는 바뀌지만 이 로그는 그대로다.
        is_latest       bit           NOT NULL CONSTRAINT DF_sel_is_latest DEFAULT 1,

        CONSTRAINT PK_schedule_entry_log PRIMARY KEY NONCLUSTERED (log_id)
    );
    PRINT '[added] dbo.schedule_entry_log';
END
ELSE
    PRINT '[skip] dbo.schedule_entry_log 이미 존재';
GO

/* 클러스터드는 **칸 이력 조회 기준**으로 잡는다. 조회가 항상
 * (schedule_id, nurse_id, work_date) 로 들어오므로 그 순서로 물리 정렬해야 한 칸의 이력이
 * 연속 페이지에 모인다. log_id 클러스터드면 삽입은 빠르지만 조회가 흩어진다.
 * ★ UNIQUE 인 이유 — 동시 저장 시 두 요청이 같은 MAX(seq) 를 읽어 같은 번호를 부여할 수 있다.
 *   DB 가 막아 주면 애플리케이션 잠금 없이도 이력이 어긋나지 않는다. 위반은 savepoint 안에서
 *   나므로 이력만 실패하고 근무표 저장은 살아남는다. */
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
     WHERE object_id = OBJECT_ID('dbo.schedule_entry_log') AND name = 'CIX_sel_cell'
)
BEGIN
    CREATE UNIQUE CLUSTERED INDEX CIX_sel_cell
        ON dbo.schedule_entry_log (schedule_id, nurse_id, work_date, seq);
    PRINT '[added] CIX_sel_cell';
END
ELSE
    PRINT '[skip] CIX_sel_cell';
GO

/* "이 근무표의 최근 변경 목록" 화면용 커버링 인덱스.
 * INCLUDE 로 화면에 필요한 컬럼을 다 담아 테이블 접근 없이 인덱스만 읽고 끝낸다. */
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
     WHERE object_id = OBJECT_ID('dbo.schedule_entry_log') AND name = 'IX_sel_recent'
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_sel_recent
        ON dbo.schedule_entry_log (schedule_id, changed_at DESC)
        INCLUDE (nurse_id, work_date, seq, before_shift_id, after_shift_id,
                 before_color, after_color, action, changed_by);
    PRINT '[added] IX_sel_recent';
END
ELSE
    PRINT '[skip] IX_sel_recent';
GO

/* 칸마다 마지막 변경 하나만 뽑는 필터 인덱스.
 * ★ UNIQUE 라 "칸당 is_latest 는 하나" 를 DB 가 강제한다. 필터가 걸려 있어 이력이 쌓여도
 *   이 인덱스는 칸 개수만큼만 커진다. */
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
     WHERE object_id = OBJECT_ID('dbo.schedule_entry_log') AND name = 'UX_sel_latest'
)
BEGIN
    CREATE UNIQUE NONCLUSTERED INDEX UX_sel_latest
        ON dbo.schedule_entry_log (schedule_id, nurse_id, work_date)
        INCLUDE (seq, after_shift_id, after_id, after_color, action, changed_by, changed_at)
        WHERE is_latest = 1;
    PRINT '[added] UX_sel_latest';
END
ELSE
    PRINT '[skip] UX_sel_latest';
GO

/* ─────────────────────────────────────────────────────────────
 * [2] schedule_entries 조회 인덱스
 *
 * ★★ dev 와 운영의 **현재 구조가 다르다.** 반드시 확인하고 맞는 쪽을 실행할 것.
 *      dev  : HEAP (인덱스 0개) · 1,636,957행 · 104MB
 *      운영 : CLUSTERED PK (entry_id) 이미 존재 · 556,358행 · 50MB
 *   테이블당 클러스터드는 하나뿐이라, 운영에 클러스터드를 또 만들려 하면 오류가 난다.
 *
 * 조회 패턴은 schedule_id 가 압도적이다(코드 전수: schedule_id 42회 · nurse_id 11 · work_date 3).
 * schedule 당 평균 676행이 늘 한 묶음으로 조회된다.
 */

/* [2-a] dev 용 — HEAP 을 클러스터드로 전환.
 *   ★ Standard 에디션이라 ONLINE = ON 을 못 쓴다. 생성 중 테이블이 잠긴다(수십 초).
 *     사용량이 적은 시간에 실행할 것. dev 적용 후 실측: 조회 0.40s → 0.11s (4배). */
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.schedule_entries') AND index_id = 1)
   AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.schedule_entries') AND name = 'CIX_schedule_entries_cell')
BEGIN
    PRINT '[start] CIX_schedule_entries_cell — 테이블 잠김';
    CREATE CLUSTERED INDEX CIX_schedule_entries_cell
        ON dbo.schedule_entries (schedule_id, nurse_id, work_date)
        WITH (SORT_IN_TEMPDB = ON, MAXDOP = 2);
    PRINT '[added] CIX_schedule_entries_cell (HEAP → CLUSTERED)';
END
ELSE
    PRINT '[skip] 클러스터드가 이미 있음 — 아래 [2-b] 로 간다';
GO

/* [2-b] 운영 용 — 클러스터드 PK(entry_id)가 이미 있으므로 논클러스터드 커버링 인덱스.
 *   INCLUDE 로 셀 조회에 필요한 값을 담아 키 조회(lookup) 없이 인덱스만 읽게 한다.
 *   기존 데이터를 재정렬하지 않아 [2-a] 보다 잠금이 훨씬 짧다. */
/*   ★ [2-a] 가 방금 만든 클러스터드까지 index_id = 1 로 잡히므로, 그것만으로 분기하면
 *     dev 에서 두 인덱스가 **둘 다** 생긴다(같은 배치에서 순차 실행되기 때문).
 *     [2-a] 가 만든 이름을 제외 조건에 넣어 두 분기를 배타로 만든다. */
IF EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.schedule_entries') AND index_id = 1)
   AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.schedule_entries') AND name = 'IX_schedule_entries_cell')
   AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.schedule_entries') AND name = 'CIX_schedule_entries_cell')
BEGIN
    CREATE NONCLUSTERED INDEX IX_schedule_entries_cell
        ON dbo.schedule_entries (schedule_id, nurse_id, work_date)
        INCLUDE (shift_id, id)
        WITH (SORT_IN_TEMPDB = ON, MAXDOP = 2);
    PRINT '[added] IX_schedule_entries_cell (NONCLUSTERED)';
END
ELSE
    PRINT '[skip] IX_schedule_entries_cell';
GO

/* ─────────────────────────────────────────────────────────────
 * [3] 검증
 */
SELECT DB_NAME() AS applied_to;
GO

SELECT  t.name AS table_name, i.name AS index_name, i.type_desc, i.is_unique, i.has_filter,
        STUFF((SELECT ', ' + c.name
                 FROM sys.index_columns ic JOIN sys.columns c
                   ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                  AND ic.is_included_column = 0
                ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 2, '') AS key_columns
  FROM sys.indexes i JOIN sys.tables t ON t.object_id = i.object_id
 WHERE t.name IN ('schedule_entry_log', 'schedule_entries') AND i.type > 0
 ORDER BY t.name, i.type_desc;
GO

/* 컬럼 17개가 나와야 한다(is_latest 포함). ORM(db/models.py ScheduleEntryLog)과 일치해야 하며,
 * 하나라도 빠지면 이력 적재가 savepoint 안에서 조용히 실패하고 조회 API 는 500 이 난다. */
SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLUMN_DEFAULT
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'schedule_entry_log'
 ORDER BY ORDINAL_POSITION;
GO

/* ─────────────────────────────────────────────────────────────
 * [4] 롤백
 *
 *   DROP INDEX IX_schedule_entries_cell ON dbo.schedule_entries;    -- 운영
 *   DROP INDEX CIX_schedule_entries_cell ON dbo.schedule_entries;   -- dev (HEAP 으로 복귀, 재정렬이라 잠긴다)
 *   DROP TABLE dbo.schedule_entry_log;                              -- ★ 적재된 이력이 사라진다
 *
 * ★ 이력 적재 코드가 배포된 뒤에 테이블을 DROP 하면 저장 시 오류가 난다.
 *   되돌릴 때는 **코드를 먼저** 되돌린다.
 */
