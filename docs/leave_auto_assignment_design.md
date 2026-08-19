# 보건휴가 · 수면OFF 자동 부여 설계

> 대상 브랜치: `feat/banned-wanted-diagnosis`
> 작성 근거: 인천의료원(office `102243`) 2026-07·08 확정 근무표 **엑셀 원본 전량 스캔**(29개 시트 · 445 레코드)
> 상태: **설계 확정 전 검토본** — 구현 착수 전 합의용

---

## 1. 배경

병원 확정 근무표에는 `보건휴가`·`수면휴가`가 매달 들어가지만 **시스템은 이를 만들어내지 못한다.**
9월 생성 결과에 두 코드가 **0건**이었다(41-RN `{O,D,E,N,DE}` / 별관1 `{O,D,E,N}`).

현재 유일한 입력 경로는 **고정 원티드**인데 실제 등록은 일부뿐이다.

| 그룹 | 8월 실제 보건휴가 | 고정 원티드 등록 |
|---|---|---|
| 51-AN | 9명 전원 1개씩 | **0건** |
| 41-AN | 10건 | 4건 |
| 42-RN | 17건 | 6건 |

→ 병원이 손으로 채우고 있다. **자동 부여가 필요하다.**

### 실제 누락 사례
중환자실 8월, 같은 날(8/16) `N15`를 찍은 두 사람 중 **한 명만** 수면휴가를 받았다.

```
김규연  16일:N15  17:OFF 18:OFF 19:D 20:E 21:E 22:OFF 23:OFF  → 8/24 수면휴가 ✓
노유경  16일:N15  17:OFF 18:OFF 19:E 20:E 21:E 22:OFF 23:D    → 8월 미부여
```
노유경은 8/16 이후 8월에 N 블록이 없어 **9월 이월이 정상**이지만(§3.2),
사람이 수기로 짜다 보면 이런 판정이 매달 반복된다. 시스템이 추적하면 사라지는 종류의 문제다.

---

## 2. 이미 있는 것 — `vacation_types` 메커니즘

**솔버 본체는 손댈 필요가 없다.** 휴가를 OFF 쿼터와 분리하는 구조가 이미 완성돼 있다.

```python
# cp_sat_basic.py:2622
vacation_types = {"휴가", "공가"}
fixed_vacation_off_cells = {
    (n, d) for (n, d), s_idx in fixed.items()
    if s_idx == off_idx_full and fixed_type_by_cell.get((n, d)) in vacation_types
}

# cp_sat_basic.py:2692
def countable_off(n, d):
    if (n, d) in vacation_off_cells:
        return 0          # ← 휴가는 OFF 쿼터를 먹지 않는다
    return X(n, d, off_idx_full)
```

`compute_off_bounds` 도 동일 원칙이다(`off_cap_semantics_label() == "nonvac_total_off"`).

```python
active_nonvac_days = max(0, avail_days - vacation_cnt)
```

세 코드 모두 이 타입에 해당한다.

| 코드 | `shifts.type` | `vacation_types` |
|---|---|---|
| 보건휴가 / 보건 | 공가 / 휴가 | ★ 해당 |
| 수면휴가 | 휴가 | ★ 해당 |
| 감정휴가 | 휴가 | ★ 해당 |

### 실측 확인
8월 확정본에서 **OFF 개수는 그대로이고 휴가가 별도로 얹힌다.**

```
51-AN   O 11개 × 9명 전원  +  보건 1 × 9명  +  감정 1 × 9명
중환자실  O 11개 × 26명 전원 +  보건 1 × 19명
```

★ 그리고 **보건휴가가 있는 날에도 D/E/N 요구가 정확히 충족된다**(51-RN 8월: 8/19 보건 4명인데 D 5/5·E 4/4·N 4/4).
즉 휴가는 **근무 가능 인원에서 빠지는 것**이지 OFF를 대체하지 않는다.
→ **날짜를 먼저 정하고 그 상태로 솔버를 돌려야 한다.** 사후 치환은 커버리지를 깨뜨린다.

### 현재 파이프라인 (그대로 재사용)
```
fixed_wanted_entries(휴가코드)
  → special_fixed_requests            (roster_create_service.py:199)
  → config_dict['fixed_cells']
  → cp_sat_basic: fixed / fixed_type_by_cell
  → vacation_types 판정 → fixed_vacation_off_cells
  → countable_off() 에서 제외
```
**자동 부여기는 `special_fixed_requests` 에 셀을 얹기만 하면 된다.**

---

## 3. 규칙 (실측 확정)

### 3.1 보건휴가 — 요일 축

```
대상   여성 AND N전담 아님 AND 고정근무 아님 AND 임산부(산전휴가) 아님 AND 그 달 정상근무
부여   월 1개
배치   평일 우선 · OFF 다음날 선호 · 하루 1~2명(최대 4명) 분산
```

**정확도: 적격 281명 중 276명 수령 = 98.2%**

제외 조건은 전부 실측으로 뒷받침된다.

| 제외 | 근거 |
|---|---|
| 남성 | 41-AN 유상현·박상진·장민성, 별관1 남 4명, 응급실 최명근·김지영·장승수 — 전원 미수령 |
| N전담 | 41-RN 이주영·임지은, 51-RN 김현지·손정은, 52-RN 김지민·김윤지 |
| 고정근무 | 종미영(DE)·박상철(M)·이현(M)·김현·지근석(M) |
| **임산부** | 산전휴가 보유 8명 전원 미수령 — 최수진A·B, 강다예, 여지혜, 박정현, 김하나, 정혜지, 권은지 |

★ **임산부 제외는 이번 분석에서 새로 확인된 조건**이다(기존 메모에는 남성·N전담만 있었다).

예외 5건(1.8%)은 **근무일 10~11일의 단축 달**이 대부분이다.

#### 배치 특성 (그룹별 실측)
```
하루 동시 인원   1명이 60~70% · 최대 4명 (그룹 규모와 무관)
OFF 다음날      40~76% (평균 60%)         ← 경향이지 절대 규칙 아님
주말 비율       대부분 0~5%
               ★ 예외: 41-RN 34% · 별관1 25% · 52-AN 22%
직전 N 과 무관   47% 가 그 달 N 이전이거나 N 과 무관
```

### 3.2 수면OFF — N 블록 축

```
트리거  N 연번이 15 도달
부여    1개
배치    N15 가 속한 N블록 종료 후 ~ 다음 N블록 시작 전
이월    그 달에 다음 N블록이 없으면(월말 도달) 다음 달로 넘김
```

**정확도: 준수 104 · 월경계 이월(정상) 9 · 위반 4 = 96.6%**

★ 위반 4건도 미부여가 아니라 **다음 블록보다 며칠 늦게 준 것**이다
(강성진·김정주·이가영B·백지은).

#### 이월 검증 — 7월 → 8월 5/5 완벽 대응
```
별관1   이성애   7/30 N15 → 8/1  수령
52병동  최유진   7/31 N15 → 8/1  수령
응급실   송지영   7/31 N15 → 8/3  수령
52병동  지민     7/30 N15 → 8/6  수령
42병동  최지수   7/30 N15 → 8/12 수령
```

