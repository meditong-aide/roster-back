/* Migration 2026-08-19 — 보건휴가 · 수면OFF 자동 부여 테이블 2종.
 *
 * 이미 dev·prod 에 적용돼 있다(2026-08 배포분). 이 파일은 **재현용 기록**이다 —
 * 이 저장소는 alembic 러너를 쓰지 않고 migrations/ 에 DDL 을 보관해 수동 실행한다.
 * 새 병원 인스턴스를 붙이거나 스키마를 복원할 때 이 파일이 기준이 된다.
 *
 * ★ 아래 정의는 2026-08-19 prod(eun_roster) INFORMATION_SCHEMA · sys.indexes 실측을
 *   그대로 옮긴 것이다. 설계 문서(docs/leave_auto_assignment_design.md)가 아니라
 *   **실제 스키마**를 정본으로 삼는다.
 *
 * ★ DDL 최소화 방침에 따라 FK 는 걸지 않는다(정합은 앱단 유지).
 *   PK + 조회 인덱스 + 유일성 제약만 둔다.
 *
 * 실행:
 *   sqlcmd -S <host> -d eun_roster -i migrations/2026_08_19_add_leave_auto_assignment_tables.sql
 */

/* ─────────────────────────────────────────────────────────────
 * ① nurse_leave_period — 휴가 대상 3-state (간호사귀속 시점 구간)
 *
 * NULL = 자동판정 / 1 = 강제포함 / 0 = 제외.
 * ★ 행이 0개면 도입 전 동작 그대로다. 백필하지 않는다 — 예외만 행을 만든다.
 * ★ 판정은 월 단위이고, 한 달에 0 과 1 이 함께 걸리면 0(제외)이 이긴다.
 *   (services/leave/leave_eligibility.py `_fold`)
 * ───────────────────────────────────────────────────────────── */
IF OBJECT_ID('dbo.nurse_leave_period', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.nurse_leave_period (
        id                     INT           IDENTITY(1,1) NOT NULL,
        nurse_id               VARCHAR(50)   NOT NULL,
        valid_from             DATE          NOT NULL,
        valid_to               DATE          NULL,          -- NULL = 열린(계속) 구간
        health_leave_eligible  BIT           NULL,          -- 3-state
        sleep_off_eligible     BIT           NULL,          -- 3-state
        pregnant               BIT           NULL,          -- 3-state · 자동판정 소스 없음
        source                 VARCHAR(20)   NOT NULL CONSTRAINT df_nlp_source DEFAULT ('edited'),
        note                   TEXT          NULL,
        created_at             DATETIME      NOT NULL CONSTRAINT df_nlp_created DEFAULT (GETDATE()),
        updated_at             DATETIME      NOT NULL CONSTRAINT df_nlp_updated DEFAULT (GETDATE()),
        CONSTRAINT pk_nurse_leave_period PRIMARY KEY CLUSTERED (id)
    );

    CREATE NONCLUSTERED INDEX ix_nurse_leave_period_lookup
        ON dbo.nurse_leave_period (nurse_id, valid_from, valid_to);
END;

/* ─────────────────────────────────────────────────────────────
 * ② nurse_night_cycle — 수면OFF 판정용 N 연번 앵커 (월별 스냅샷)
 *
 * ★ DB 의 schedule_entries 에는 'N' 만 저장된다. N1~N15 연번은 엑셀에만 있으므로
 *   마감 시점에 이 표로 연번 상태를 이어붙인다(services/leave/night_cycle_service.py).
 * ★ 수면OFF 기능이 꺼진 그룹에서도 앵커는 남긴다 — 연번은 기능과 무관하게 이어져야
 *   하고, 나중에 켤 때 과거 앵커가 없으면 판정이 불가능하다.
 * ★ (nurse_id, group_id, year, month) 유일 — 같은 달을 두 번 전진시키면 seq/pending
 *   이 부풀어 수면OFF 부여가 어긋난다.
 * ───────────────────────────────────────────────────────────── */
IF OBJECT_ID('dbo.nurse_night_cycle', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.nurse_night_cycle (
        id               INT          IDENTITY(1,1) NOT NULL,
        nurse_id         VARCHAR(50)  NOT NULL,
        group_id         VARCHAR(50)  NOT NULL,
        year             SMALLINT     NOT NULL,
        month            TINYINT      NOT NULL,
        seq_at_end       INT          NULL,   -- 그 달 마지막 N 의 연번 = 다음 달 시작점
        pending_sleep    INT          NULL,   -- 이월된 미부여 수면OFF 수
        sleep_off_count  INT          NULL,   -- 그 달 부여 횟수 (보통 0 또는 1)
        sleep_off_seq    INT          NULL,   -- 월말 누적 회차 = 전월 seq + count
        created_at       DATETIME     NOT NULL CONSTRAINT df_nnc_created DEFAULT (GETDATE()),
        updated_at       DATETIME     NOT NULL CONSTRAINT df_nnc_updated DEFAULT (GETDATE()),
        CONSTRAINT pk_nurse_night_cycle PRIMARY KEY CLUSTERED (id)
    );

    CREATE UNIQUE NONCLUSTERED INDEX ux_nurse_night_cycle_scope
        ON dbo.nurse_night_cycle (nurse_id, group_id, year, month);
END;

/* 검증 — 두 테이블과 인덱스가 만들어졌는지 */
SELECT t.name AS tbl, i.name AS idx, i.is_unique, i.is_primary_key
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
WHERE t.name IN ('nurse_leave_period', 'nurse_night_cycle') AND i.type > 0
ORDER BY t.name, i.name;
