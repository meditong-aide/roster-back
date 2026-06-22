# 간호사 속성 시점 테이블 설계 (effective-dated, 속성별 분리)

> team처럼 속성을 effective-dated period 테이블로 분리. 공통 mixin + 제너릭 리졸버로 "테이블 多 = 코드 多"를 차단.
> 근거: `TEMPORAL_NURSE_MODEL_DESIGN.md`(v3), 업계 조사(`reference: Workday 섹션식 / Kimball mini-dimension / Data Vault rate-of-change satellite`).
> 작성 2026-06-22.

## 0. 원칙
- **쓰기는 period 테이블에만 (유일한 SSOT).** `nurses` 컬럼은 **단방향 파생 투영**(read-only) — 앱 로직은 컬럼을 **직접 쓰지 않는다**. `upsert_period`가 같은 txn에서 오늘값을 컬럼에 투영. → team의 고통(양방향 쓰기 충돌 + 지연 reconcile cron) **원천 차단**.
- **읽기 이원화**: as-of 필요(솔버·per-day) = **resolver** / 현재값 표시(127곳 등 다수) = **투영된 컬럼 그대로**. 컬럼 DROP은 reader 점진 전환 후 **선택**(쓰기가 이미 period-only라 급할 이유 없음).
- **속성별 분리**(경계 독립). 한 wide row에 몰지 않음 → redundancy 0.
- **공통 mixin 1 + 제너릭 resolver/upsert 1** → 테이블 수와 무관하게 코드 평평.
- effective-dated 규칙: `[valid_from, valid_to)` 반열림 · **겹침 금지** · **gap 허용** · 변경=**close-before-open**(옛 구간 닫고 새 구간, 삭제 금지).

## 1. 테이블 목록

| 테이블 | 값 컬럼 | group_id | 솔버 경로 | 상태 |
|---|---|---|---|---|
| `nurse_team_period` | team_id INT | ✅ | per-day team min | 있음 |
| `nurse_grade_period` | grade INT | ✅ (병동귀속) | grade min hard→soft | **신규** |
| `nurse_allowed_shift_period` | allowed_shifts JSON | ✗ | **forbidden 셀**(가동중) | **신규** |
| `nurse_weekendoff_period` | weekend_off TINYINT | ✗ | 주휴 고정셀 + weekday rotation | **신규** |
| `nurse_fixedshift_period` | fixed_shift VARCHAR(20) | ✗ | fixed 셀 빌드 | **신규** |
| `nurse_monthly_limits` | d/e/n min·max·exact | ✅ | 월 단위 | 있음 |

신규 **4개**.

## 2. 스키마 (SQLAlchemy, MSSQL)

```python
# 공통 mixin — FK·인덱스는 서브클래스에서 __tablename__ 확정 후 부여
class EffectiveDatedPeriodMixin:
    id         = Column(INTEGER, primary_key=True, autoincrement=True)   # MSSQL IDENTITY
    nurse_id   = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    valid_from = Column(DATE, nullable=False)
    valid_to   = Column(DATE, nullable=True)            # null = 열린(계속) 구간
    source     = Column(VARCHAR(20), nullable=False, default="edited")   # inherited|edited|redistribute
    note       = Column(TEXT, nullable=True)
    created_at = Column(DATETIME, default=func.now())
    updated_at = Column(DATETIME, default=func.now(), onupdate=func.now())

class NurseGradePeriod(Base, EffectiveDatedPeriodMixin):
    __tablename__ = "nurse_grade_period"
    group_id = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    grade    = Column(INTEGER, nullable=True)
    __table_args__ = (Index("ix_ngp_nurse", "nurse_id", "valid_from"),
                      Index("ix_ngp_group", "group_id", "valid_from"))

class NurseAllowedShiftPeriod(Base, EffectiveDatedPeriodMixin):
    __tablename__ = "nurse_allowed_shift_period"
    allowed_shifts = Column(JSON, nullable=False)        # ["D"] / ["D","E"] / ["N"]
    __table_args__ = (Index("ix_nasp_nurse", "nurse_id", "valid_from"),)

class NurseWeekendOffPeriod(Base, EffectiveDatedPeriodMixin):
    __tablename__ = "nurse_weekendoff_period"
    weekend_off = Column(TINYINT, nullable=True)
    __table_args__ = (Index("ix_nwop_nurse", "nurse_id", "valid_from"),)

class NurseFixedShiftPeriod(Base, EffectiveDatedPeriodMixin):
    __tablename__ = "nurse_fixedshift_period"
    fixed_shift = Column(VARCHAR(20), nullable=True)
    __table_args__ = (Index("ix_nfsp_nurse", "nurse_id", "valid_from"),)
```
> `nurse_team_period`는 기존 정의 유지(이 mixin으로 리팩터는 선택). JSON은 MSSQL에서 NVARCHAR(MAX).

## 3. 리졸버 (services/nurse_period_resolver.py 신규)