#### N 연번은 월을 넘어 이어진다
```
7월 마지막 연번 → 8월 첫 연번 연속   142/150 = 94.7%
7월 마지막 연번 + 8월 issued N 개수 = 8월 마지막 연번   83/88 = 94.3%
```
★ 현재 `cross_month_lookback = 6일` 로는 절대 추적 불가.
★ **`schedule_entries` 만으로 연번 복원이 가능**하다는 것이 94.3%로 확인됐다
   → 실시간 카운터 불필요, **월말 앵커 1개만 저장**하면 된다.

#### 배치 특성
```
직전 N 로부터   2일후 57 · 1일후 20 · 3일후 12  → 1~3일에 75%
직전날          OFF 69%
요일            무관 (주말 31/119건)
```

### 3.3 감정휴가 — 자동화 대상에서 제외

```
운영 병동   24개 (병동·구분·월) 조합 중 6개에서만 발생
주기        매월이 아님 — 51-AN 7월 0명 → 8월 11명 / 52-AN 7월 9명 → 8월 0명
대상        사실상 전원 (고정근무 지원인력 박상철·노미선도 수령)
표본        총 31건
```
★ **"언제 주는가"가 병동 재량**이고 매월이 아니다. 규칙으로 굳히면 실제 운영과 어긋난다.
→ **고정 원티드로 병원이 지정하는 현행 유지.**
→ 연 몇 회 부여인지는 **병원 확인 필요**(7·8월 2개월로는 분기/반기 판별 불가).

---

## 4. ★ 코드 매핑 문제

`shifts.name` 이 병동마다 다르고 `type` 도 갈린다.

| `name` | `type` | 병동 |
|---|---|---|
| **보건휴가** | 공가 | 41-RN·AN, 51-RN·AN, 별관1, 응급실-RN·AN, 호스피스 |
| **보건** | 휴가 | 42-RN·AN, 52-RN·AN, 중환자실 |

→ **이름으로 찾으면 절반을 놓친다.**

다만 `schedule_entries.shift_id`(코드)는 이미 표준화돼 있다.
```
8월 전 13그룹:  보건 175건 · 수면 57건 · 감 13건
```

### 결정: `shifts` 에 타깃 플래그를 둔다 — `off_swap` 의 **스키마 관례만** 차용
멀티 병원 DB(102560·101358 등 공용)이므로 코드를 상수로 박을 수 없다.
그런데 **"어느 코드인지 지목하는" 똑같은 문제를 이미 푼 구조가 있다.**

