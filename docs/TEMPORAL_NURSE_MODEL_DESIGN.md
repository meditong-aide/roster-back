# 시점(時點) 기반 간호사 모델 설계 (v2)

> 월별/기간별로 달라지는 간호사 속성(팀·등급·근무제한·주말off)과 존재 이벤트(파견·휴직·병동이동·퇴사),
> 프리셉터 관계를 **시점 개념**으로 표현하기 위한 설계. v2 = month-grain 폐기 → **effective-dated 구간**.
> 최종 갱신 2026-06-09.

## 0. 배경 / 문제

`nurses` 단일 스냅샷이라 "6월 팀 vs 7월 팀", "3주차까지 D / 4주차부터 D·E" 같은 시점 변화를 표현 못 한다. 그로 인해:
- 팀 재분배가 발효 cron 전까지 `Nurse.team_id`에 반영 안 됨.
- 9B 7월 생성이 stale `Nurse.team_id`(team None)를 읽어 infeasible.
- 일반 간호사 group 변경 시 과거 근무표 소실·미래 원티드 오배치.

> **v1(month-grain `nurse_month_profile`) 폐기 경위**: team/grade/shift제한은 **월 중간에 날짜 경계로 바뀐다**(교육으로 3주차까지 D, 월중 병동이동 시 병동별 팀/등급). 월 단위 한 행으로는 표현 불가 → **effective-dated 구간**으로 전환.

## 1. 설계 원칙

1. **세 종류 분리 — 모양이 다르다.**
   | 종류 | 예 | 모양 | 저장 |
   |---|---|---|---|
   | 존재/위치 이벤트 | 파견·휴직·병동이동·퇴사 | 구간(start~end) | `nurse_assignment` |
   | 시점 속성 | team·grade·shift제한·weekend_off | **effective-dated 구간** | period 테이블 3종(신규) |
   | 관계 | preceptor ↔ preceptee | 기간 있는 양자 관계 | `preceptorship`(신규) |
2. **per-day 근무 희망은 "원티드"이지 속성이 아니다.** "2일·3일만 D 달라" = `nurse_shift_requests`(소프트, per-day) / `fixed_wanted_entries`(고정셀, per-day) — **이미 날짜 단위로 존재.** period가 이걸 재구현하지 않는다. (period = 구조 속성만)
3. **시점 속성 = effective-dated 구간 `[valid_from, valid_to)`.** month-grain 아님. 한 달짜리 구간은 특수 케이스.
4. **정의는 재사용, 기간만 신규.** team 정의=`teams`, grade 스케일=`roster_grade_config` 재사용. per-nurse "누가 언제 어느 팀/등급"만 새 테이블.
5. **`nurses` = 현재값 캐시(DB).** 진실은 period/assignment/preceptorship. 시점이 중요하면 리졸버를 보고 `nurses`는 "오늘값"으로만. 야간 reconcile 동기화.
6. **단일 리졸버.** 모든 화면·생성기가 같은 리졸버를 통해 시점 해석.
7. **append-only + Freeze-on-plan.** 하드 삭제 금지. 근무표 **발행 시 그 달을 덮는 구간을 `frozen` 스냅샷**으로 동결해 과거 보존(§4.6).

## 2. DB 테이블 구조

### 2.1 유지
`groups`, `teams`, `shifts`, `schedules`/`schedule_entries`, `nurse_monthly_limits`(이미 월 단위), `wanted_requests`/`nurse_shift_requests`/`fixed_wanted_entries`(이미 per-day).

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
  source VARCHAR(20) NOT NULL DEFAULT 'edited',  -- inherited|edited|redistribute|frozen
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

-- ③ shift제한·주말off (간호사귀속, 병동무관) → 사례: 교육으로 3주차까지 D, 4주차부터 D·E
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

> 폴백: 구간 없으면 `nurses`(현재값). 발행 시 그 달 덮는 구간을 `frozen`으로 동결.

### 2.4 `nurse_assignment` — 존재 전용 (속성 deprecate)
파견/휴직/병동이동/퇴사만. `target_team_id`/`target_grade`/`kind='permanent_change'`는 **period로 이관 후 deprecate**.
> **`nurse_assignment`를 속성 period로 재사용하지 않는다** — 존재↔속성 재혼합, 경계 독립(team 7/15·grade 7/22·이동 7/15 제각각), ward-scope 모호, 기존 존재-조회 쿼리 오염 때문.

