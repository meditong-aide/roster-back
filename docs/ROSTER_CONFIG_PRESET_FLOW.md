# 근무표 생성 설정 — 설정페이지 폐지 → 생성 시 모달 플로우

브랜치: `feat/roster-config-preset`
작성: 2026-06-24

별도 "설정 페이지"를 없애고, **근무표 생성 시점에 설정 모달**에서 기준을 정하는 플로우로 전환한다.
모달은 두 탭으로 구성된다.

- **근무표 만들기 설정**: 이번 생성에 적용할 기준을 직접 편집
- **저장한 설정**: 그룹에 저장해둔 설정 버전(프리셋) 목록을 불러옴

저장된 설정은 그룹(office+group)별로 `version`(0부터)·`config_name`·`config_memo`를 갖는다.

---

## 0. 핵심 원리 (왜 이렇게 짰나)

**솔버는 일부 토글을 `roster_config`가 아니라 라이브 테이블에서 읽는다.**

| 토글 | 솔버가 실제로 읽는 곳 |
|---|---|
| `weekly_off_group` | `weekly_off_settings.activate`, `nurses.weekly_off_enabled` |
| `use_mid` (M근무) | `shift_manage`(M슬롯), `nurses.is_night_nurse`(M), `roster_grade_config`(M키) |

따라서 "이 설정대로 생성"하려면 그 설정의 토글이 라이브 테이블에 **반영(apply)**돼 있어야 한다.
구(舊) 플로우는 설정 페이지의 "저장" = "적용"이라 저장 시점에 라이브를 동기화했다.

신(新) 플로우는 **저장(북마크)과 생성(적용)을 분리**한다.

- **저장하기** → `roster_config` row만 기록(**순수** — 라이브 안 건드림)
- **근무표 만들기** → payload를 config row로 굳히고(materialize) **라이브 동기화 후 생성**

라이브 동기화는 **생성마다 payload 값으로 항상** 수행하므로, 어떤 설정으로 생성하든 라이브 상태가
그 설정과 일치한다(이전 생성으로 인한 stale 없음).

---

## 1. 스키마 변경 (DDL)

대상 테이블: **`roster_config`** (신규 테이블 없음, 컬럼 4개 + 인덱스 1개 추가)

| 컬럼 | 타입(MSSQL) | NULL | 의미 |
|---|---|---|---|
| `version` | `INT` | NULL | 그룹(office+group)별 0부터 시작하는 프리셋 버전. **legacy row = NULL = 프리셋 아님**(목록 비노출) |
| `config_name` | `NVARCHAR(100)` | NULL | 프리셋 이름. 자동 생성분 `'새로운 설정n'` 포함 |
| `config_memo` | `NVARCHAR(500)` | NULL | 간단 메모 |
| `updated_at` | `DATETIME` | NULL | 마지막 저장 시각(upsert 시 앱이 갱신). `created_at`은 최초 생성 고정 |

인덱스: **개발 마무리 후 추가 예정 (현재 미적용)**
```sql
-- 나중에 추가할 것 (지금은 실행 안 함)
CREATE UNIQUE INDEX ux_roster_config_group_version
    ON dbo.roster_config (office_id, group_id, version)
    WHERE version IS NOT NULL;
```
> 현재는 컬럼만 추가하고 인덱스는 보류한다. `version` 할당은 앱의 `MAX+1` 단독으로 동작하며,
> 동시 저장 충돌(같은 그룹에 동시 2회 저장)은 실무상 거의 없어 당장은 유일성 인덱스 없이 둔다.
> 추후 추가 시 — 일반 `UNIQUE`는 **MSSQL이 복합 UNIQUE에서 NULL을 동일값으로 취급**해 version=NULL인
> legacy row 다수가 충돌하므로, 반드시 위처럼 `WHERE version IS NOT NULL` **필터드 유니크**로 추가한다.
> (인덱스를 다시 넣을 때 `save_roster_config_service`의 IntegrityError 재시도 가드도 함께 복원.)

### 적용 방법

운영(MSSQL)은 **수기 .sql 직접 실행 권장** (멱등 가드 포함):
```
migrations/2026_06_23_add_roster_config_preset_columns.sql
```
- `COL_LENGTH(...) IS NULL` 가드로 컬럼 ADD 멱등
- 컬럼 ADD 커밋 후 별도 `GO` 배치에서 인덱스 생성(필터드 인덱스는 컬럼 존재 후 가능)
- 적용 전 **DB 백업 필수**

