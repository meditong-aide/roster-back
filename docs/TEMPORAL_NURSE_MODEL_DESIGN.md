# 시점(時點) 기반 간호사 모델 설계

> 월별로 달라지는 간호사 속성(팀·등급·근무제한·주말off·월한도)과 존재 이벤트(파견·휴직·병동이동·퇴사),
> 프리셉터 관계를 **시점 개념**으로 표현하기 위한 설계. 작성 기준일 2026-06-09.

## 0. 배경 / 문제

현재 `nurses` 테이블은 **단일 스냅샷**이라 "6월 팀 vs 7월 팀"을 표현할 수 없다. 그로 인해:

- **팀 재분배 버그**: `재분배 apply`가 `NurseAssignment(속성변경, target_team_id, 발효=7/1)` 이벤트만 만들고 `Nurse.team_id`는 발효 cron 전까지 안 바뀜 → "실제 팀이 안 바뀐다".
- **9B 생성 infeasible**: 생성기가 stale `Nurse.team_id`(team None 9명)를 읽어 팀 제약이 깨짐.
- **일반 간호사 group 변경 시**: `nurses.group_id`가 발효 시점에 한 번 flip되는 단일값이라 (a) 이동 후 **과거 달 근무표가 "내 근무표"에서 소실**, (b) 이동 전 **미래 달 원티드가 옛 병동에 오배치**.

근본 원인 = **소비 단위(월별 근무표)와 데이터 모델(시점 없는 단일값)의 불일치.**

## 1. 설계 원칙

1. **세 종류를 분리한다 — 모양이 다르다.**
   | 종류 | 예 | 모양 | 저장 |
   |---|---|---|---|
   | 존재/위치 이벤트 | 파견·휴직·병동이동·퇴사 | 구간(start~end, 열린 끝 가능) | `nurse_assignment` (구간) |
   | 시점 속성 | team·grade·shift제한·weekend_off | 기간 동안 유효한 값 | `nurse_month_profile` (월 단위) |
   | 관계 | preceptor ↔ preceptee | 기간 있는 양자 관계 | `preceptorship` (관계+기간) |
2. **시간 grain = 월(月).** 근무표·원티드·월한도가 모두 월 단위로 소비되므로, 임의-날짜 구간이나 "현재값+발효cron"이 아니라 `(nurse_id, year, month)` 키를 쓴다. → 발효 타이밍 문제 소멸, 겹침/공백 버그 원천 차단.
3. **`nurses` = 캐시.** "오늘 기준 현재값" 비정규화 캐시(시간 안 따지는 화면·싼 조회용). **진실의 원천 = temporal 테이블.** 야간 reconcile로 동기화.
4. **단일 리졸버.** 모든 화면·생성기가 `nurse_presence_in_month` / `resolve_nurse_as_of` 한 곳을 통해 시점 해석. 엔드포인트마다 다른 규칙 금지.
5. **append-only.** 퇴사/종료도 하드 삭제 금지. 과거 근무표는 그 달 데이터를 영구 참조.
6. **Freeze-on-plan (과거 동결).** 근무표를 생성·발행한 달은 그 시점의 간호사 속성(team/grade/shift_rule/…)을 `nurse_month_profile`에 **동결**한다. 이후 `nurses`(현재값)가 바뀌어도 그 달은 동결값으로 보존된다. → "profile 없으면 `nurses` 폴백" 규칙이 **과거를 훼손하지 않게** 만드는 핵심. (예: 7월에 팀 1→2 바뀌어 `nurses`=2가 돼도, 이미 동결된 6월 profile=1 이라 6월 근무표는 1로 유지)

---

## 2. DB 테이블 구조

### 2.1 유지 (그대로)

- `groups`, `teams(office_id, group_id, team_id PK)`, `shifts`, `schedules`/`schedule_entries`.
- `nurse_monthly_limits` — **이미 (nurse, group, year, month) 월 단위.** 본 모델의 시점 패턴과 정합. 유지.
- `wanted_requests` / `nurse_shift_requests` / `fixed_wanted_entries` — 이미 month + group_id 보유. **단, 제출 시 group 귀속 규칙을 §3·§4.4로 교정.**

### 2.2 유지하되 **역할 축소**: `nurse_assignment`

존재(위치/가용) 이벤트 **전용**으로 좁힌다. 속성 변경(team/grade/shift)을 여기 얹지 않는다.

