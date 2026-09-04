-- 근무표 생성 시도 기록
--   ① roster_config.last_generate_status / last_generate_at  — 목록 필터
--   ② roster_config_attempt                                   — 시도별 입력 스냅샷(분석)
--
-- 실사례(2026-09-04, 남촌 중환자실1): max_nig_per_month=1 과 not_one_night=True 가
--   동시에 켜져 아무도 N 을 못 하는 상태가 됐고, 산술로 판정 가능한 모순인데도
--   precheck 를 지나 solver 까지 가 완화 탐색 159회를 돌고 실패했다.
--   그 설정이 프리셋으로 남아 목록에 계속 노출된다.
--
-- ★ 배포 순서 — **이 마이그레이션이 코드보다 먼저 전 환경에 들어가야 한다.**
--   last_generate_status / last_generate_at 은 SQLAlchemy 매핑 컬럼이라
--   db.query(RosterConfig) 가 전부 SELECT 목록에 넣는다(_fetch_latest_config ·
--   /config/versions · /config/version/{v} · save_roster_config_service ·
--   constraint_tools). 컬럼이 없는 환경에 코드가 먼저 가면 그 조회들이
--   Invalid column name 으로 죽는다. 기록 함수의 try/except 는 못 막는다 —
--   거기 닿기 전에 죽기 때문이다.
--   (dev 가 prod 마이그레이션의 DDL 을 놓친 이력이 있어 특히 주의)
--
-- ★★ 설계 이력 — 세 번 뒤집었다. 되돌리려는 사람이 같은 길을 다시 걷지 않도록 남긴다.
--   1차 — 실패 기록을 roster_config 에 **행**으로 넣었다. config_id 가 항상 최댓값이
--     되어 config_id DESC 로 "최신 config" 를 고르는 8곳이 그 기록 행을 집었다
--     (실측: 프리셋의 use_mid 를 False→True 로 바꿔도 8곳은 False 를 반환).
--   2차 — last_generate_status + last_generate_ym 컬럼으로 **월별** 판정을 시도했다.
--     컬럼은 마지막 시도 하나만 담는데 프리셋은 달마다 재사용된다. 실측 오판 2건:
--       · 9월 실패 후 10월 성공 → 마지막 성공만 기억해 9월 작업에서도 노출
--       · 10월만 실패 → ym=202610 > 202609 라 9월 작업까지 가림
--   3차(현재) — 월별 판정을 **하지 않는다.** 실패하면 월 구분 없이 숨기고, 다시
--     쓰려면 새로 저장한다. 그러면 2차의 두 오판이 애초에 성립하지 않고 컬럼 하나로
--     충분하다. in-place 편집만 무효화하면 되는데, 그건 last_generate_at 과
--     updated_at 을 같은 행 안에서 비교해 푼다(조인·리셋 로직 없음).
--     roster_config_attempt 는 목록 판정에서 빠지고 **분석 전용**으로 남는다.

-- ① roster_config.last_generate_status / last_generate_at — 목록 필터용.
--
--   status  NULL(안 써봄) | success | blocked | infeasible
--     실패한 설정이 프리셋 목록에 남으면 다시 선택돼 같은 실패를 반복한다.
--     다시 쓰려면 새로 저장하면 되므로 실패는 그대로 숨긴다.
--     ★ 개입(해결책 카드 config_override · 온톨로지 treatment · team_min 자동 완화 ·
--       apply-resolution 의 임시 컬럼 델타)이 낀 런은 기록하지 않는다 —
--       저장된 값 그대로의 성패가 아니다. 그런 시도도 attempt 에는 남는다.
--
--   ★★ **편집 저장으로는 되살리지 않는다.** 그 설정을 고쳐 저장해도(프론트가
--     config_id 를 보내 같은 행을 in-place 덮어쓴다) 목록에 다시 내보내지 않는다 —
--     저장은 검증이 아니고, 실패한 설정까지 보이면 프리셋의 의미가 퇴색된다.
--     새로 쓰려면 config_id 없이 저장해 신규 행을 만든다(NULL 에서 시작).
--     되살림 로직이 없으므로 쓰기 경로마다 상태를 리셋할 필요도 없다.
--   ★ 다만 다시 생성해서 **성공하면** success 로 덮여 복귀한다. 실제로 근무표가
--     만들어졌으므로 검증된 것이고, 프리셋(검증된 설정 모음)의 정의에 부합한다.
--
--   ★ 'error' 는 목록에 영향을 주지 않는다. 낙인 3지점을 안 거치고 빠져나간 예외를
--     담는 값인데, 거기엔 **설정 탓이 아닌 것**(일시적 DB 오류·구현 버그)도 섞인다.
--     그것으로 프리셋을 영구히 지우면 운영 사고 한 번에 멀쩡한 설정이 사라진다.
--     attempt 에는 남으므로 추적은 된다.
--
--   소급 적용은 하지 않는다 — 기존 행은 "안 쓴 것"과 "실패한 것"이 구분되지 않아
--   NULL 로 두고, 새 시도부터 기록한다.
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'last_generate_status'
)
BEGIN
    -- COLLATE 명시 — 이 DB 기본은 SQL_Latin1_General_CP1_CI_AS 인데 기존 컬럼은
    --   Korean_Wansung_CI_AS 다. 섞이면 조인이 오류 468 로 실패한다(아래에서 실제로 겪었다).
    ALTER TABLE roster_config ADD last_generate_status VARCHAR(20)
        COLLATE Korean_Wansung_CI_AS NULL;
