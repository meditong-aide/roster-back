# 시점(時點) 기반 간호사 모델 설계 (v3)

> 월별/기간별로 달라지는 간호사 속성(팀·등급·근무제한·주말off)과 존재 이벤트(파견·휴직·병동이동·퇴사),
> 프리셉터 관계를 **시점 개념**으로 표현하기 위한 설계.
> v2→v3 변경: **freeze/스냅샷 폐기 → "완전 타임라인"**, gap 규칙 확정, 근무자관리 표시(월선택+그레이아웃 제거+소속 필터+상태 배지) 확정.
> 최종 갱신 2026-06-09.

## 0. 배경 / 문제

`nurses` 단일 스냅샷이라 "6월 팀 vs 7월 팀", "3주차까지 D / 4주차부터 D·E", "월중 병동이동 시 병동별 팀/등급" 같은 시점 변화를 표현 못 한다. 그로 인해:
- 팀 재분배가 발효 cron 전까지 `Nurse.team_id`에 반영 안 됨.
- 9B 7월 생성이 stale `Nurse.team_id`(team None)를 읽어 infeasible.
- 일반 간호사 group 변경 시 과거 근무표 소실·미래 원티드 오배치.

> **폐기 이력**: v1 month-grain(`nurse_month_profile`)은 월중 날짜 경계 변화를 못 담아 폐기 → **effective-dated 구간**(v2). v2의 freeze/스냅샷은 "과거를 계속 유연하게 보여줘야" 하는 요구와 충돌해 폐기 → **완전 타임라인**(v3).

## 1. 설계 원칙

1. **세 종류 분리 — 모양이 다르다.**
   | 종류 | 예 | 모양 | 저장 |
   |---|---|---|---|
   | 존재/위치 이벤트 | 파견·휴직·병동이동·퇴사 | 구간(start~end) | `nurse_assignment` |
   | 시점 속성 | team·grade·shift제한·weekend_off | **effective-dated 구간** | period 테이블 3종(신규) |
   | 관계 | preceptor ↔ preceptee | 기간 있는 양자 관계 | `preceptorship`(신규) |
2. **per-day 근무 희망은 "원티드"이지 속성이 아니다.** "2일·3일만 D 달라" = `nurse_shift_requests`(소프트) / `fixed_wanted_entries`(고정셀) — **이미 per-day로 존재.** period가 재구현하지 않는다.
3. **시점 속성 = effective-dated 구간 `[valid_from, valid_to)`.** 규칙: **겹침 금지(overlap ✗), gap(미지정) 허용, 변경은 close-before-open**(옛 구간 닫고 새 구간 열기, 삭제 금지).
4. **정의는 재사용, 기간만 신규.** team 정의=`teams`, grade 스케일=`roster_grade_config`. per-nurse "누가 언제 어느 팀/등급"만 신규.
5. **`nurses` = 현재값(오늘) 캐시(DB).** **과거 진실은 캐시가 아니라 닫힌 구간(타임라인)이 보장**한다. 시점이 중요하면 리졸버를 본다. 야간 reconcile 동기화.
6. **단일 리졸버.** 모든 화면·생성기가 같은 리졸버로 시점 해석.
7. **완전 타임라인(append-only) — freeze 안 씀.** 변경 = **옛 구간 close + 새 구간 open**, 옛 구간 삭제 금지. 과거를 물으면 그 날을 덮는 닫힌 구간을 읽으면 항상 정확. 스냅샷이 없어 **언제든 과거 구간을 수정하면 그 시점 view가 바로 갱신**(유연성). → freeze/박제 불필요.

## 2. DB 테이블 구조

### 2.1 유지
`groups`, `teams`, `shifts`, `schedules`/`schedule_entries`, `nurse_monthly_limits`(월 단위 유지), `wanted_requests`/`nurse_shift_requests`/`fixed_wanted_entries`(per-day).