### 2.5 신규: `preceptorship` (관계+기간)
`(preceptor_id, preceptee_id, group_id, start_date, end_date, status)`. `nurses.preceptor_id`는 캐시로 강등.

### 2.6 `nurses` = 현재값 캐시
`group_id/team_id/grade/is_night_nurse/is_weekend_off/preceptor_id`는 "오늘값" 캐시. 진실=period/assignment/preceptorship.

> **폐기**: v1의 `nurse_month_profile`(커밋 `ac09a53`) → 위 period 3종으로 **교체 예정**(dev 미반영이라 비용 없음).

## 3. 정책 매트릭스

**원칙**: 가시성·접근은 "assignment-유도 존재가 (병동, 월)과 하루라도 겹치는가". 발효/현재날짜 무관, **assignment 등록 순간부터**(미래 start 포함, cancelled 제외).

| 케이스 | 근무자관리 | 원티드 조회 | 원티드 제출 | 내 근무표 | 전체 근무표 |
|---|---|---|---|---|---|
| 정상 소속 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 병동이동(미래 예정) target | ✅(예정) | ✅ | ✅ | ✅ | ✅ |
| 병동이동 후 source | 과거 달만(회색) | 과거 ✅ | ❌ | 과거 ✅ | 과거 ✅ |
| 파견(기간) target | 겹친 달 ✅ | ✅ | ✅ | ✅ | ✅ |
| 휴직 | 겹친 달(회색) | ✅ | ❌(안 짬) | ✅ | ✅ |
| 퇴사 | 퇴사 전 달까지(회색) | 과거 ✅ | ❌ | 과거 ✅ | 과거 ✅ |

**split month(월중 이동)**: 7/15 A→B면 7월은 A(1–14)+B(15–31). A·B 양쪽 표시, **per-day 귀속**으로 각 병동 로스터가 자기 날짜만 채움. "내 근무표"는 두 세그먼트 머지.

## 4. 백엔드

### 4.1 리졸버 (SSOT)
```python
resolve_shift_rule(db, nurse_id, date) -> list[str]          # per-day 허용 shift (구간 as-of)
resolve_team(db, nurse_id, group_id, date) -> int | None     # 병동귀속 as-of
resolve_grade(db, nurse_id, group_id, date) -> int | None    # 병동귀속 as-of
nurse_presence_in_month(db, nurse_id, year, month) -> [(group_id, day_range)]
group_members_in_month(db, group_id, year, month) -> [...]   # 근무자관리/생성기/전체근무표 공용
# 구간 없으면 nurses(현재값) 폴백.
```

### 4.2 엔드포인트 (변경/신규)
- `GET /nurses?group_id&year&month` (근무자관리): `group_members_in_month` + 존재 플래그(active/inbound/outbound/leave/resigned).
- `GET /nurses/{id}`: as-of 속성 + 기간 세그먼트.
- `GET /roster/issued_roster/me` / `/issued_roster`: `nurse_presence_in_month` 기반(±6일 윈도우 임시로직 대체).
- 원티드 제출: 대상 월·그 날의 as-of 병동에 귀속.
- 재분배 preview/apply: §4.3.

### 4.3 재분배 apply 재설계
- 병동 변경 → `nurse_assignment(병동이동)` (존재).
- 팀/등급 변경 → `nurse_team_period`/`nurse_grade_period` upsert(대상 기간). 발효 cron 불필요.
- 프리셉터 종료 → `preceptorship.end_date`.

### 4.4 생성기 read 전환 (핵심)
- **shift제한**: 일자별 `resolve_shift_rule(nurse, day)` → 허용 외 shift **셀 금지**로 주입(솔버는 per-cell 금지 지원). → 교육 사례 됨.
- **team/grade**: 로스터(병동·월)당 그 간호사 활성 세그먼트의 **단일값**(`resolve_team/grade`). 월중 이동은 A/B 두 로스터로 split → 각 로스터 단일 팀.
- 현 읽기 지점: `roster_create_service.py:5474, 5811`(`getattr(n,"team_id")`).

### 4.5 발효 cron 역할 재정의
`flush_pending_*`는 **`nurses` 캐시 동기화**용으로만. 진실=period/assignment. reconcile은 "캐시 == as-of-today" 검증.