**bulk fetch — 테이블당 1쿼리(간호사 수 무관).**
```python
def fetch_periods(db, Model, nurse_ids, month_start, month_end, group_id=None):
    q = db.query(Model).filter(
        Model.nurse_id.in_(nurse_ids),
        Model.valid_from < month_end,
        or_(Model.valid_to.is_(None), Model.valid_to > month_start),
    )
    if group_id is not None and hasattr(Model, "group_id"):
        q = q.filter(Model.group_id == group_id)     # ward-aware (team/grade)
    by_nurse = defaultdict(list)
    for r in q.all():
        by_nurse[r.nurse_id].append(r)
    return by_nurse

def resolve_asof(rows_for_nurse, day, value_attr, default):
    for r in rows_for_nurse:                          # 겹침 없음 → 최대 1개 매치
        if r.valid_from <= day and (r.valid_to is None or day < r.valid_to):
            return getattr(r, value_attr)
    return default                                    # gap = default
```

**gap(미지정) default 정책 — 속성별:**
| 속성 | gap default |
|---|---|
| allowed_shifts | 전체 허용(제한 없음 = forbidden 셀 미주입) |
| weekend_off | 캐시 `nurses.is_weekend_off` → 없으면 0 |
| fixed_shift | 없음(고정 미적용) |
| grade | 캐시 `nurses.grade` → 없으면 `null_grade_policy` |
| team | ward-aware 캐시 `nurses.team_id` (group 일치 시만) |

## 4. upsert (close-before-open)

```python
def upsert_period(db, Model, nurse_id, valid_from, value_attr, value,
                  group_id=None, *, nurse=None, cache_attr=None):
    # valid_from 을 덮는 열린 구간을 찾아 닫고(값이 다르면), 새 구간 open
    cur = open_span_covering(db, Model, nurse_id, valid_from, group_id)
    if cur is not None and getattr(cur, value_attr) == value:
        return cur                                    # 동일값 = no-op
    if cur is not None:
        cur.valid_to = valid_from                     # close-before-open
    row = Model(nurse_id=nurse_id, valid_from=valid_from, valid_to=None, **{value_attr: value})
    if group_id is not None: row.group_id = group_id
    db.add(row)
    # ── 단방향 동기 투영 (앱은 컬럼을 여기서만 갱신) ──
    # 변경이 today 를 덮으면 nurses 캐시 컬럼에 즉시 반영(같은 txn, 지연 0, 충돌 불가).
    # 미래발효(valid_from > today)면 투영 안 함 → 일일 roll job 이 발효일에 반영.
    if nurse is not None and cache_attr and valid_from <= _today():
        setattr(nurse, cache_attr, value)
    return row
```
> 투영을 끄려면 `nurse=None`. 컬럼을 이미 DROP 한 속성은 그냥 안 넘기면 됨(분기 무해).

## 5. backfill (현 캐시 → 열린 구간 1개)

각 간호사 1 row(open span `[backfill_from, null)`):
- allowed_shift ← `_normalize_allowed_shift_types(nurses.is_night_nurse)`
- weekendoff ← `nurses.is_weekend_off`
- fixedshift ← `nurses.fixed_shift`
- grade ← `nurses.grade` (group_id=`nurses.group_id`)

## 6. 솔버 연결 (경로별 — 순차)

| 속성             | 주입 지점                                                | 변경                                                                                                                                     |
| -------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| allowed_shifts | `roster_create_service.py:2556~2623`                 | 간호사 단일 `allowed` → **per-day** `resolve_asof(allowed_shift, day)`. disallowed=all−allowed. 다운스트림(forbidden→`initial_forbidden`→솔버) 재사용 |
| weekend_off    | 주휴 고정셀 계산(`:1115~`, `_calc_weekly_off_weekday_by_*`) | 간호사 단일 플래그 → per-day resolve. weekday rotation × 구간경계 상호작용 확인                                                                          |
| fixed_shift    | `_build_fixed_shift_roster(:1007~)`                  | 단일 코드 → per-day resolve                                                                                                                |
| grade          | team/grade 단일값(`cp_sat_basic.py:786`)                | 우선 month-as-of(1일 기준) 단일값 유지, day-grain은 수요 시                                                                                          |

> allowed_shifts만 day-grain 실수요(교육) 확실 → **1순위**. 나머지는 같은 resolver 위에 경로만 추가.

## 7. 흐름 (생성기 read)
```
1. group_members_in_month(group,y,m)            → nurse_ids        (기존, 1쿼리)
2. fetch_periods × 5테이블 (IN nurse_ids)        → 메모리 맵        (5쿼리)
3. per (nurse, day) resolve_asof, gap→default
4. 솔버 주입 (forbidden 등)
```
DB I/O = 생성당 +5 bulk SELECT. 월배치 + 인덱스 + 희소 → 무시 가능.