### 2.2 정의 재사용 (period의 FK/해석 타깃)
- `teams (office_id, group_id, team_id PK; team_name, min_shift, …)` — **team 정의**.
- `roster_grade_config (group_id, null_grade_policy, grade_names_json)` — **grade 스케일/이름(그룹별)**.

### 2.3 신규: 시점 속성 period 3종

team·grade는 변경 경계가 독립이고 FK 타깃이 달라 **별개 테이블**. shift제한은 병동무관(간호사귀속).

```sql
-- ① team 기간 (병동귀속) → teams 재사용
CREATE TABLE nurse_team_period (
  id INT PRIMARY KEY AUTO_INCREMENT,
  nurse_id VARCHAR(50) NOT NULL,
  group_id VARCHAR(50) NOT NULL,
  valid_from DATE NOT NULL,
  valid_to   DATE NULL,             -- 반쪽열림 [from, to). null=계속
  team_id INT NULL,                 -- (group_id, team_id) → teams
  source VARCHAR(20) NOT NULL DEFAULT 'edited',  -- inherited|edited|redistribute
  note TEXT, created_at DATETIME, updated_at DATETIME,
  INDEX ix_ntp_nurse (nurse_id, valid_from),
  INDEX ix_ntp_group (group_id, valid_from)
);

-- ② grade 기간 (병동귀속) → roster_grade_config 스케일로 해석
CREATE TABLE nurse_grade_period (
  id INT PRIMARY KEY AUTO_INCREMENT,
  nurse_id VARCHAR(50) NOT NULL,
  group_id VARCHAR(50) NOT NULL,
  valid_from DATE NOT NULL,
  valid_to   DATE NULL,
  grade INT NULL,
  source VARCHAR(20) NOT NULL DEFAULT 'edited',
  note TEXT, created_at DATETIME, updated_at DATETIME,
  INDEX ix_ngp_nurse (nurse_id, valid_from)
);

-- ③ shift제한·주말off (간호사귀속, 병동무관) → 교육 사례(3주차까지 D, 4주차부터 D·E)
CREATE TABLE nurse_shiftrule_period (
  id INT PRIMARY KEY AUTO_INCREMENT,
  nurse_id VARCHAR(50) NOT NULL,
  valid_from DATE NOT NULL,
  valid_to   DATE NULL,
  allowed_shifts JSON NOT NULL,     -- ["D"] / ["D","E"] ...
  weekend_off TINYINT NULL,
  source VARCHAR(20) NOT NULL DEFAULT 'edited',
  note TEXT, created_at DATETIME, updated_at DATETIME,
  INDEX ix_srp_nurse (nurse_id, valid_from)
);
```

**구간 규칙(중요)**
- **겹침 금지**: 같은 (nurse, group, attr)에 한 날 두 값 불가. **변경 시 직전 open 구간의 `valid_to`를 새 `valid_from`으로 닫고** 새 구간을 연다(close-before-open).
- **gap 허용**: 전 기간을 꽉 채울 필요 없음. 구간 없는 날 = **미지정**.
- **줄이기/늘리기**: `[1~15]`를 `[1~12]`로 닫으면 13~15는 미지정(gap)으로 남는다. 정상.

**예시 (7월)**

`nurse_shiftrule_period` — 교육(3주차까지 D / 4주차부터 D·E)
| nurse | from | to | allowed_shifts |
|---|---|---|---|
| n1 | 07-01 | **07-22** | `["D"]` |
| n1 | 07-22 | (계속) | `["D","E"]` |

`nurse_team_period` — 7/15 A→B 병동이동(병동별 팀)
| nurse | group | from | to | team |
|---|---|---|---|---|
| n2 | A | 07-01 | **07-15** | Ta |
| n2 | B | 07-15 | (계속) | Tb |