END
GO

-- 폐기된 설계의 잔재 정리 — 있으면 지운다.
--   last_generate_snapshot : 스냅샷을 roster_config 에 두면 SELECT * 하는 26곳이
--     매번 수십 KB 를 끌고 온다 → roster_config_attempt.snapshot 으로 이전.
--   last_generate_ym       : 월별 판정을 컬럼 하나로 표현하려던 것. 마지막 시도만
--     담겨 오판이 났고(실측 2건), 월 구분 없이 숨기는 방침으로 정리하며 폐기.
IF EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'last_generate_snapshot'
)
BEGIN
    ALTER TABLE roster_config DROP COLUMN last_generate_snapshot;
END
GO

IF EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'last_generate_ym'
)
BEGIN
    ALTER TABLE roster_config DROP COLUMN last_generate_ym;
END
GO

IF EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'last_generate_at'
)
BEGIN
    ALTER TABLE roster_config DROP COLUMN last_generate_at;
END
GO

-- ────────────────────────────────────────────────────────────────
-- ② roster_config_attempt — 시도 시점의 입력 전체. 성공·실패 모두 한 행씩 누적.
--
-- 왜 roster_config 에 행으로 넣지 않는가 (1차 설계를 뒤집은 이유) —
--   roster_config 는 "지금 유효한 설정" 을 담는 곳이고, 코드베이스가 그 전제로
--   "이 병동의 최신 config" 를 찾는다. 기록을 같은 테이블에 행으로 넣으면
--   config_id 가 항상 최댓값이 되어 **config_id DESC 로 고르는 8곳**이 기록 행을
--   현재 설정으로 집는다. 그중 agents_v2/tools/constraint_tools.py 는 그 쿼리로
--   **UPDATE 대상**을 고른다. 테이블을 나누면 config_id 가 늘지 않아 그 경로가 없다.
--
-- 왜 스냅샷 본문인가 — 생성 성패는 roster_config 혼자 정하지 않는다. 같은 설정이라도
--   명단·daily_shift·원티드가 바뀌면 결과가 달라진다. 기존 input_hash
--   (shadow_diagnosis._input_hash)는 같은 입력을 묶는 지문일 뿐 되돌릴 수 없어
--   "무엇이 문제였나" 를 못 보고, 그 해시에는 **원티드가 빠져 있다**.
--
-- 왜 성공도 남기는가 — 실패만 모으면 비교 대상이 없다.
--   "직전엔 됐는데 왜 안 되지" 는 직전 성공 입력과 대조해야 답이 나온다.
--
-- 설정값 컬럼을 따로 두지 않는 이유 — snapshot 의 config 키에 roster_config 전 필드가
--   이미 들어 있다(daily_shift 요구·team_min·선호 가중치까지 합쳐 66필드).
-- ★ 문자열 컬럼에 COLLATE 를 **명시**한다. 이 DB 의 기본 collation 은
--   SQL_Latin1_General_CP1_CI_AS 인데 기존 테이블(roster_jobs·roster_config 등)은
--   Korean_Wansung_CI_AS 로 만들어져 있다. 기본값에 맡기면 job_id 조인이
--   "Cannot resolve the collation conflict" (오류 468)로 아예 실패한다 —
--   FAILED job 에서 그 시점 설정을 찾는 것이 이 테이블의 주 용도라 치명적이다.
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_NAME = 'roster_config_attempt'
)
BEGIN
    CREATE TABLE roster_config_attempt (
        attempt_id       BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        -- 어느 프리셋으로 시도했나. 프리셋이 지워져도 기록은 남아야 하므로 FK 를 걸지 않는다.
        source_config_id INT            NULL,
        office_id        VARCHAR(50)    COLLATE Korean_Wansung_CI_AS NULL,
        group_id         VARCHAR(50)    COLLATE Korean_Wansung_CI_AS NOT NULL,
        -- 비동기(SQS→worker) 경로의 roster_jobs.job_id. 동기 호출은 NULL.
        --   실패는 schedules 에 행이 남지 않아(result_roster_id=None) FAILED 건과
        --   "그때 무슨 설정이었나" 를 잇는 고리가 이것뿐이다.
        job_id           VARCHAR(100)   COLLATE Korean_Wansung_CI_AS NULL,
        year             INT            NULL,
        month            INT            NULL,
        -- success | blocked | infeasible  (개입 여부는 intervened 컬럼으로 분리)
        status           VARCHAR(20)    COLLATE Korean_Wansung_CI_AS NOT NULL,
        -- 이 런에 개입이 있었나 — config_override(해결책 카드) · treatment_ids(온톨로지
        --   처방) · applied_relaxations(team_min hard→soft 자동 완화) 등.
        --   개입이 낀 성패는 **저장된 프리셋 값의 성패가 아니라서** 프리셋에 낙인하지
        --   않는다. 기록은 남기되 분석 때 골라낼 수 있어야 하므로 컬럼으로 분리한다
        --   (status 에 접두를 붙이면 'intervened:infeasible' 이 21자라 VARCHAR(20) 를
        --   넘겨 INSERT 가 거부되고, 예외가 삼켜져 기록이 통째로 유실된다).
        intervened       BIT            NULL,
        -- nurses(등급·팀·허용시프트·고정근무·개인 상하한·주휴) · config(roster_config +
        --   daily_shift 요구 + team_min + 선호 가중치) · wanted · grade_config 전부.
        snapshot         NVARCHAR(MAX)  COLLATE Korean_Wansung_CI_AS NULL,
        created_at       DATETIME       NULL
    );