### 4.6 Freeze-on-plan (과거 동결)
period는 희소(델타). 영구 변경 시 `nurses`가 새 값이 되면 과거 달이 폴백으로 틀어진다. **발행(issued) 시점에 그 달을 덮는 구간을 `source='frozen'`으로 스냅샷**(이미 frozen이면 멱등 보존). 발행된 달은 이후 변경과 무관하게 그 시점으로 재현.

## 5. 프론트 표시 (구간은 소비되는 자리에서)

- **근무자관리 — 월 선택 신규 + 변동 칩**: 월 셀렉터 추가. 변동 있는 셀만 타임라인 칩(`D ─▶ D·E(22~)`, 팀 `1▶2`, `9A→9B(15~)`) + 떠남/휴직/퇴사 회색 배지.
- **사이드 프로필 — 기간 편집기**: 허용근무/팀/등급을 `from~to + 값` 줄로. **기본 1줄(전월 계속)**, 바뀔 때만 줄 추가. 팀/등급 세그먼트는 병동이동에서 자동 생성.
- **근무표 그리드**: shift제한은 per-cell **잠김/회색**(교육: 1–21일 E·N 잠김, 22일~ E 가능). 병동이동은 타병동 일자 회색 마스킹 + "내 근무표"는 세그먼트 머지.
- **그룹키 일관**: 모든 조회 selectedGroupId 명시(VERSIONS 통일과 동일).

## 6. 마이그레이션 (점진 — 빅뱅 금지)

| Phase | 내용 | 검증 |
|---|---|---|
| **1. team** | `nurse_team_period` 생성 + 현재 `nurses.team_id` 당월 backfill + `resolve_team` + 생성기 team read 전환 + 재분배 apply가 period에 기록. (v1 `nurse_month_profile` 코드 교체) | 9B 7월 재생성이 재분배 팀 반영 |
| **2. shiftrule** | `nurse_shiftrule_period` + 생성기 per-day 셀 금지 주입 | 교육 사례(D→DE) 재현 |
| **3. grade** | `nurse_grade_period` + backfill + 생성기 read | 회귀: 기존 생성 동일 |
| **4. preceptorship** | 관계 테이블 + backfill + 종료 자동화 | N:1·기간 |
| **5. assignment 정리** | `target_team_id/grade/속성변경` 이관 후 deprecate | 참조 없음 |
| **6. 일반간호사 view** | `/me`·원티드 제출 presence 기반 | §3 매트릭스 통과 |

각 phase: backfill → 이중읽기 비교 → read 전환 → 캐시 강등. 롤백=read 소스 되돌리기.

## 7. 조심할 것 (함정)

1. **솔버 single-team-per-roster**: team/grade은 로스터당 간호사 단일값(`cp_sat_basic.py:786`, 팀 min_shift 집계). **같은 병동 내 월중 team 변경 불가** — 병동이동(split)으로만, 또는 추후 솔버 per-day team 확장. (shift_rule은 per-cell이라 월중 변경 OK)
2. **`nurse_assignment` 속성 재사용 금지** — 재혼합·경계독립·ward-scope모호·쿼리오염(§2.4).
3. **정의 재사용 OK** — teams/roster_grade_config는 그대로 FK/해석 타깃.
4. **per-day는 원티드** — "2일 D"를 period가 하지 않게(중복 재구현 방지).
5. **겹침/공백** — (nurse, group, attr) 구간 겹침 금지 검증.
6. **캐시를 진실로 삼지 말 것** — 생성기는 as-of 리졸버(9B infeasible 원인).
7. **하드 삭제 금지 + Freeze** — 과거 보존.
8. **MSSQL 전환** — JSON→NVARCHAR(MAX), TINYINT/TEXT/백틱 방언, AUTO_INCREMENT→IDENTITY.

## 8. 미해결 / 결정 필요

1. **grade 스코프**: 병동귀속(사례2처럼 병동별 grade) vs 간호사귀속. (현재 설계 = 병동귀속 가정)
2. 솔버를 per-day team으로 확장할지(같은 병동 월중 team 변경 허용).
3. split 월 "내 근무표" 머지 UI: 통합 캘린더 vs 병동별 탭.
4. 재발행 시 frozen 갱신: 보존(기본) vs 재동결.
5. 휴직 중 원티드: 보기만 vs 부분 휴직 처리.
6. 미래 target 병동 "전체 근무표" 노출 시작 시점.