> 폴백: 구간 없으면(미지정/이관 전) `nurses`(**ward-aware** — `nurses.group_id == 요청 group`일 때만, 아니면 null). 과거는 닫힌 구간이 직접 답하므로 캐시 폴백에 의존하지 않는다.

### 2.4 `nurse_assignment` — 존재 전용 (속성 deprecate)
파견/휴직/병동이동/퇴사만. `target_team_id`/`target_grade`/`kind='permanent_change'`는 **period로 이관 후 deprecate**.
> **속성 period로 재사용 금지** — 존재↔속성 재혼합, 경계 독립(team 7/15·grade 7/22·이동 7/15 제각각), ward-scope 모호, 기존 존재-조회 쿼리 오염.

### 2.5 신규: `preceptorship` (관계+기간)
`(preceptor_id, preceptee_id, group_id, start_date, end_date, status)`. `nurses.preceptor_id`는 캐시로 강등.

### 2.6 `nurses` = 현재값 캐시
`group_id/team_id/grade/is_night_nurse/is_weekend_off/preceptor_id`는 "오늘값" 캐시. 진실=period/assignment/preceptorship.

> **폐기**: v1 `nurse_month_profile`(커밋 `ac09a53`) → period 3종으로 **교체 예정**(dev 미반영이라 비용 없음).

## 3. 정책 매트릭스 (가시성/접근)

**원칙**: 가시성은 **"그 달 그 병동 소속(membership)"**으로 판단. *근무일 수가 아니라 소속*(휴직·파견-나감처럼 근무 0일이어도 소속이면 보임). **assignment 등록 순간부터**(미래 start 포함, cancelled 제외) 적용. **그레이아웃(흐림)은 안 쓰고**, 비근무 멤버는 **상태 배지**로, 떠난 사람은 **그 달에 미표시**(보려면 그 달 선택).

| 케이스 | 근무자관리(선택 월) | 원티드 조회 | 원티드 제출 | 내 근무표 | 전체 근무표 |
|---|---|---|---|---|---|
| 정상 소속 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 병동이동 transition 월(양쪽 근무일 ≥1) | ✅ + 마커(전출←/전입→) | ✅ | ✅ | ✅ | ✅ |
| 병동이동 후 옛 병동 | **미표시**(그 달 선택해 봄) | 과거 ✅ | ❌ | 과거 ✅ | 과거 ✅ |
| 파견(나감) — home | ✅ + `파견 중` 배지 | ✅ | (정책) | ✅ | ✅ |
| 휴직(그 달 근무 0) | ✅ + `휴직` 배지 | ✅ | ❌ | ✅ | ✅ |
| 퇴사 | 퇴사 전 달까지 ✅ | 과거 ✅ | ❌ | 과거 ✅ | 과거 ✅ |

- **그레이아웃 제거**: 월 셀렉터가 있으니 과거/미래는 그 달을 선택해서 본다. 0-근무 멤버는 그레이가 아니라 **배지**로 상태 표시.
- **헤드카운트**: 이동 마커 인원은 **본 카운트에서 제외하고 별도 집계**(예: "정규 14 · 이동 2 · 휴직 1").
- **split month**: 7/15 A→B면 7월은 A(1–14)+B(15–31). A·B 양쪽에 ≥1 근무일이라 양쪽 표시(마커). per-day 귀속으로 각 로스터가 자기 날짜만 채움. "내 근무표"는 두 세그먼트 머지.

## 4. 백엔드

### 4.1 리졸버 (SSOT) — 기존 로직 재사용
이미 존재(hanjongjun 작업, 머지됨):
- **`get_active_assignments_for_month(db, group_id, year, month)`** (`assignment_service.py:888`) — **월별 presence 단일 함수**. `[start, effective_end]`가 월과 겹치고 source/target==group 인 assignment(active+completed) 반환. 생성기도 사용(`roster_create_service.py:3002, 4652`).
- **`day_windows.py`(`is_inbound`)** — 인바운드는 assignment 기간 외 날 blocked(per-day 윈도우/split).
- `is_inbound`/`is_outbound` 플래그, 커밋 `7906498 전출자 과거병동 가시성`.