또는 Python 멱등 스크립트(개발/스테이징):
```bash
cd roster-back
PYTHONPATH=app python migrations/2026_06_23_add_roster_config_preset_columns.py --dry-run   # 계획만
PYTHONPATH=app python migrations/2026_06_23_add_roster_config_preset_columns.py             # 적용
PYTHONPATH=app python migrations/2026_06_23_add_roster_config_preset_columns.py --rollback  # 되돌림(데이터 손실)
```
검증: 컬럼 4개 존재(`version`·`config_name`·`config_memo`·`updated_at`). 인덱스는 현재 미적용.

---

## 2. 백필 (Backfill)

### 2-1. 필수 백필 — **없음**

기존 `roster_config` row는 모두 `version = NULL`로 남는다(= 프리셋 아님). 이는 **의도된 동작**이다.

- "저장한 설정" 목록은 `version IS NOT NULL`만 노출 → 기존 row는 안 뜸.
- 사용자가 모달에서 "저장하기" 또는 생성(materialize)하면 그때부터 version이 부여되며 쌓인다.

→ 즉 기능 켜는 데 **데이터 마이그레이션/백필이 필요 없다.**

### 2-2. 선택 백필 — "현재 설정을 프리셋으로 노출" (운영 판단)

기존 병동이 **지금 쓰던 설정을 모달의 '저장한 설정'에서 바로 보고 싶다**면, 그룹별 최신
`roster_config`를 version 0 프리셋으로 승격할 수 있다.

```sql
-- 그룹별 최신 config_id 에 version=0, 이름 부여 (예시 — 운영 합의 후 실행)
;WITH latest AS (
    SELECT config_id,
           ROW_NUMBER() OVER (PARTITION BY office_id, group_id ORDER BY created_at DESC) AS rn
    FROM dbo.roster_config
)
UPDATE rc
   SET rc.version = 0,
       rc.config_name = N'현재 설정',
       rc.updated_at  = rc.created_at
FROM dbo.roster_config rc
JOIN latest l ON l.config_id = rc.config_id AND l.rn = 1
WHERE rc.version IS NULL;
```
- 미적용 시: 모달 첫 진입에 목록이 비어 있고, 사용자가 저장/생성하면 채워짐(기본값).
- 적용 여부는 **운영/기획 결정** — 기본은 미적용 권장(깔끔하게 사용자 큐레이션으로 시작).

### 2-3. 향후 백필 (후속 PR, P4c)

`use_mid` 저장 위치를 `daily_shift`로 이전할 때:
- 기존 `roster_config.use_mid` / `shift_manage` M슬롯(manpower>0) → 해당 (group, month)
  `daily_shift.use_mid = true`로 백필 필요.
- **이번 PR 범위 아님.** 별도 작업으로 진행.

---

## 3. 프론트 전달 사항 (API 계약)

### 3-1. 설정 저장 (저장하기 / 설정 저장)
`POST /config/save` — body: `RosterConfigCreate`
- 신규 필드: `config_id`(있으면 그 프리셋 **수정** = in-place, 없으면 **신규** INSERT), `config_name`, `config_memo`
- 신규 프리셋은 서버가 `version = MAX+1`(그룹별 0부터) 할당. **클라이언트가 version을 보내지 않음.**
- "복사" / "+ 새 설정으로 시작" 후 저장 → `config_id` 없이 전송(새 version 생성)
- 불러온 프리셋 수정 후 저장 → `config_id` 포함 전송(같은 version 유지)
- **응답**:
```jsonc
{ "message":"Configuration saved successfully",
  "config_id": 123, "version": 2, "config_name": "...", "config_memo": "..." }
```
> 저장은 **순수**다. weekly_off/use_mid 등 라이브 적용은 일어나지 않는다(생성 시 적용).

### 3-2. 저장한 설정 목록 (저장한 설정 탭)
`GET /config/versions?group_id=<관리자 선택 그룹>`
- `version IS NOT NULL`인 프리셋만, **version 내림차순**
- **응답** (배열):
```jsonc
[{
  "config_id": 123,
  "version": 2,
  "config_name": "2병동 기본 설정",
  "config_memo": "7월 기본 근무표용 ...",
  "summary": { "day_req":4, "eve_req":3, "nig_req":3, "off_days":9,
               "weekly_off_group":true, "fixed_wanted_use_yn":false, "use_mid":false },
  "last_saved_at": "2026-06-10T...",          // updated_at(없으면 created_at)
  "last_applied": { "year":2026, "month":7, "schedule_version":1 }  // 없으면 null
}]
```
- 카드 요약 문자열("D4 E3 N3 · 월 OFF 9일 …")·"최근 적용 7월 근무표 VER1"·"마지막 저장"은
  위 필드로 프런트가 조립.