| 컬럼 | 비고 |
|---|---|
| id, nurse_id, office_id | |
| source_group_id, target_group_id | 병동이동/파견의 출발·도착 |
| start_date, expected_end_date, end_date | 구간(end null = 열림) |
| reason | `파견` / `휴직` / `병동이동` / `퇴사` |
| status | `active` / `completed` / `cancelled` |
| ~~target_team_id, target_grade, target_shift_types, kind='permanent_change'~~ | **deprecate → `nurse_month_profile` 로 이관 (Phase 4)** |
| payload, note, created_at, updated_at | |

> `퇴사`는 reason='퇴사', end_date=퇴사일, target_group_id=null 로 표현(또는 별도 `resignation_date` 유지 — §8 결정).

### 2.3 신규: `nurse_month_profile` (시점 속성)

근무표가 읽는 **월 단위 속성**. 함께 바뀌는 로스터-결합 속성을 한 행에 묶는다.

```sql
CREATE TABLE nurse_month_profile (
    nurse_id    VARCHAR(50)  NOT NULL,
    year        SMALLINT     NOT NULL,
    month       TINYINT      NOT NULL,
    group_id    VARCHAR(50)  NOT NULL,         -- 그 달의 소속 병동(이동 시 §3 split 규칙)
    team_id     INTEGER      NULL,             -- FK (group_id, team_id) -> teams
    grade       INTEGER      NULL,
    shift_rule  JSON         NULL,             -- 허용 shift 목록(현 is_night_nurse 대체). [] = N전담 해제
    weekend_off TINYINT      NULL,             -- 현 is_weekend_off 대체
    source      VARCHAR(20)  NOT NULL DEFAULT 'inherited',  -- inherited|carry_forward|edited|redistribute|frozen
    note        TEXT         NULL,
    created_at  DATETIME, updated_at DATETIME,
    PRIMARY KEY (nurse_id, year, month),
    FOREIGN KEY (group_id, team_id) REFERENCES teams(group_id, team_id)
);
CREATE INDEX ix_nmp_group_ym ON nurse_month_profile (group_id, year, month);
```

설계 노트:
- **델타만 저장**: 행이 없으면 base(`nurses`) 또는 직전 월에서 상속. 대부분 간호사는 행이 없다.
- `(nurse_id, year, month)` PK → 한 달 = 한 행 → **겹침/공백 불가**(임의 구간보다 단순).
- **Freeze 행(`source='frozen'`)**: 근무표 발행 시 그 달 속성을 박제(§4.6). 폴백 안전성의 토대.
- `monthly_limit`은 `nurse_monthly_limits`에 그대로(컬럼 수가 많아 분리 유지). 같은 grain이라 리졸버에서 동일하게 합친다.

### 2.4 신규: `preceptorship` (관계 + 기간)

```sql
CREATE TABLE preceptorship (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    preceptor_id VARCHAR(50) NOT NULL,   -- FK nurses
    preceptee_id VARCHAR(50) NOT NULL,   -- FK nurses
    group_id     VARCHAR(50) NOT NULL,
    start_date   DATE NOT NULL,
    end_date     DATE NULL,              -- null = 진행중. 병동이동 발효 시 자동 종료
    status       VARCHAR(10) NOT NULL DEFAULT 'active',
    note         TEXT NULL,
    created_at   DATETIME, updated_at DATETIME
);
CREATE INDEX ix_prec_preceptee ON preceptorship (preceptee_id, start_date);
CREATE INDEX ix_prec_preceptor ON preceptorship (preceptor_id, start_date);
```

- 현 `nurses.preceptor_id` 단일 컬럼은 **캐시로 강등**(현재 진행중 프리셉터). 진실 = 이 테이블.
- 한 프리셉터가 N명 프리셉티를 가질 수 있고, 기간이 있으므로 단일 컬럼/Assignment로는 표현 불가 → 별도 테이블이 거버넌스상 정답.

### 2.5 `nurses` (캐시로 명시)

`group_id, team_id, grade, is_night_nurse, is_weekend_off, preceptor_id` 등은 **"오늘(as-of today) 기준 현재값" 캐시**로 재정의. 진실은 `nurse_month_profile`(+ `nurse_assignment` + `preceptorship`). reconcile cron이 동기화.

---

## 3. 정책 매트릭스