신규(작게 추가):
```python
resolve_shift_rule(db, nurse_id, date) -> list[str]          # per-day 허용 shift
resolve_team(db, nurse_id, group_id, date) -> int | None     # ward-aware as-of (폴백도 ward-aware)
resolve_grade(db, nurse_id, group_id, date) -> int | None
nurse_active_days_in_group(db, nurse_id, group_id, year, month) -> int   # 마커/배지·헤드카운트용 (B)
group_members_in_month(db, group_id, year, month) -> [...]   # 소속 기준 명단 + 상태 플래그
```

### 4.2 엔드포인트 (변경/신규)
- `GET /nurses?group_id&year&month` (근무자관리): **소속 기준** 명단 + 상태 플래그(active/inbound/outbound/leave) + 마커. (월 셀렉터가 year/month 전달)
- `GET /nurses/{id}`: **선택 월 as-of** 속성 + 기간 세그먼트.
- `GET /roster/issued_roster/me` / `/issued_roster`: presence 기반(±6일 임시로직 대체).
- 원티드 제출: 대상 월·그 날의 as-of 병동에 귀속.

### 4.3 재분배 apply 재설계
- 병동 변경 → `nurse_assignment(병동이동)`.
- 팀/등급 변경 → `nurse_team_period`/`nurse_grade_period` upsert(close-before-open). 발효 cron 불필요.
- 프리셉터 종료 → `preceptorship.end_date`.

### 4.4 생성기 read 전환 (핵심)
- **shift제한**: 일자별 `resolve_shift_rule(nurse, day)` → 허용 외 shift **셀 금지** 주입(솔버 per-cell 지원). → 교육 사례.
- **team/grade**: per-day로 읽되 **gap(미지정)일은 팀 min에서 제외**(option a). 이로써 같은 병동 월중 team 변경도 자연 처리(per-day team). → §7-1 제약이 사실상 해소되는 방향(솔버 per-day team 주입 작업 필요).
- 현 읽기 지점: `roster_create_service.py:5474, 5811`.

### 4.5 발효 cron 역할 재정의
`flush_pending_*`는 **`nurses` 캐시 동기화**용으로만. 진실=period/assignment. reconcile은 "캐시 == as-of-today" 검증.

### 4.6 과거 보존 = 완전 타임라인 (freeze 안 씀)
period는 희소(델타)지만 **변경 시 옛 구간을 삭제하지 않고 close-before-open**으로 닫아 남긴다. 과거 달을 물으면 그 날을 덮는 **닫힌 구간**이 직접 답하므로, `nurses`가 새 값이 돼도 과거가 틀어지지 않는다. **스냅샷/freeze 불필요** — 과거 구간을 고치면 그 시점 view가 즉시 갱신(유연성). 발행 근무표 격자 자체는 기존대로 `IssuedRosterSnapshot`에 남는다(별개).

## 5. 프론트 표시

- **근무자관리 — 월 셀렉터 상단 신규**: 선택 월 기준 `GET /nurses?group_id&year&month`. **소속 명단**(휴직/파견 포함) + 비근무 멤버는 **상태 배지**(`휴직`·`파견 중(B)`·이동 `←/→`). **그레이아웃 없음.** 떠난 이동자는 그 달엔 미표시(그 달 선택해 봄). 헤드카운트는 **이동 제외 + 별도 집계**.
- **사이드 프로필 — 선택 월 as-of**: 그 달 기준 team/grade/shift제한 표시. 기간 편집은 `from~to + 값` 줄(기본 1줄, 바뀔 때만 추가). 줄이면 미지정(gap)으로 남음. 팀/등급 세그먼트는 병동이동에서 자동 생성. (팀 구성 화면 자체는 당분간 안 바꿈 — 위험)
- **근무표 그리드**: shift제한 per-cell **잠김**(교육: 1–21일 E·N 잠김, 22일~ E 가능). 병동이동은 타병동 일자 마스킹 + "내 근무표" 세그먼트 머지.
- **그룹키 일관**: 모든 조회 selectedGroupId 명시.