END
GO

-- 이미 기본 collation 으로 만들어진 환경 보정(dev 선적용분). 인덱스가 걸린 컬럼은
--   ALTER 전에 인덱스를 내려야 하므로, 인덱스 생성보다 앞에 둔다.
IF EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'roster_config_attempt' AND COLUMN_NAME = 'job_id'
      AND COLLATION_NAME <> 'Korean_Wansung_CI_AS'
)
BEGIN
    IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_roster_config_attempt_job')
        DROP INDEX ix_roster_config_attempt_job ON roster_config_attempt;
    IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_roster_config_attempt_group_created')
        DROP INDEX ix_roster_config_attempt_group_created ON roster_config_attempt;

    ALTER TABLE roster_config_attempt ALTER COLUMN office_id
        VARCHAR(50) COLLATE Korean_Wansung_CI_AS NULL;
    ALTER TABLE roster_config_attempt ALTER COLUMN group_id
        VARCHAR(50) COLLATE Korean_Wansung_CI_AS NOT NULL;
    ALTER TABLE roster_config_attempt ALTER COLUMN job_id
        VARCHAR(100) COLLATE Korean_Wansung_CI_AS NULL;
    ALTER TABLE roster_config_attempt ALTER COLUMN status
        VARCHAR(20) COLLATE Korean_Wansung_CI_AS NOT NULL;
    ALTER TABLE roster_config_attempt ALTER COLUMN snapshot
        NVARCHAR(MAX) COLLATE Korean_Wansung_CI_AS NULL;
END
GO

-- 병동별 최근 시도 조회용. 분석 진입점이 "이 병동에서 최근에 뭐가 실패했나" 라서
--   group_id + created_at 이 선두여야 한다.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'ix_roster_config_attempt_group_created'
)
BEGIN
    CREATE INDEX ix_roster_config_attempt_group_created
        ON roster_config_attempt (group_id, created_at DESC);
END
GO

-- 이미 만들어진 환경 보정(dev 선적용분) — intervened 컬럼 추가.
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'roster_config_attempt' AND COLUMN_NAME = 'intervened'
)
BEGIN
    ALTER TABLE roster_config_attempt ADD intervened BIT NULL;
END
GO

-- retention DELETE 용 — 프리셋당 최근 20건만 남기므로 (source_config_id, attempt_id)
--   조합으로 상위 N 을 바로 집어야 한다. 이게 없으면 정리 때마다 group 범위를 훑는다.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'ix_roster_config_attempt_source'
)
BEGIN
    CREATE INDEX ix_roster_config_attempt_source
        ON roster_config_attempt (source_config_id, attempt_id DESC);
END
GO

-- roster_jobs 에서 FAILED 를 발견했을 때 그 시점 설정으로 바로 가기 위한 역인덱스.
--   roster_jobs 에는 config_id 컬럼이 없어 반대 방향(job→config)은 이 경로뿐이다.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'ix_roster_config_attempt_job'
)
BEGIN
    CREATE INDEX ix_roster_config_attempt_job
        ON roster_config_attempt (job_id);
END
GO