**핵심 원칙**: 가시성·접근은 **"그 간호사의 assignment-유도 존재가 (병동, 월)과 하루라도 겹치는가"** 로 결정. **발효 여부·현재 날짜 무관**, assignment가 **등록(active)된 순간부터**. (원티드는 미리 짜므로 "현재"가 아니라 "예정된 소속" 기준이어야 함.)

`present(nurse, 병동, 월)` = 그 월에 해당 병동에서 1일 이상 존재(근무 가능/예정).

| 케이스 | 근무자관리(목록) | 원티드 조회 | 원티드 제출 | 내 근무표 | 전체 근무표 |
|---|---|---|---|---|---|
| 정상 소속 | ✅ | ✅ | ✅ | ✅ | ✅(소속 병동) |
| **병동이동(미래 예정)** target 병동 | ✅(예정 표시) | ✅ | ✅ | ✅(해당 월) | ✅ |
| 병동이동 후 source 병동 | 과거 달만 ✅(회색) | 과거 달 ✅ | ❌(이미 떠남) | 과거 달 ✅ | 과거 달 ✅ |
| **파견(기간)** target | 기간 겹친 달 ✅ | ✅ | ✅ | ✅ | ✅ |
| 파견 복귀 후 | source로 자동 복귀 | | | | |
| **휴직(기간 유/무)** | 겹친 달 ✅(회색) | ✅(보기) | ❌(안 짬) | ✅(빈/표시) | ✅ |
| **퇴사** | 퇴사 전 ≥1일 달까지 ✅(회색) | 과거 ✅ | ❌ | 과거 ✅ | 과거 ✅ |

규칙 요약:
- **근무자 관리**: 그 월에 ≥1일 존재 → 표시(떠난/끝난/퇴사자는 회색 + 사유 배지).
- **원티드/근무표**: 예정 포함 `present(nurse, 병동, 월)` 이면 접근. 트리거 = assignment 등록 순간(미래 start 무방, cancelled 제외).
- **보기 vs 제출 분리**: 휴직은 "보기 OK, 제출 ❌". 떠난 source/퇴사 미래도 제출 ❌.

### 3.1 월 중간 이동(split month) — per-day 귀속

7/15에 A→B면 7월은 A(1–14)+B(15–31). "≥1일 겹침"이라 7월에 **A·B 양쪽에 표시**되고, **per-day로 자연 처리**:
- 솔버 `active_window/blocked_days`: A 근무표는 15–31 차단, B 근무표는 1–14 차단 → 각 병동에 본인이 **자기 날짜만큼** 들어감.
- 원티드(`nurse_shift_requests`는 per-day): **그 날 활성 병동**에 귀속.
- **내 근무표(split)**: A세그먼트 + B세그먼트를 **하나의 달력으로 머지**해 보여줌(§5).

→ "한 달 = 한 병동"으로 단순화할 필요 없음. `nurse_month_profile.group_id`는 "월 1일 소속(대표값)"으로 두고, 실제 일자 귀속은 assignment 구간으로 해석.

---

## 4. 백엔드

### 4.1 리졸버 (단일 진실)

```python
# 시점 해석의 SSOT. 모든 화면/생성기가 이걸 통해서만 시점 속성을 읽는다.

def nurse_presence_in_month(db, nurse_id, year, month) -> list[Presence]:
    """그 월에 이 간호사가 존재하는 (group_id, day_range, role) 목록.
    base 소속 + 활성 assignment(파견/병동이동/휴직/퇴사) 구간을 month 와 교집합."""

def resolve_nurse_as_of(db, nurse_id, year, month) -> NurseAsOf:
    """그 월의 유효 속성: base(nurses) ⊕ nurse_month_profile(override)
    ⊕ nurse_monthly_limits ⊕ preceptorship(active). team/grade/shift_rule/limit/preceptor 포함."""

def group_members_in_month(db, group_id, year, month) -> list[NurseAsOf]:
    """그 병동·그 월의 명단 = present(_, group_id, month) 인 간호사 + as-of 속성.
    근무자관리/생성기/전체근무표 공용."""
```

### 4.2 엔드포인트 (변경/신규)