## 6. 마이그레이션 (점진 — 빅뱅 금지)

| Phase | 내용 | 검증 |
|---|---|---|
| **1. team** | `nurse_team_period` 생성 + 현재 `nurses.team_id` backfill(open 구간) + `resolve_team`(ward-aware) + 생성기 team read 전환 + 재분배 apply→period. (v1 `nurse_month_profile` 코드 교체) | 9B 7월 재생성이 재분배 팀 반영 |
| **2. shiftrule** | `nurse_shiftrule_period` + 생성기 per-day 셀 금지 | 교육 사례(D→DE) |
| **3. grade** | `nurse_grade_period` + backfill + read | 회귀: 기존 생성 동일 |
| **4. 근무자관리 표시** | 월 셀렉터 + 소속 필터 + 상태 배지 + 헤드카운트 분리 + 사이드 as-of | §3 매트릭스 |
| **5. preceptorship** | 관계 테이블 + backfill + 종료 자동화 | N:1·기간 |
| **6. assignment 정리** | `target_team_id/grade/속성변경` 이관 후 deprecate | 참조 없음 |
| **7. 일반간호사 view** | `/me`·원티드 제출 presence 기반 | §3 통과 |

각 phase: backfill → 이중읽기 비교 → read 전환 → 캐시 강등. 롤백=read 소스 되돌리기.

## 7. 조심할 것 (함정)

1. **솔버 team 단일값**: 현재 team/grade은 로스터당 간호사 단일값(`cp_sat_basic.py:786`). option(a)로 **per-day team 주입**(gap일 팀 min 제외)을 하면 월중 team 변경까지 풀리지만, 그건 솔버 입력 변경 작업.
2. **A: 전출(병동이동) source 경계 = `start_date`** (effective_end 아님). `get_active_assignments_for_month`는 effective_end 기준이라, **영구 이동의 옛 병동에 무기한 잔류**한다 → source-side는 start_date로 끊어야. **파견은 반대**(복귀하니 source 유지 = `파견 중` 배지). → 재사용 시 reason 분기 필요.
3. **C: 헤드카운트(이동 제외) ≠ 생성 공급(이동 활성일 포함)** — 두 수치 라벨 분리(혼동·9B 공급오해 방지).
4. **`nurse_assignment` 속성 재사용 금지** (§2.4).
5. **per-day는 원티드** — "2일 D"를 period가 하지 않게.
6. **구간 규칙**: **겹침 금지 / gap(미지정) 허용 / close-before-open**. ward-aware 폴백(다른 병동 캐시값 끌어오지 않기).
7. **캐시를 진실로 삼지 말 것** — 생성기는 as-of 리졸버(9B infeasible 원인). 과거는 닫힌 구간.
8. **정의 재사용 OK** — teams/roster_grade_config는 FK/해석 타깃.
9. **MSSQL 전환** — JSON→NVARCHAR(MAX), TINYINT/TEXT/백틱 방언, AUTO_INCREMENT→IDENTITY.

## 8. 미해결 / 결정 필요

1. **grade 스코프**: 병동귀속(현재 설계) vs 간호사귀속.
2. **솔버 per-day team** 주입 범위(option a): 어디까지 적용(gap만 vs 월중 변경 전체).
3. split 월 "내 근무표" 머지 UI: 통합 캘린더 vs 병동별 탭.
4. 휴직 중 원티드: 보기만 vs 부분 휴직(월 일부) 처리.
5. 미래 target 병동 "전체 근무표" 노출 시작 시점(assignment 등록 즉시 vs 승인 후).