> ★ 차용하는 것은 **"그룹당 1건의 BIT 플래그로 코드를 지목한다"는 관례뿐**이다.
> `off_swap` 의 **동작을 재사용하지 않으며 `off_swap.py` 는 건드리지 않는다.**
> 상세는 바로 아래 [§4.1](#41--off_swap-과-무엇이-다른가--로직은-공유하지-않는다).

```
roster_config.off_swap_enabled   BIT   기능 on/off
shifts.off_swap_target           BIT   ★ 어느 코드로 변환할지 (그룹당 1건)
```

검증까지 갖춰져 있다 — `_assert_off_swap_target_valid`([shift_service.py:28](roster-back/app/services/shift_service.py#L28))
```
1) type='근무' 인 코드에는 설정 불가   ← 커버리지 oversupply 방지
2) 동일 group_id 내 True 는 단 1건만   ← 다수면 sequence ASC 첫 건만 채택돼 의도와 어긋남
```

#### 왜 `roster_config` 에 코드 문자열을 두지 않는가
> **정보 자체가 불필요해진 것이 아니다.** "이 그룹에서 보건휴가에 해당하는 코드는 무엇인가" 는
> 없으면 부여가 불가능한 필수 정보다. **어디에 적어두느냐만 바뀐다** —
> `roster_config` 의 문자열 → 해당 `shifts` 행에 세우는 표식.
> 그래서 초안 컬럼 `roster_config.health_leave_shift_id` 는 드롭한다(SQL §0).

| | `roster_config.health_leave_shift_id`(문자열) | `shifts.health_leave_target`(BIT) |
|---|---|---|
| 오타 | 가능 — `'보건'` vs `'보건휴가'` | 불가 |
| **존재 검증** | 별도 확인 쿼리 필요 | **플래그 자체가 존재를 보장** |
| 다중 지정 | 막을 장치 없음 | 검증 함수로 1건 강제 |
| 일관성 | 새 방식 | **`off_swap` 과 동일** |
| 프론트 | 텍스트/드롭다운 | shifts 목록의 체크박스 |

★ **존재 검증이 결정적이다.** 문자열 방식은 "설정한 코드가 그 그룹 `shifts` 에 있는지"를
매번 확인해야 하는데(응급실-AN 에는 `수면휴가` 자체가 없다), 플래그 방식은 그 문제가 없다.
**코드가 없는 그룹은 플래그를 켤 대상이 아예 없으므로 미사용으로 자연 처리된다.**

★ office 102243 현황: 13그룹 전부 `shift_id='보건'` 행이 **정확히 1개씩** 존재하고
`type` 은 공가/휴가로 갈리나 `근무` 가 아니라 검증을 통과한다. 기존 `off_swap_target=True` 는 0건이라 충돌도 없다.

### 4.1 ★ `off_swap` 과 무엇이 다른가 — 로직은 공유하지 않는다

**`off_swap_target` 을 재사용하면 안 된다.** 두 기능은 동작이 정반대다.

```python
# off_swap.py:33 — 초과 OFF 를 연차 코드로 변환
baseline = int(getattr(latest_config, "off_days", 0) or 0)   # 생성이 끝난 뒤 후처리
```

| | `off_swap` | 보건휴가 |
|---|---|---|
| 시점 | 생성 **후** 후처리 | 생성 **전** 사전 주입 |
| 대상 셀 | 초과 **OFF** | **근무일** |
| OFF 쿼터 | 초과분을 소비 | **안 먹음** (`vacation_types` → §2) |
| 발동 조건 | OFF 가 남을 때만 | 적격자 **전원 매월 1개** |
| 커버리지 | 영향 없음 (OFF→휴가성 코드) | **가용 인력 1명 감소** |

★ 보건휴가를 `off_swap_target` 으로 지정하면 **"OFF 가 남는 사람만 보건휴가를 받는"**
엉뚱한 결과가 된다. 실측은 적격 281명 중 276명이 **OFF 잔여와 무관하게** 매월 1개다(§3.1).
게다가 OFF→보건으로 이름만 바꿔봐야 쉬는 날 총량이 그대로라
실측의 `OFF 11 + 보건 1 = 12일` 이 재현되지 않는다.

**따라서 별도 컬럼 · 별도 resolver · 별도 검증 함수를 만든다.**
```
shifts.health_leave_target            ← off_swap_target 과 다른 컬럼
_resolve_health_leave_shift()         ← _resolve_target_shift 와 다른 함수 (형태만 동일)
_assert_health_leave_target_valid()   ← 정책 2개(type≠'근무' · 그룹당 1건)만 같은 형태
```

#### 두 기능이 한 그룹에서 동시에 켜지면
타깃 코드가 다르므로(연차 `FB` vs 보건) 충돌하지 않는다.
다만 **보건휴가로 근무일이 줄면 OFF 초과가 덜 생겨 연차 변환량이 감소**할 수 있다.
★ office 102243 은 `off_swap_target` 이 **0건**이라 당장 무관하다
(dev 의 6건은 전부 타 office — 중환자실1·2, 9병동, 4병동).

---

## 5. 스키마

### 5.1 스키마 — `off_swap` 의 컬럼 **관례**를 따른다 (동작은 무관 · §4.1)

**Step 1 범위 — 보건휴가만** (`scripts/leave_auto_assignment_step1.sql`)
```sql
-- 어느 코드가 보건휴가인가 (그룹당 1건 · type≠'근무')
ALTER TABLE shifts        ADD health_leave_target  BIT NOT NULL DEFAULT 0;
-- 기능 on/off + 주말 배치 허용
ALTER TABLE roster_config ADD health_leave_enabled BIT NULL;
ALTER TABLE roster_config ADD health_leave_weekend BIT NULL;
```

**Step 4 범위 — 수면OFF** (착수 시점에 별도 SQL · 같은 구조)
```sql
ALTER TABLE shifts        ADD sleep_off_target  BIT NOT NULL DEFAULT 0;
ALTER TABLE roster_config ADD sleep_off_enabled BIT NULL;
ALTER TABLE roster_config ADD sleep_off_cycle   INT NULL;   -- 기본 15
```

기본값 (office 102243)
```
shifts.health_leave_target=1   각 그룹의 shift_id='보건' 행 (13그룹 전부 1개씩 존재)
health_leave_enabled=1         전 그룹
health_leave_weekend=1         41-RN · 별관1 · 52-AN   (실측 주말 34%·25%·22%)
                        =0     그 외 (0~5%)
```

★ **응급실-AN 은 `수면휴가` 코드가 없으므로 Step 4 에서 `sleep_off_target` 을 켤 행이 없다**
→ 자동으로 미사용. 별도 예외 처리가 필요 없다.

### ★ DDL 이 항상 먼저다
모델을 먼저 고치면 SQLAlchemy 가 없는 컬럼을 SELECT 해 **기존 조회가 전부 실패한다.**
```
Invalid column name 'health_leave_shift_id'  →  RosterConfig 조회 불가 (실측)
```
순서: **SQL 실행 → 확인 → `models.py` 수정 → 회귀 생성**

### 5.2 `nurse_night_cycle` 신규 — **Step 4 에서 확정** (지금 만들지 않는다)

#### ★ 왜 저장이 필요한가 — DB 에 N 연번이 없다
`schedule_entries.shift_id` 에는 **`'N'` 만** 저장된다. `N1`~`N15` 연번은 엑셀에만 있다.
그래서 `schedule_entries` 만으로는 "15 에 도달했는가"를 알 수 없다.

실측(중환자실 8월) — 연번 없이 "마지막 N블록 이후 수면 없음"으로 판정하면
```
실제 pending  3명 (유희주·이재영·노유경)
계산 결과    20명 전원이 후보  ← 판정 불가
```
**앵커(마지막 연번)가 있어야** `앵커 + 당월 N 개수` 로 연번이 복원되고 15 도달 지점이 특정된다.

#### ✅ 확정 — **월별 스냅샷** (2026-08-03)
원리적으로는 **앵커 1행이면 충분**하다(`9월 = 8월앵커 + 9월N`, `10월 = 8월앵커 + 9월N + 10월N` …).
월별로 두는 실익은 둘뿐이다.

| | 앵커 1행 | 월별 스냅샷 |
|---|---|---|
| 계산량 | 앵커 이후 전 월 누적 | 직전 달만 참조 |
| **오차 누적** | 복원 오차(94.3%)가 **매달 쌓임** | 확정 시 갱신 → **그 달로 국한** |

★ 오차의 원인은 **근무표에 안 잡히는 N**(파견·타 병동 지원)이다.

**결정 근거** — 9월 확정본을 기다리지 않고 지금 확정한다.
1. **월별은 앵커의 상위 호환이다.** 월별 표에서 최신 1행만 읽으면 앵커와 똑같이 쓸 수 있다.
   반대(앵커 → 월별)는 과거 데이터가 없어 불가능하다. **되돌릴 필요가 없는 선택.**
2. 오차 누적은 회복 수단이 없다. 앵커 1행으로 시작하면 검증 데이터가 좋아진 뒤에도
   이미 쌓인 오차를 걷어낼 방법이 없다.
3. `nurse_monthly_limits`(per-nurse × 월, 233행 사용 중)와 같은 패턴이라 신규 개념이 아니다.

#### 용어 정정
이건 `nurse_grade_period` 같은 **effective-dated period 가 아니다**(valid_from~valid_to·close-before-open·as-of).
`nurse_monthly_limits` 와 같은 **월별 스냅샷**(`nurse_id, group_id, year, month` UNIQUE · 확정 시 upsert)이다.

#### 근본 대안 (이번 범위 밖)
`schedule_entries.shift_id` 에 `'N1'`~`'N15'` 를 그대로 저장하면 앵커 테이블 자체가 불필요해진다.
다만 `code2main` 정규화 · 통계 집계 · 프론트 표시가 모두 `'N'` 을 전제하고 있어 영향이 크다.

---

아래는 Step 4 착수 시의 초안이다(확정 아님).
`nurse_monthly_limits` 와 동일 스코프 패턴(per-nurse × 월)을 따른다.

```sql
CREATE TABLE nurse_night_cycle (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    nurse_id      VARCHAR(50)  NOT NULL,
    group_id      VARCHAR(50)  NOT NULL,
    year          SMALLINT     NOT NULL,
    month         TINYINT      NOT NULL,
    seq_at_end    INT          NULL,   -- 그 달 마지막 N 의 연번 (다음 달 시작점)
    pending_sleep INT          NULL,   -- 이월된 미부여 수면OFF 수
    created_at    DATETIME     DEFAULT GETDATE(),
    updated_at    DATETIME     DEFAULT GETDATE(),
    CONSTRAINT ux_nurse_night_cycle_scope UNIQUE (nurse_id, group_id, year, month)
);
```

★ `night_count` 같은 **그 달만 세면 나오는** 집계 컬럼은 두지 않는다.
`schedule_entries` 에서 언제든 계산되므로 중복 저장할 이유가 없다(§3.2 94.3% 검증).

#### ★ 예외 — 수면OFF 누적 회차 (2026-08-04 추가)
```sql
ALTER TABLE nurse_night_cycle ADD sleep_off_count INT NULL;  -- 그 달 부여 횟수
ALTER TABLE nurse_night_cycle ADD sleep_off_seq   INT NULL;  -- 그 달 말 누적 회차
```
누적 회차는 **전 기간을 훑어야 나오는 상태값**이라 `seq_at_end` 와 같은 층위다.
그 달만 세면 되는 `night_count` 와 성격이 다르므로 위 원칙의 예외가 맞다.

두 컬럼을 **함께** 두는 이유도 `seq_at_end`/`pending_sleep` 과 같다.
```
seq 만 있으면    "이번이 12번째"는 알아도 그 달에 받았는지를 못 본다
count 만 있으면  누적을 매번 전 기간 SUM 해야 한다
```

**★ 근무환경 지표로 쓴다 — 수령 빈도가 곧 나이트 부담이다**
2026-07~08 실측(2개월).
```
수면 0회 (115명)   2개월 N 평균  5.8회
수면 1회 (113명)   2개월 N 평균 10.6회   ← 거의 2배
```
수면OFF 1회 ≈ N 15회이므로 `sleep_off_seq × 15 ≈ 누적 나이트` 로 환산된다.

활용
```
누적 회차       최신 행의 sleep_off_seq
수령 간격       count > 0 인 달들의 간격 → "평균 3.2개월에 1회"
과부하 탐지     같은 병동에서 seq 상위 = 나이트 편중 대상자
병동 간 비교    평균 seq 로 인력 배분 문제 감지
연도별 추이     year 로 묶어 "작년 4회 → 올해 7회" 악화 감지
```

백필 결과(2개월이라 최대 1회 — 누적이 쌓이면 변별력이 커진다)
```
평균 seq 상위   42병동-RN 0.70 · 응급실-RN 0.69 · 52병동-RN 0.62
평균 seq 하위   42-AN 0.25 · 51-AN 0.25 · 52-AN 0.23 · 응급실-AN 0.00(코드없음)
```
★ RN 이 AN 보다 일관되게 높다 — 나이트 부담이 RN 에 집중된다는 실측과 부합한다.

★ **누적 기준은 데이터가 있는 2026-07 부터**다. 그 이전 이력은 0 에서 시작한다.
  백필은 `tools/leave_analysis/backfill_sleep_off_seq.py` — `seq_at_end`/`pending_sleep`
  은 건드리지 않고(엑셀 연번 기준 유지) 부여 사실만 **DB 확정본**에서 센다.

★ 두 컬럼이 **각각 필요한 이유** — 중환자실 유희주·이재영이 실증한다.
```
8/30 N15 → 8/31 N1 시작 → seq_at_end = 1  이지만  pending_sleep = 1
```
`seq_at_end` 만으로는 이월을 표현할 수 없다.

★ FK 제약은 [[feedback_pix_ddl_no_constraints]] 방침에 따라 걸지 않는다(PK + NOT NULL 만).

---

## 6. 구현 단계

### Step 1 — 보건휴가 설정 스키마 (솔버 무영향) ✅ **dev 완료 · prod 대기**

**적용 현황 (2026-08-03)**
```
dev(eun_roster_dev)   DDL·값주입·코드배선 완료 · 회귀 ALL PASS
prod(eun_roster)      ★ 미적용 — main 배포 직전에 DDL 먼저 실행
```

★★ **배포 순서**: `prod DDL` → `main 배포`. 뒤집으면 SQLAlchemy 가 없는 컬럼을
SELECT 해 **RosterConfig / Shift 조회가 전부 실패**한다.

**★ 저장 덮어쓰기 함정 (구현 중 발견)**
`save_roster_config_service` 는 `model_dump()` 를 일괄 `setattr` 한다.
스키마 기본값을 `False` 로 두면 **이 필드를 모르는 기존 저장 화면이 저장할 때마다
설정이 꺼진다**(`manages_avoid` 가 기피를 전량 삭제했던 것과 같은 구조).
```
스키마     Optional[bool] = None          미전송과 명시적 False 를 구분
수정 경로   _PRESERVE_IF_NONE 가드         None 이면 setattr 스킵 → 기존 값 유지
신규 경로   직전 최신 config 에서 승계      생성은 created_at DESC 로 고르므로
                                        승계 없이는 새 프리셋 저장 = 기능 OFF
```

**변경 파일**
```
models.py              Shift.health_leave_target · RosterConfig.health_leave_enabled/weekend
shift_service.py       _assert_single_target_flag(공통) + off_swap/health_leave 각 래퍼
                       ★ Step 4 의 sleep_off_target 까지 3중이 되므로 헬퍼로 뽑았다.
                         기존 _assert_off_swap_target_valid 는 시그니처·에러문구·동작 동일.
roster_schema.py       ShiftUpdate/ShiftAdd 에 health_leave_target · Config 에 2필드
shift_service_mssql.py shifts 응답 3곳에 health_leave_target
roster_service.py      _PRESERVE_IF_NONE 가드 + 신규 프리셋 승계
routers/roster.py      config 응답 2필드
```

**검증 결과 (dev 실DB)**
```
전량 조회        roster_config 637 · shifts 1457     모델 확장 후에도 정상
신규 컬럼        enabled=1 → 5건 · target=1 → 5건    SQL 주입분과 일치
shifts 응답      52-AN 14건 중 '보건' 1건만 target
저장 보존        미전송 저장 후 True/True 유지 · 행수 637 불변
신규 프리셋 승계  새 config 에 enabled/weekend 승계 확인 → 명시 DELETE · 잔재 0
```

★ `agents_v2/tools/constraint_tools.py` 화이트리스트에는 **넣지 않았다** —
에이전트 쓰기 표면을 넓히지 않기 위함(무가드 setattr 이슈 미해소 상태).
```
범위   보건휴가만. 수면OFF 는 Step 4.
SQL    scripts/leave_auto_assignment_step1.sql   ← 사용자 실행
변경   models.py   Shift.health_leave_target · RosterConfig.health_leave_enabled/weekend
       shift_service.py  _assert_health_leave_target_valid  (off_swap 검증 본뜸)
       roster_schema.py  ShiftBase/RosterConfigBase 필드 노출
순서   ★ SQL 먼저 → 확인 → models.py → 회귀
검증   13그룹 9월 생성 통과 (기준선: 성공 13/13 · 커버리지 미달 0)
리스크 순서를 뒤집으면 기존 조회가 전부 깨진다 (§5.1)
```

### Step 2 — 보건휴가 사전 주입 ✅ **dev 구현·검증 완료 (2026-08-03)**

**결과 — 13그룹 자동 부여**
```
부여 160건 · 부적격 부여 0 · violations 0 · infeasible 0 (13/13)
OFF 미달 1건(별관1/김영미) = §8 리스크 1 재현 (수동 실증 때는 민수연 — 매번 다른 1명)
```
배제가 전부 실측 규칙대로 걸렸다.
```
남성                     제외 ✓
고정근무 (period 3명 + 컬럼전용 3명)  제외 ✓  ← 노미선·안현정·이소미 포함
N전담 6명 (allowed=["N"])           제외 ✓
휴직 5명                            제외 ✓
```

**★ 접점을 `engine_nurses` 확정 직후로 잡아 조건 3종이 자동 충족된다**
```
4552  _overlay_home_profile_asof    fixed_shift 실효값 (period 없으면 컬럼 유지)
4564  _split_fixed_nurses           고정근무자 → engine_nurses 에서 제외        ③
4740  allowed_shifts as-of 오버레이  N전담 판정이 정확해짐
4783  active_range 필터             휴직/퇴사자 제외                          ②
4802  ★ plan_health_leave           shift_type 을 직접 실어 보냄               ①
4881  _build_special_fixed_cells    소비
```

★ N전담 판정은 `is_night_nurse` 컬럼이 아니라 **`allowed_shifts` + `is_n_only_profile()`** 이다.
  `nurses.allowed_shifts` 는 **DB 컬럼이 아니라** 오버레이가 `__dict__` 로 주입하는 런타임 속성이고,
  파이프라인의 team 미지정 분기(4764)도 같은 함수를 써서 판정이 일치한다.
  (실측: `is_night_nurse` 컬럼과 period `allowed_shifts` 값이 16명 전원 동일)

```
신규   services/leave/health_leave_planner.py
         plan_health_leave(db, group_id, year, month, nurses, daily_shift) -> list[dict]
접점   roster_create_service._collect_nurses_and_preferences 직후
         special_fixed_requests.extend(plan_health_leave(...))
로직   ① 대상 선별 (§3.1 + 아래 필수조건 3종)
       ② 후보일 = 평일 (health_leave_weekend=1 이면 전일)
       ③ 정렬: 인력 여유 큰 날 > OFF 다음날 > 분산
       ④ 하루 상한 max(1, 적격자수 // 10) 로 클램프
검증   9월 생성 후 보건휴가 개수 = 적격자 수 · 커버리지 미달 0
리스크 인력이 빠듯한 그룹(별관1 여유 6)에서 OFF 균등이 흔들릴 수 있다 (§7.2 실측 1건)
       → 부여 실패 시 그 사람만 스킵하고 경고 (전체 실패 금지)
```

#### ★ 필수 조건 3종 — 2026-08-03 전 병동 실증에서 확정 (§7.2)

**① `shift_type` 을 반드시 함께 실어야 한다**
`fixed_type_by_cell`([cp_sat_basic.py:2605](roster-back/app/services/cp_sat_basic.py#L2605))은
빌더가 준 `shift_type` 을 **우선** 쓰고, 비면 `code2type` 으로 폴백한다. 그런데
`code2type` 은 `shift_manage` 기반이라 D/E/N/M 뿐이어서 `'보건'` 을 못 찾는다.
→ 타입이 비면 `vacation_types` 판정에서 탈락해 **일반 OFF 로 처리되고 쿼터를 먹는다.**
```python
special_fixed_requests.append({
    "nurse_id": ..., "day": ..., "shift_id": target.shift_id,
    "shift_type": target.type,      # ★ 생략 금지 ('휴가' 또는 '공가')
})
```

**② 휴직자를 제외한다**
휴직 중인 간호사는 근무표에서 가려져 전 셀이 비고 `OFF=0` 이라 주입해도 배치되지 않는다.
실증에서 4명(임유나·박지수·박민혜·김주래)이 헛주입됐다(결과는 무해하나 계획이 어긋난다).
```sql
NOT EXISTS (SELECT 1 FROM nurse_assignment a
            WHERE a.nurse_id = n.nurse_id AND a.reason = N'휴직' AND a.status = 'active')
```

**③ 고정근무 판정은 `resolve_asof` 실효값으로 한다 — period 만 보면 놓친다**
`nurse_allowed_shift_period` 행이 **아예 없으면 컬럼 값이 그대로 유효**하다(캐시 유지 = 안전 동작).
"구간이 있는데 값이 NULL" 인 경우(=NULL 로 덮여 소멸)와 반드시 구분해야 한다.
```
김현우·남혜숙·배윤정   period 행 1건 → period_fixed='M'   정상 배제
노미선·안현정·이소미   period 행 0건 → 컬럼 'M' 이 유효    ★ period 만 보면 적격으로 오판
```
→ 실증에서 이 3명에게 보건휴가가 잘못 부여됐다(M 22→21). 동작 자체는 정상이었으나
   실측 규칙(§3.1 "고정근무X")에 어긋난다. [[feedback_fixed_shift_ssot_period_overrides_column]]

### Step 3 — 진단 연동
```
접점   roster_create_service 의 UndiagProbe / per-nurse MCS
추가   "보건휴가로 인한 커버리지 부족" 원인 카드
       resolution: 해당 보건휴가를 다른 날로 이동 (per-nurse 원클릭)
```
★ 이 브랜치의 기존 resolution 4종(주말휴무 해제 · 월 야간한도 하향 · banned 해제 · allowed 추가)과
같은 형태로 편입한다.

---

## 수면OFF (Step 4~5) — **최후순위**

> 작업량이 가장 크다(상태 테이블 + 확정 훅 2곳 + 2-pass + 백필).
> 보건휴가(Step 2)와 진단 연동(Step 3)이 안정된 뒤에 착수한다.

### Step 4 — 수면OFF 상태 추적 ✅ **dev 완료 (2026-08-03)**

**스키마·배선**
```
DDL      shifts.sleep_off_target · roster_config.sleep_off_enabled/cycle · nurse_night_cycle
값       102243 12그룹 ON (응급실-AN 은 '수면' 코드 없음 → 자동 미사용) · cycle=15
코드     models.py · shift_service(_assert_sleep_off_target_valid) · roster_schema ·
         shift_service_mssql(응답) · routers/roster(config 응답) · _PRESERVE_IF_NONE 4키
훅       publish_roster 의 commit 직전 → upsert_night_cycle_snapshot
회귀     조회·저장보존·신규프리셋 승계 ALL PASS · 보건휴가 설정 13건 불변
```
★ 검증 함수는 Step 1 에서 뽑아둔 `_assert_single_target_flag` 공통 헬퍼를 그대로 썼다
  (off_swap / health_leave / sleep_off 3중 — 예상대로 헬퍼가 값을 했다).

**★★ 백필 전에 파서 결함을 잡았다 — 괄호 표기**
`N(B)5` · `D(B)` 같은 타 팀 지원 표기를 제거하지 않아 그 셀이 통째로 누락됐다.
README 에는 "괄호를 제거해야 한다"고 적혀 있었으나 **구현이 빠져 있었다.**
```
권현은  N11 N12 N13 N14 N(B)15 N(B)1  →  파서는 N14 로 오판
```
단순 개수 오차가 아니라 **N15 도달 판정이 뒤집히는** 문제다.

수정 후 재산출에서 세 가지가 더 나왔고, 전부 **DB 를 레퍼런스로** 해소했다.
```
① 같은 사람이 여러 파일에      (보고용) 14건 / AI본 15건 → DB=14 가 정답
② 동명이인을 병동명으로 못 가름   응급실-RN 김지영 ↔ 응급실-AN 김지영
③ 이름 표기 흔들림            '이  솔'(공백2) · '김지영b' · '황상은(파트장)'

수정 전  170명 · DB 대조 97.1% · 41병동 5명 어긋남
수정 후  227명 · DB 대조 100%  · 미포함 0명
```

**백필 결과 (2026-08 스냅샷)**
```
227행 / 13그룹 · seq_at_end>=15 9명 · pending>0 4명
pending 4명 = 선예원(52-RN) · 노유경 · 유희주 · 이재영(중환자실)  ← §7 이월 검증 대상과 일치
유희주·이재영은 seq_at_end=1 인데 pending=1  ← 두 컬럼이 각각 필요한 이유의 실증
```

**★ 계산 로직 실증 — 7월 앵커를 만들어 8월을 재계산해 대조**
```
seq_at_end 정확도  182/212 (85.8%)   ← 7월 앵커 보유자 한정
pending    정확도  219/227 (96.5%)   ← 수면OFF 부여 판정의 실질 지표
```
불일치의 원인은 로직이 아니다.
```
① 7월 앵커 품질   42병동-AN 은 **7월 근무표가 DB 에 없다**(8월만 존재) → 8명 오차
② 엑셀 연번의 사람 실수  8/251명(3.2%)이 15 순환 규칙을 위반
     김효경 19일 N15 → 20일 N11 (기대 N1)   ·  남수연 2일 N9 → 3일 N9 (연번 반복)
     박재윤 13일 N7 → 28일 N6 (역행)        ·  배지숙 8일 N4 → 19일 N6 (건너뜀)
```
★ 즉 **로직은 실무 규칙과 96.8% 일치**하고, 나머지는 손으로 매긴 연번의 오류다.
  시스템이 앵커를 이어가면 이 실수가 사라지고, 월별 스냅샷이라 오차도 누적되지 않는다.

★ 7월 앵커(207행)는 검증 산출물로 남겨둔다. 42병동-AN 8명은 원본 부재로 부정확하다.

---

#### 원래 계획 (참고)
```
신규   services/leave/night_cycle_service.py
         sync_night_cycle(db, schedule_id)    확정 시 호출
         get_cycle_anchor(db, group_id, nurse_ids, year, month) -> {nurse_id: (seq, pending)}
접점   ★ 확정 경로가 둘이다 — 반드시 양쪽에서 호출
         routers/roster.py:1336  publish_roster
         scripts/import_finalized_roster.py  (알림 회피로 라우터 우회)
백필   8월 엑셀에서 seq_at_end 추출 (스캐너로 170명 확보 완료)
검증   7월 앵커 + 8월 N개수 = 8월 앵커 재현 (94.3% 기준선)
```

### ★ 앵커 갱신 규약 — `issued` 기준 + 연쇄 재계산 (2026-08-04)

앵커는 **마감(issued) 근무표**에서만 만든다. draft 는 확정이 아니므로 반영하지 않는다.

```
publish_roster        발행/재발행 → rebuild_night_cycle_from(그 달)
POST /save            마감본 수정 시에만 → rebuild_night_cycle_from(그 달)
                      ★ /save 는 ScheduleEntry 를 전량 삭제 후 재삽입한다.
                        마감본을 고치면 N 배치가 바뀌므로 앵커도 다시 잡아야 한다.
```

**★ 그 달만 갱신하면 안 된다 — 연쇄가 필요하다**
앵커는 전월 값을 이어받는다(`seq_at_end` · `pending_sleep` · `sleep_off_seq`).
과거 달이 바뀌면 그 이후가 전부 틀어지므로 **(변경월, 이후 모든 마감월)** 을 순서대로 다시 만든다.

**★★ 두 훅 모두 `db.flush()` 가 반드시 먼저다 — 안 하면 조용히 0행이 된다**
세션이 `autoflush=False` 다(`db/client2.py:189`). **같은 함정을 두 곳에서 밟았다.**

| 훅 | flush 없이 부르면 |
|---|---|
| `POST /save` | bulk delete 는 즉시 반영되고 재삽입은 세션에만 → **entries 0건**으로 읽는다 |
| `POST /publish` | `schedule.status='issued'` 가 세션에만 → rebuild 가 **옛 draft** 를 보고 대상 0건 |

실측
```
/save     HTTP 200 인데 앵커 미생성
/publish  13건 전부 200 인데 앵커 28행(직접 호출로 만든 것) 그대로
flush 추가 후  → 앵커 229행 · 그달수령 72명 (독립 기대값 72 와 정확히 일치)
```

`/save` 의 구체적 흐름은 이렇다.
```
① db.query(ScheduleEntry).delete()   bulk delete → **즉시 DB 에서 삭제**
② db.add(entry) × N                  flush 전까지 **세션에만** 존재
③ compute_snapshot 의 db.query()     DB 를 읽으므로 → 0건
```
→ 훅은 **정상 진입하고 예외도 없는데** 스냅샷이 비어 앵커가 안 생긴다.
실측에서 이것 때문에 "코드는 맞는데 API 로는 안 된다"를 한참 헤맸다.

```python
db.flush()                       # ★ 재삽입분을 DB 에 반영한 뒤 읽는다
rebuild_night_cycle_from(...)
```
★ `upsert_night_cycle_snapshot` 은 스냅샷이 0명이면 **warning 을 남긴다**.
  마감 근무표에 entry 가 0건인 것은 비정상이므로 조용히 넘기지 않는다.

★ 검증(실계정 `aiicmc` · 중환자실 8월 868셀)
```
POST /roster/save → HTTP 200 · entries 868 보존 · status issued 유지
8월 앵커 updated_at  09:45:02 → 10:20:23   갱신 확인
```

**★ 같은 달에 issued 가 여러 건이면 최신 1건만 쓴다**
`publish_roster` 가 발행 시 같은 달 기존 issued 를 draft 로 내려 월당 1건을 보장하지만,
과거 데이터나 직접 수정으로 다건이 될 수 있다. 그대로 두면 같은 달을 두 번 전진시켜
seq/pending 이 부풀려진다(실측: 중환자실에서 "6개월 연쇄"로 잘못 잡혔다).

**검증 (중환자실 · 명시 원복)**
```
8월 sleep_off_seq 를 99 로 조작 → rebuild_night_cycle_from(2026, 9)
  9월  99 로 이어받음 ✓
  10월 99 로 이어받음 ✓
  8월은 재계산 대상이 아니라 불변 ✓
  월당 1건 채택으로 "6개월" → "2개월" 정상화 ✓
  앵커·status 전량 원복 확인 ✓
```

★ 재계산은 **엑셀 백필값을 DB 계산값으로 덮는다**(§Step4 의 85.8% 차이가 이것).
  시스템이 일관되게 계산하는 편이 낫다 — 엑셀 연번에는 사람 실수가 3.2% 있다.
  다만 **최초 앵커(2026-07)는 시작점이므로 보존**한다.

---

### Step 5 — 수면OFF 배치 ✅ **dev 완료 (2026-08-03) · 2-pass 대신 1-pass 후처리**

**★ 설계 변경 — 2-pass 를 접었다**
착수 전 "수면OFF 가 무엇을 대체하는가"를 실측했다(2026-08 확정본).
```
수면 받음  57명   OFF 11.00 · 수면 1.00 · 쉼 12.00 · 근무 16.93
수면 없음 171명   OFF 11.24 · 수면 0.00 · 쉼 11.24 · 근무 17.33
```
OFF 는 거의 같고 쉼만 늘었다 → **근무일을 대체한다**(보건휴가와 같은 성질).
OFF 를 치환하면 `countable_off` 가 줄어 off_days 미달이 된다.

★ 근무일 대체라면 **생성 결과의 근무 셀 하나를 바꾸면 되므로 재생성이 불필요**하다.
  `off_swap` 이 이미 같은 위치에서 `generated` dict 를 변환하고 있어 그 옆에 붙였다.
  → 솔버를 두 번 돌리지 않아 생성 시간이 2배가 되는 것을 피했다.

**구현**
```
신규   services/leave/sleep_off_postprocess.py
         postprocess_sleep_off(db, schedule, generated, latest_config)
접점   roster_create_service — postprocess_off_swap 직전
로직   ① 전월 앵커 + 당월 N 으로 연번 복원 → cycle 도달 블록 특정
       ② 후보 = 블록 종료 다음날 ~ 다음 N 블록 시작 전의 **근무일**
       ③ daily_shift 필요 인원을 넘기는 날만 치환 (커버리지 보호)
       ④ 못 고르면 그 사람만 스킵 → pending 으로 다음 달 자동 이월
```

**검증 — 13그룹 9월 생성**
```
수면 44건 · 보건 160건 · violations 0 · infeasible 0
★ 8월 pending 4명(선예원·노유경·유희주·이재영) 전원 9월 수령 ✓
OFF 미달 1건 = 별관1/이성애(수면 0 · 보건 1) → 수면OFF 와 무관.
   §8 리스크 1(별관1 OFF 균등)의 재현 — 매번 다른 1명이 걸린다(민수연→김영미→이성애).
```

**★ N전담자는 수면OFF 를 받지 못한다 — 실무와 일치한다**
10월 실테스트에서 9월 이월 19명 중 9명이 미수령이었고, 원인을 파보니 결함이 아니었다.
```
응급실-AN 3명   '수면' 코드 없는 그룹                     → 정상
N전담 6명       N 블록 사이가 전부 OFF → 치환할 근무일 없음 → 스킵
```
★ 처음에는 "`allowed_shifts=[\"N\"]` 설정이 실제와 다르다"고 오판했다.
  7·8월을 **합산**해서 본 착시였다. 월별로 가르면 설정이 정확하다.
```
7월 (period_allowed=[])     N 4~7 · D 4~7 · E 6~10 · 수면 0~1 · 보건 1
8월 (period_allowed=["N"])  N 14~15 · D 0 · E 0 · 수면 0 · 보건 0
```
8월 1일부터 진짜 N전담으로 전환됐고, **실무도 8월엔 수면·보건을 주지 않았다.**
구현이 실무와 정확히 일치한다. 못 받은 분은 `pending` 으로 보존되어
전담이 해제되면 소급 부여된다. [[feedback_check_existing_reference_before_measuring]]

**★ 충족률 69.8% 는 결함이 아니다 — 스킵분이 100% 이월된다**
```
대상 63 · 배치 44 · 스킵 19   (커버리지 여유 없음 16 + 응급실-AN 코드없음 3)
9월 말 pending                19명   ← 스킵과 정확히 일치
  41-RN 3→3 · 42-RN 3→3 · 51-RN 5→5 · 52-AN 1→1 · 52-RN 2→2
  별관1 1→1 · 응급실-AN 3→3 · 호스피스 1→1 · 중환자실 0→0
```
★ 커버리지를 깨면서까지 부여하지 않고, 못 준 만큼 다음 달로 넘긴다.
  실무도 같다 — 8월에 4명이 이월됐고 9월에 전원 받았다.

★ 스킵의 직접 원인은 D/E/N 필요 인원이 타이트하다는 것이다
  (`m_count` 는 전 그룹 0 이라 **M 근무일은 애초에 뺄 수 없다**).

---

#### 원래 계획 (2-pass · 참고)
```
1차 생성  → N 배치 확정
계산      → 앵커(seq_at_end) + 당월 N 누적으로 15 도달 지점 탐색
           + pending_sleep 이 있으면 첫 N블록 시작 전에 우선 배치
날짜 결정 → N15 가 속한 블록 종료 후 ~ 다음 블록 시작 전
2차 생성  → 그 셀을 fixed_cell(휴가)로 넣고 재생성
```
★ 솔버 제약으로 직접 넣는 대안은 **비권장** — "누적 15 도달 후 5일 내" 는 조건부 제약이라
모델이 복잡해지고 infeasible 위험이 커진다. 실측 간격도 1~12일로 흔들려 엄격한 제약과 맞지 않는다.

---

## 7. 검증 계획

| 단계 | 방법 | 기준 |
|---|---|---|
| 회귀 | 13그룹 9월 생성 | 성공 13/13 · 커버리지 미달 0 |
| 보건 | 생성 결과 보건휴가 수 | 적격자 수와 일치 · 평일 비율 90%+ |
| 수면 | 앵커 재현 | 7월 앵커 + 8월 N = 8월 앵커 (94%+) |
| 이월 | pending 4명 | 선예원·노유경·유희주·이재영이 9월에 부여받는지 |
| 진단 | infeasible 유도 | 원인 카드에 휴가가 지목되는지 |

★ 기준선(현재 상태): 13그룹 9월 생성 **성공 13/13 · 진짜 1N 3건**(폴백 확률적 결함).

### 7.1 사전 실증 — 별관1 (최악 조건) ✅
설계 착수 전, **수급 여유가 가장 적은 그룹**에 보건휴가를 수동 투입해 파이프라인을 검증했다.

```
투입   fixed_wanted_entries 에 '보건' 6건 (9/2·8·14·17·22·28 · 하루 1명 · 전부 평일)
       적격자 = 이정희A·민수연·이성애·김영미·김현경·박재윤 (§3.1 규칙 적용 결과)
조건   엔진 10명 · off_days 10 · 근무가능 200 · 수요 194 → 여유 6
       보건휴가 6개 투입 시 여유 0 (최악)
```

결과
```
생성        성공 · 330건
보건휴가     6건 전부 지정한 날짜에 배정 ✓
커버리지     미달 0 / 90 슬롯 ✓
OFF        10개 × 8명 · 9개 × 2명   ← 균등 훼손 (유일한 부작용)
```

**검증된 것**
1. `special_fixed_requests` 경로가 **정상 동작**한다 — 솔버 수정 없이 휴가가 들어간다.
2. 여유 0 에서도 **커버리지는 안 깨진다**.
3. 대가는 **OFF 균등**이다 → §8 리스크 1.

★ 로그의 `스킵(특수코드 중복=6)` 은 **정상**이다.
   보건휴가는 `special_fixed_requests`(roster_create_service.py:199) 에서 이미 처리되고,
   `fixed_wanted` 하드고정 루프(5219)는 중복을 막으려 건너뛴다. 미적용이 아니다.

※ 투입 데이터·생성 draft 는 검증 후 전량 삭제했다.

### 7.2 ★ 전 병동 실증 — 13그룹 159명 (2026-08-03) ✅

Step 2 착수 전, **prod 전수를 dev 로 마이그**한 뒤 13그룹 전체에 보건휴가를 투입해
설계 전제 3가지를 실측했다. dev 환경: office 102243 prod 미러(16/16 행수 일치).

```
투입   fixed_wanted_entries 에 '보건' 159건 (적격자 월 1개 · 평일 분산)
       주말 허용 3그룹(41-RN·별관1·52-AN)만 주말 포함 풀
대조군  주입 전 동일 조건 9월 생성 (gen9) — 13그룹 violations 0 · infeasible 0
```

결과
```
주입 159 → 배치 155 · 날짜준수 155/155 · violations 0 · infeasible 0 (13/13)
```

| 전제 | 결과 |
|---|---|
| ① OFF 쿼터 미소비 | **154/155** — 일반 근무자 OFF 10 유지 |
| ② 근무일 대체 | 확인 — 일반 20→19 · 고정M 22→21 |
| ③ 커버리지 유지 | violations 0 · infeasible 0 · 13그룹 전부 |

**미배치 4건 = 전부 휴직자** (임유나·박지수·박민혜·김주래).
근무표에서 가려져 `OFF=0` 이라 배치 대상이 아니다 → **정상**. Step 2 필수조건 ②의 근거.

**OFF 미달 8건 분해** — `off_days` 기준으로 다시 보면 실제 회귀는 1건뿐이다.
```
4건  휴직자 (OFF=0)                      → 정상
3건  고정M+주말휴무 (원래 OFF=8)          → 주입 무관 · 근무 22→21 로 정상 대체
1건  별관1/민수연 OFF 10→9                → ★ 유일한 실제 회귀 = 실효 0.6%
```
★ "OFF 가 줄었다" 만으로 판정하면 12건이 걸린다. 대부분은 **베이스라인이 off_days 를 초과했다가
정규화된 것**(12→10, 11→10)이라 무해하다. **최종 OFF < off_days** 로 판정해야 한다.
[[feedback_check_existing_reference_before_measuring]]

**민수연 사례 — §7.1 이 예견한 OFF 균등 훼손의 재현**
```
전: OFF 10 · 보건 0 · 근무 20   (쉼 10)
후: OFF  9 · 보건 1 · 근무 20   (쉼 10)   ← 근무가 아닌 OFF 를 대체
```
같은 그룹의 다른 5명은 전부 `OFF 10 · 보건 1 · 근무 19` 로 정상.
별관1은 11명 중 6명 주입으로 인원이 가장 빠듯한 조건 → **새 결함이 아니라 §8 리스크 1의 재현.**

※ dev 검증 데이터는 사용자 지시로 **보존**한다(정리 불필요).

---

## 8. 리스크 / 미확인

### 리스크
1. **인력 빠듯한 그룹 — OFF 균등이 깨진다** (실증 완료, §7.1 · 전 병동 재현 §7.2)
   별관1 9월 수급 여유 6(엔진 10명×20일=200 vs 수요 194)에 보건휴가 6개를 넣으면 여유가 0이 된다.
   **생성은 성공하고 커버리지도 안 깨지지만, OFF 가 10→9 로 밀리는 인원이 생긴다.**
   → 수급 여유 < 적격자 수 인 그룹은 **부분 부여** 또는 daily_shift 하향으로 대응.
   ★ `off_first` 설정 문제가 아니다(§8.1 참조).
2. **2-pass 비용** — 생성 시간이 약 2배. 13그룹 기준 현재 20~40초/그룹.
3. **import 경로 누락** — Step 3 에서 양쪽 배선을 빠뜨리면 앵커가 갱신되지 않아
   다음 달 계산이 통째로 어긋난다.

### 8.1 ★ `off_first` — 이름과 동작이 어긋난다 (건드리지 말 것)
이름은 "OFF 우선"처럼 읽히지만 **`True` 가 daily 커버리지 우선**이다.

```python
# cp_sat_basic.py:3341
# off_first=True: OFF range는 SOFT objective(가중치)로만 유도, HARD 제거.
# (사용자 명세: off_days 무시 + daily 커버리지 우선 → OFF 균등은 차순위)
```

| | `off_first=True` | `off_first=False` (현재 전 13그룹) |
|---|---|---|
| 우선순위 | **daily 커버리지** | **off_days(월 OFF 수)** |
| 커버리지 | `assigned <= need` **하드**(초과 금지·3280) | 근무 oversupply 허용 |
| `off_days` | **무시** | **준수**(OFF cap tight clamp) |
| OFF 균등 | HARD 제거 · SOFT only(weight −100000) | cap 으로 사실상 고정(weight −200) |

★ `True` 에서 "OFF oversupply" 가 되는 건 `assigned <= need` 로 초과 근무를 막아
   **남는 셀이 OFF 로 흘러간 결과**일 뿐, OFF 를 우선한다는 뜻이 아니다.

**이 병원은 월 OFF 수 우선(8월 실측 전원 11개·σ=0.00)이므로 `off_first=False` 가 맞다.**
현재 설정이 이미 옳다 — **변경 불필요.**

### 미확인
1. **감정휴가 주기** — 분기 1회인지 반기 1회인지. 6월 이전 파일 필요. **병원 확인 대상**.
2. **보건휴가 날짜 선택의 병원 내부 기준** — 실측은 "평일·OFF 다음날 선호·1~2명 분산"까지만
   확인됐고, 그 안에서 어느 날을 고르는지는 재량으로 보인다.
3. **미등록 인력 3명**(김경근·강성진·최종현) — 근무표에 31셀씩 누락 중이라
   해당 그룹의 수급 계산이 실제보다 낙관적이다.

---

## 9. 참고 — 이번 분석 산출물

```
scratchpad/rs/scan_leave_patterns.py   엑셀 전량 스캐너 (29시트 · 550레코드)
scratchpad/rs/leave_scan.json          원시 스캔 결과
scratchpad/rs/leave_idx.json           중복 제거 인덱스 (445 레코드)
scratchpad/rs/night_cycle_seed.json    8월 말 seq_at_end 앵커 (170명 · DB 매칭)
```

★ 스캐너 주의점 — **헤더의 날짜 열과 데이터의 날짜 열이 한 칸 어긋난 시트가 있다**(41병동 7월).
행마다 근무코드가 가장 많이 잡히는 시작열을 고르는 방식으로 보정했다.
이를 놓치면 연번 연속률이 25.7%로 잘못 나온다(실제 94.7%).