| 엔드포인트 | 변경 |
|---|---|
| `GET /nurses` (근무자관리 목록) | `group_members_in_month(group_id, year, month)` 사용. `year/month` 파라미터 추가, 존재 플래그(active/inbound/outbound/leave/resigned) 부여 |
| `GET /nurses/{id}` (사이드 프로필) | as-of 속성으로 응답(team/grade 등 그 월 값) |
| `GET /roster/issued_roster/me` | **home 단일값 → `nurse_presence_in_month` 기반.** split 시 세그먼트 머지 |
| `GET /roster/issued_roster` (전체) | `present(caller/nurse, group, month)` 로 접근 판정(±6일 윈도우 임시 로직 대체) |
| `GET /wanted/...` (조회/제출) | 제출 시 **대상 월의 as-of 병동**에 귀속(§4.4) |
| `GET /teams` / 재분배 preview·apply | apply가 **`nurse_month_profile`(team_id)** 에 기록(아래) |
| (신규) `GET /nurses/{id}/timeline` | assignment+profile+preceptorship 이력 조회(감사/디버그) |

### 4.3 재분배 apply 재설계

- **병동 변경** → `nurse_assignment(reason='병동이동', start, end)` (존재 이벤트). 그대로.
- **팀/등급 변경** → ~~`create_permanent_change`(NurseAssignment 속성변경)~~ → **`nurse_month_profile` upsert**(대상 월부터). 발효 cron 불필요.
- 프리셉터 종료 → `preceptorship.end_date` 세팅.

### 4.4 생성기·원티드 read 전환 (가장 중요)

- **생성기**: `Nurse.team_id`(캐시) 직접 읽기 → `resolve_nurse_as_of(req.year, req.month)` 로 전환. (9B infeasible의 stale 캐시 원인 해소)
- **원티드 제출**: group_id 를 `resolve_nurse_as_of(target month).group_id`(split이면 그 날 병동)로 귀속. "7월치인데 옛 병동 A 저장" 방지.

### 4.5 발효 cron 역할 재정의

`flush_pending_permanent_changes`는 **시점 속성의 진실이 아니라 `nurses` 캐시 동기화** 용도로만. 진실은 profile/assignment. reconcile은 "캐시 == as-of-today 값" 검증.

### 4.6 Freeze-on-plan (과거 동결)

`nurse_month_profile`은 기본적으로 **희소(sparse)** — 바뀐 사람만 행이 있고, 없으면 `nurses`(현재값) 폴백. 문제는 **영구 변경**: `nurses`가 새 값으로 갱신되면, profile 행이 없는 **과거 달**이 폴백으로 새 값을 끌어와 과거가 틀어진다.

**해법 — 발행(issued) 시점에 그 달을 동결한다:**

```python
def freeze_month_profiles(db, group_id, year, month):
    """근무표 발행 시, 그 달 명단 전원의 as-of 속성을 nurse_month_profile 에
    source='frozen' 으로 upsert. 이미 frozen 행이 있으면 보존(멱등)."""
    for nurse in group_members_in_month(db, group_id, year, month):
        upsert_profile(
            db, nurse_id=nurse.nurse_id, year=year, month=month,
            group_id=nurse.group_id, team_id=nurse.team_id, grade=nurse.grade,
            shift_rule=nurse.shift_rule, weekend_off=nurse.weekend_off,
            source="frozen", if_absent_only=True,
        )
```

- **호출 지점**: `publish_roster`(발행) 시. draft 생성 시엔 동결하지 않음(아직 확정 아님) — 발행이 "과거가 됨"의 경계.
- **멱등**: 이미 `frozen` 행이 있으면 덮어쓰지 않음. 재발행 정책은 §8-결정.
- **효과**: 발행된 달은 이후 `nurses`/profile 변경과 무관하게 그 시점 명단·속성으로 영구 재현. 미발행 미래 달은 profile 없이 `nurses`(현재값)로 자연 폴백.

---

## 5. 프론트