- `'새로운 설정n'` 자동 생성분도 version이 있어 **목록에 함께 내려간다**. 프런트가
  `config_name LIKE '새로운 설정%'`으로 구분(뱃지/접기/정렬 뒤로 등 표현은 프런트 재량).

### 3-3. 설정 불러오기 (선택 → 상세 설정)
`GET /config/version/{selector}?group_id=&schedule_id=`
- 선택자 우선순위:
  1. `schedule_id` 제공 → 그 근무표가 생성에 쓴 config(이전 설정 불러오기)
  2. `{selector}`가 **정수** → 그 그룹의 해당 `version` 프리셋
  3. 그 외(예: `latest`) → 그룹 최신 config, 없으면 DEFAULT 생성
- 없는 version → **404**
- **응답**: 전체 설정 dict(`config_id, version, config_name, config_memo, day/eve/nig_req,
  off_days, max_conseq_work, max_nig_per_month, weekly_off_group, use_mid, off_first, …, created_at, updated_at`)

### 3-4. 근무표 만들기 (생성)
`POST /roster_create/async` — `RosterRequest`에 추가:
```jsonc
{
  "year": 2026, "month": 7,
  "group_id": "<관리자 선택 그룹>",
  "config_id": <불러온 프리셋 config_id 또는 null>,   // baseline
  "config": { ...모달 현재(편집 포함) 설정 전체... }    // 보내면 materialize
}
```
- **`config` 전송 시**: 서버가 baseline(`config_id`)과 비교
  - 동일 → baseline 재사용(새 row 없음)
  - 다르거나 baseline 없음 → **`'새로운 설정n'`** 신규 row(version=MAX+1)
  - 결정된 설정으로 **라이브 동기화(apply) 후 생성**
- **`config` 미전송 시**: 기존처럼 `config_id`(없으면 최신) 사용
- **응답**에 추가:
```jsonc
{ "...": "...", "materialized_config": { "config_id": 130, "version": 5, "config_name": "새로운 설정3" } }
```
> **계약(중요)**: 프런트는 생성 응답의 `materialized_config.config_id`를 받아
> **"현재 로드된 설정"을 그것으로 갱신**해야 한다. 안 하면 같은 화면에서 재생성 시
> baseline이 옛 값으로 남아 중복 `'새로운 설정n'`이 계속 생긴다.

### 3-5. 배포 순서 (⚠️ 필수)
저장이 순수해졌으므로 **백엔드 단독 선배포 금지**. 프런트 모달 개편(생성 시 `config` payload 전송 +
`materialized_config` 반영)과 **동시 배포**해야 weekly_off/use_mid가 정상 적용된다.

---

## 4. 동작 변경 / 주의

1. **`/config/save`는 더 이상 라이브 테이블을 동기화하지 않는다**(순수화). 동기화는 생성 시
   `apply_config_side_effects`(roster_service.py)가 담당.
2. materialize/apply는 `POST /roster_create/async`(+`wait_for_result=true`)에만 연결됨.
   별도 `POST /roster_create/generate`(순수 sync)에는 미연결 — 모달이 그 경로를 쓰면 추가 필요.
3. `version` 동시 할당 충돌은 `MAX+1` + 필터드 유니크 + 3회 재시도로 처리(동시 저장은 사실상 희박).
4. provenance: `schedules.config_id` → `roster_config.config_id` FK는 upsert(in-place)로 안 끊김 →
   "최근 적용"은 기존 조인만으로 산출. 발행본은 `issued_roster_snapshot.config_json`이 별도 스냅샷.

---

## 5. 테스트

| 파일 | 범위 |
|---|---|
| `tests/test_roster_config_preset_save.py` | upsert·version 할당 (4) |
| `tests/test_roster_config_preset_endpoints.py` | 목록·로드·최근적용·404 (5) |
| `tests/test_roster_config_materialize.py` | materialize·재사용·자동이름·apply·save 순수 (5) |

마이그레이션: 모델 import + MSSQL DDL 오프라인 컴파일 + SQLite 멱등/유니크 검증 완료.
회귀: generation/grade/precheck 기존 24개 통과.

---

## 6. 남은 작업 (후속 PR)

- **P4c**: `use_mid` 저장을 `daily_shift` 모달/라우터로 이전. `resolve_use_mid(db, office, group[, year, month])`
  헬퍼 도입 → 비월(非월) 소비자(grade_service·nurse_service·replacement·constraint_tools) repoint.
  기존 `roster_config.use_mid`/`shift_manage` → `daily_shift.use_mid` 백필(§2-3). (덩치 커서 분리)