## 8. 마이그레이션 단계
| P | 내용 | 검증 |
|---|---|---|
| 0 | mixin + 4 모델 + DDL(테이블 생성) | 스키마 생성·import |
| 1 | resolver/upsert/fetch_periods (제너릭) | 단위테스트(겹침·gap·close-before-open) |
| 2 | backfill(캐시→open span) | row 수 = 간호사 수 |
| 3 | **allowed_shifts** 솔버 전환(2556~2623) + dual-read 비교 | 교육 D→DE 케이스, 무회귀 |
| 4 | weekend_off 전환 | 주휴 회귀 |
| 5 | fixed_shift 전환 | 고정근무 회귀 |
| 6 | grade 전환(hard→soft) | G1 회귀 |
| 7 | 쓰기 경로 전환: 속성변경 API → **`upsert_period`만** (컬럼 **직접 쓰기 전부 제거**) + **단방향 일일 투영 job**(미래발효 roll: `컬럼 = resolve(today)`) | 컬럼 == resolve(today), 직접쓰기 grep 0 |
| 8 | (선택·나중) reader 점진 전환 후 `nurses` 컬럼 DROP | 쓰기가 이미 period-only라 안전 |

각 P: backfill→이중읽기 비교→read 전환→캐시 강등. 롤백=read 소스 되돌리기.

## 9. 함정
- **컬럼 직접 쓰기 금지 (핵심).** `nurses.{is_night_nurse,is_weekend_off,fixed_shift,grade}` 를 앱에서 직접 set 하는 코드는 전부 `upsert_period` 경유로. team 고통의 원인 = 이 규칙 부재(양방향). CI grep 가드 권장.
- 겹침금지/gap허용/close-before-open 불변식은 **upsert에서만** 강제(직접 INSERT 금지).
- allowed_shifts day-grain 시 **N전담→team None 부작용**(`:4496`)이 그 날부터 발효 → grade/team period와 경계 정합 확인.
- weekend_off는 weekday rotation과 결합 → 구간 경계 넘는 주(week) 처리 주의.
- MSSQL: JSON→NVARCHAR(MAX), IDENTITY, TINYINT.
- 휴직 부분구간(§7-10)은 **별개 메커니즘**(blocked-days) — 이 설계에 섞지 말 것.

## 10. 프리셉터 (Phase 5 — 별개 작업, 관계 테이블)

> 속성(1 nurse + 값)이 아니라 **관계**(preceptor+preceptee 2명+기간) → `EffectiveDatedPeriodMixin` 미사용. 위 4개 속성 period(P0)와 **분리**.

### 10.1 현재 이중 저장 (cancel 위험의 근원)
| 저장소 | 역할 | 누가 읽나 |
|---|---|---|
| `nurses.preceptor_id` | 캐시(현재 페어) | **솔버** (`roster_create_service.py:4765/4906/5065`) |
| `nurse_assignment` (reason=`프리셉티`) | 기간 + lifecycle | `flush_expired_preceptees(:1587)` · `flush_orphan_preceptee_assignments(:1667)` · transfer-detach(`:1418`) |

### 10.2 테이블
```python
class Preceptorship(Base):
    __tablename__ = "preceptorship"
    id           = Column(INTEGER, primary_key=True, autoincrement=True)
    preceptor_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    preceptee_id = Column(VARCHAR(50), ForeignKey("nurses.nurse_id"), nullable=False)
    group_id     = Column(VARCHAR(50), ForeignKey("groups.group_id"), nullable=False)
    start_date   = Column(DATE, nullable=False)
    end_date     = Column(DATE, nullable=True)            # null = 진행 중
    status       = Column(VARCHAR(10), nullable=False, default="active")  # active|ended|cancelled
    note         = Column(TEXT, nullable=True)
    created_at   = Column(DATETIME, default=func.now())
    updated_at   = Column(DATETIME, default=func.now(), onupdate=func.now())
    __table_args__ = (Index("ix_pcs_preceptee", "preceptee_id", "start_date"),
                      Index("ix_pcs_preceptor", "preceptor_id", "start_date"))
```

### 10.3 마이그레이션 순서 (빅뱅 cancel ❌ — cancel은 맨 마지막)
| P | 내용 |
|---|---|
| 5-1 | `preceptorship` 테이블 생성 |
| 5-2 | **backfill**: `assignment(reason=프리셉티)` → preceptorship. (assignment는 **그대로 둠**) <br> preceptee=`assignment.nurse_id` · preceptor=`nurses[nurse_id].preceptor_id`(캐시 조인) · start/end=`start_date`/`expected_end_date` · group=`source_group_id` |
| 5-3 | dual-read 검증 (preceptorship == assignment 파생) |
| 5-4 | **lifecycle 재지정**: `flush_expired_preceptees`·transfer-detach → `preceptorship.end_date` 기준 (← cancel 전 필수) |
| 5-5 | 솔버 read·캐시 reconcile → preceptorship 기준 |
| 5-6 | (마지막) 신규 프리셉티-assignment 쓰기 중단 + old row 은퇴/cancel |

### 10.4 함정
- **cancel을 먼저 하면 lifecycle cron이 `nurses.preceptor_id`를 flush 못 해 stale preceptor → 솔버 오염.** 반드시 5-4(lifecycle 이관) 후 cancel.
- presence/가시성은 이미 프리셉티 skip(`assignment_service.py:362`)이라 cancel 영향 적음.
- backfill 시 **이미 flush된 과거 페어는 `preceptor_id`=null** 가능 → 과거 관계 일부 소실(active는 온전).