1. **월 컨텍스트가 시간축**: 사용자는 한 번에 한 달만. interval/이력 UI 미노출. "7월 팀 편집" = 7월 근무표 안에서.
2. **carry-forward**: 새 달 진입 시 직전 달/ base 상속, 바뀐 것만 편집(델타). "이전 달 복사" 버튼.
3. **근무자관리 — 월 선택 신규 추가**: 지금은 "현재 소속" 명단만 보여주지만, **월 셀렉터(드롭다운/탭)를 새로 추가**한다. 선택 월에 따라 `GET /nurses?group_id&year&month` 로 그 달 명단을 다시 불러오고(존재 플래그: active/inbound/outbound/leave/resigned), 떠남·휴직·퇴사자는 회색 + 사유 배지로 표시. 발행된 과거 달은 동결 명단(§4.6)을 그대로 재현.
4. **원티드 제출 귀속**: 제출 UI가 "대상 월의 소속 병동"을 as-of로 표시하고 거기에 붙임. 휴직 월은 제출 버튼 비활성(보기만).
5. **내 근무표(split)**: 두 병동 세그먼트를 하나의 달력으로 머지 렌더.
6. **그룹키 일관**: 모든 조회가 selectedGroupId 명시(이미 진행한 VERSIONS 통일과 동일 원칙). switch-group 토큰 의존 제거 방향.

---

## 6. 마이그레이션 순서 (점진 — 빅뱅 금지)

| Phase | 내용 | 검증 |
|---|---|---|
| **1. team** | `nurse_month_profile` 생성 + 현재 `nurses.team_id`로 당월 backfill + 리졸버 + **생성기 team read 전환** + 재분배 apply가 profile에 기록 | 9B 7월 재생성이 재분배 팀을 반영하는지 |
| **2. grade/shift_rule/weekend_off** | profile 컬럼 확장 + backfill + 생성기 read 전환 | 회귀: 기존 달 생성 결과 동일 |
| **3. preceptorship** | 관계 테이블 + `nurses.preceptor_id` backfill + 종료 자동화 | 프리셉터 N:1, 기간 |
| **4. assignment 정리** | `nurse_assignment`에서 target_team_id/grade/속성변경 kind deprecate(읽기 중단 → 컬럼 제거) | 더 이상 참조 없음 |
| **5. 일반간호사 view** | `/me`·원티드 제출을 presence 기반으로 전환 | 이동 전/후 6·7월 매트릭스(§3) 통과 |

각 phase: backfill → 이중読み(캐시+profile 비교 로그) → read 전환 → 캐시 강등. 롤백 = read 소스 되돌리기.

---

## 7. 조심할 것 (함정)

1. **EAV(범용 attribute_history) 금지** — 타입/ FK/제약/질의 단순성 상실. 관심사별 타입드 테이블 유지.
2. **캐시를 진실로 삼지 말 것** — 생성기는 반드시 as-of 리졸버. (현 9B infeasible 원인)
3. **단일 진실 + reconcile** — nurses 캐시 drift 방지.
4. **하드 삭제 금지** — 과거 근무표 보존. 퇴사/종료는 end만.
5. **반쪽열린 구간 [from, to)** + month PK로 겹침/공백 차단.
6. **원티드 귀속 시점 확정** — 제출 시 대상 월 병동 고정.
7. **info 노출** — 일반 간호사에 미래 target 병동 "전체 근무표"를 언제부터 보일지 정책 명시.
8. **성능** — as-of 해석은 인덱스 한 방((nurse_id, year, month)).
9. **MSSQL 전환** — 현재 `feat/mssql-transition` 진행 중. JSON/AUTOINCREMENT/UPSERT 문법 방언 차이 주의.
10. **Freeze 타이밍/멱등** — 동결은 **발행(issued) 시점**에만(draft는 미동결). 이미 `frozen` 행은 보존(멱등). 재발행 시 동결값을 최신으로 덮을지 vs 보존할지 정책을 §8에서 확정(기본 권장: 보존, 명시적 재동결만 갱신).

---

## 8. 미해결 / 결정 필요 (Open Questions)

1. 퇴사를 `nurse_assignment(reason='퇴사')`로 통일할지, `nurses.resignation_date` 유지할지.
2. split 월의 "내 근무표" 머지 UI: 한 캘린더 통합 vs 병동별 탭.
3. 미래 target 병동 "전체 근무표" 노출 시작 시점(assignment 등록 즉시 vs 승인 후).
4. `nurse_month_profile`을 wide 한 행 유지 vs grade/shift를 별도 테이블로 추가 분리(변경 빈도 차이 시).
5. 휴직 중 원티드: "보기만"으로 충분한지, 부분 휴직(월 일부)은 어떻게.
6. **재발행 시 동결 갱신 정책**: 이미 발행→동결된 달을 다시 발행할 때 frozen profile을 보존(과거 불변)할지, 최신값으로 재동결할지. (기본 권장: 보존; 운영자가 명시적으로 "재동결"할 때만 갱신)
