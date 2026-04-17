# Team × Grade 통합 — Front-side Infeasibility Precheck 명세

## 목적
근무표 생성(POST /roster/generate 등) 호출 **이전**에 프론트에서 돌려 **수학적으로 확정적인 infeasibility**를 잡아낸다.
여기 기재된 모든 검사는 **deterministic**(랜덤 없음, 솔버 불필요)이며, 한 번이라도 위반하면 백엔드 CP-SAT은 필연적으로 infeasible 또는 hard violation을 만든다.

본 문서는 기존 `INFEASIBLE_DIAGNOSTICS_FRONT_BACK_ARCHITECTURE.md`의 백엔드 precheck(`CAPACITY_TOTAL_SHORTAGE`, `N_CAPACITY_SHORTAGE`)를 **팀/등급/공통풀 통합 관점**으로 확장한 프론트 전용 가이드다.

---

## 0. `use_mid` 처리 규약

- `use_mid`는 **프론트가 입력하는 파라미터가 아니라 `roster_config` 테이블에 저장된 상태**다.
- 알고리즘(grade_constraints 등 백엔드 모듈)은 `cfg.use_mid`를 참조해 `apply_shifts`를 `{D,E,N}` 또는 `{D,E,N,M}`으로 동적 결정한다 — 프론트는 이 값을 **수정 요청에 실어 보내지 않는다**.
- 프론트가 precheck를 돌리려면 현재 그룹의 `use_mid`를 조회해야 하므로 다음 조회 API가 필요하다:

```
GET /api/groups/{group_id}/roster-config
Response: {
  "use_mid": true,
  "daily_shift_requirements": { "D": 3, "E": 3, "N": 2, "M": 1 },
  "daily_shift_requirements_by_day": [...] | null,
  "num_days": 30,
  ...
}
```

- Precheck 전역 집합 `S`:
  - `use_mid == false` → `S = {D, E, N}`
  - `use_mid == true`  → `S = {D, E, N, M}`
- 이하 모든 공식에서 "shift s"는 `S`를 순회한다. `O`(OFF)는 커버리지 계산에 포함하지 않는다.

---

## 1. 입력 데이터 계약 (Frontend가 보유해야 함)

| 항목 | 출처 | 사용처 |
|---|---|---|
| `nurses[]` | `GET /api/nurses?group_id=` | id, grade, allowed_shifts(=`is_night_nurse`), team_id, join_date, leave_date, personal_off_adjustment |
| `teams[]` | `GET /api/teams` (기존) | team_id, members |
| `roster_config` | `GET /api/groups/{id}/roster-config` | use_mid, daily_shift_requirements, num_days, global_monthly_off_days, standard_personal_off_days 등 |
| `team_coverage` | `GET /api/groups/{id}/team-coverage` | team_min_shift |
| `grade_constraints` | `GET /api/groups/{id}/grade-constraints` | minimum_by_shift, max_by_shift |
| `fixed_assignments[]` | 개인 선호/휴가/공가 | join/leave와 별개로 특정 일자 비근무 강제 |

### 파생값 정의

```text
S = apply_shifts (use_mid 반영)
D = num_days (월 일수)
T = teams
N_all = nurses
N_team[t] = nurses where team_id == t
N_common = nurses where team_id == null
grade[n] = int or null
allowed[n] ⊆ S      # is_night_nurse JSON을 set으로 변환. 빈값/null → S 전체
active[n,d] = (join[n] ≤ d ≤ leave[n]) and d ∉ fixed_off[n]
need[s,d]  = daily_shift_requirements_by_day[d][s] if 있음 else daily_shift_requirements[s]
```

공통 규약:
- `allowed[n]`이 비었거나 null이면 `S` 전체 허용.
- `need[s,d]`가 미정의면 0으로 본다.

---

## 2. Precheck 카탈로그 (확정적 infeasibility)

각 항목 출력 스키마:
```json
{
  "reason_code": "...",
  "severity": "hard",
  "evidence": { ... }
}
```

### A. 전역 커버리지

#### A-1. `GLOBAL_DAY_CAPACITY_SHORTAGE`
**조건**: 특정 일자 d에 근무 가능 인력 수 < 해당 일자 총 요구량
```
∃ d ∈ [0, D):  Σ_{s∈S} need[s,d]  >  |{ n : active[n,d] }|
```
**evidence**: `{day, required_total, available_nurses}`

---

#### A-2. `GLOBAL_SHIFT_ALLOWED_SHORTAGE`
**조건**: 특정 (s, d)에서 shift s를 허용하는 활성 인력 수 < need
```
∃ (s,d):  need[s,d]  >  |{ n : active[n,d] ∧ s ∈ allowed[n] }|
```
`use_mid=True`일 때 M에도 동일하게 적용.
**evidence**: `{shift, day, required, allowed_nurses}`

---

#### A-3. `CAPACITY_TOTAL_SHORTAGE` *(기존)*
**조건**: 월 총 요구 > 월 총 공급 상한
```
Σ_{d,s} need[s,d]  >  Σ_n max(0, (leave[n]-join[n]+1) - required_off_days[n])
```
`required_off_days[n] = global_monthly_off_days + standard_personal_off_days + personal_off_adjustment[n]`
**evidence**: `{required_total, capacity_total, nurse_count, num_days, off_days}`

---

### B. 팀 로컬 제약

#### B-1. `TEAM_MIN_EXCEEDS_GLOBAL_NEED`
**조건**: 팀 최소 합 > 전역 요구량 (해당 shift·day)
```
∃ (s,d):  Σ_{t∈T} team_min[t,s]  >  need[s,d]
```
(모든 팀이 동일 min을 쓰면 `|T| * team_min[s] > need[s,d]`)
**evidence**: `{shift, day, teams_min_sum, global_need}`

> 주의: 팀 최소는 팀에 한정된 인원에서 나와야 하므로, 공통 풀 인력으로 대체 불가. 초과되면 확정 infeasible.

---

#### B-2. `TEAM_SIZE_INSUFFICIENT`
**조건**: 팀 크기 < 팀이 매일 커버해야 하는 최소 인원 합
```
∃ t:  |N_team[t]|  <  Σ_{s∈S} team_min[t,s]
```
**evidence**: `{team_id, team_size, team_min_sum}`

---

#### B-3. `TEAM_ACTIVE_MEMBERS_INSUFFICIENT`
**조건**: 특정 일자 d에 팀 활성 인원 < 팀 최소 합
```
∃ (t,d):  |{ n ∈ N_team[t] : active[n,d] }|  <  Σ_{s∈S} team_min[t,s]
```
**evidence**: `{team_id, day, active_count, required_min_sum}`

---

#### B-4. `TEAM_SHIFT_ALLOWED_SHORTAGE`
**조건**: 팀 내에 해당 shift를 허용하는 인원이 최소 요구 미달
```
∃ (t,s,d):  team_min[t,s] > |{ n ∈ N_team[t] : active[n,d] ∧ s ∈ allowed[n] }|
```
**evidence**: `{team_id, shift, day, required, allowed_count}`

---

### C. Grade 제약

#### C-1. `GRADE_MIN_SUM_EXCEEDS_NEED`
**조건**: 등급별 최소 합 > shift need
```
∃ (s,d):  Σ_g minimum_by_shift[s][g]  >  need[s,d]
```
**evidence**: `{shift, day, min_sum, need}`

---

#### C-2. `GRADE_MAX_SUM_BELOW_NEED`
**조건**: 등급별 max가 설정된 경우 max 합 + (max 미설정 등급의 가용 인원) < need
```
For each (s,d):
  capped_g      = { g : max_by_shift[s][g] is defined }
  capped_sum    = Σ_{g ∈ capped_g} max_by_shift[s][g]
  free_capacity = |{ n : grade[n] ∉ capped_g ∧ active[n,d] ∧ s ∈ allowed[n] }|
  if capped_sum + free_capacity < need[s,d]: infeasible
```
**evidence**: `{shift, day, capped_sum, free_capacity, need}`

---

#### C-3. `GRADE_MIN_AVAILABLE_SHORTAGE`
**조건**: 특정 grade의 활성·허용 인원 < 요구 최소
```
∃ (s,d,g):  minimum_by_shift[s][g]  >  |{ n : grade[n]==g ∧ active[n,d] ∧ s ∈ allowed[n] }|
```
**evidence**: `{shift, day, grade, required, available}`

---

#### C-4. `GRADE_ANTIPAIR_FORCES_SHORTAGE`
**조건**: anti-pair max가 너무 작아서 비해당 등급 인원만으로 need를 채울 수 없음
```
∃ (s,d,g) with max_by_shift[s][g] defined:
  non_g_allowed = |{ n : grade[n] != g ∧ active[n,d] ∧ s ∈ allowed[n] }|
  if need[s,d] - max_by_shift[s][g]  >  non_g_allowed: infeasible
```
예: N need=2, Grade3 max=1, Grade3 아닌 N 허용 인력이 0명 → 확정 infeasible.
**evidence**: `{shift, day, grade, max, non_grade_available, need}`

---

### D. `use_mid` 관련

#### D-1. `MID_REQUIRED_MISSING`
**조건**: `use_mid == true`인데 `daily_shift_requirements`에 `M` 키가 없거나 0
```
use_mid == true ∧ (M ∉ keys(daily_shift_requirements) ∨ daily_shift_requirements["M"] == 0)
```
> 설정 오류 경고. 알고리즘이 M을 생성하지 않으니 UI에서 수정 유도.
**severity**: `hard` (스케줄링은 돌지만 M이 비어 UX 파손)

---

#### D-2. `MID_DISABLED_BUT_USED`
**조건**: `use_mid == false`인데 team_min 또는 grade constraints에 M 키가 포함
```
use_mid == false ∧ (
  ∃ t: "M" ∈ team_min[t]  ∨
  ∃ s=="M": minimum_by_shift/max_by_shift 에 존재
)
```
**severity**: `hard` (무시될 값이지만 사용자 의도와 실제 실행이 다름 → 차단)
**evidence**: `{offending_keys}`

---

### E. 공통 풀 · `allowed_shifts` 관련

#### E-1. `COMMON_POOL_NIGHT_CAPACITY`
**조건**: 공통 풀 인력 중 N 허용자가 월간 N need를 충족 못함 (팀이 N min=0일 때만 의미 있음)
```
If ∀ t: team_min[t, "N"] == 0:
  common_N_capacity = Σ_{n ∈ N_common, "N" ∈ allowed[n]} working_days_capacity(n)
  monthly_N_need    = Σ_d need["N", d]
  if common_N_capacity < monthly_N_need: infeasible
```
`working_days_capacity(n) = (leave[n]-join[n]+1) - required_off_days[n]`

> N전담 공통 풀 전용 케이스. 팀이 N min>0인 구조면 이 검사는 스킵.
**evidence**: `{common_N_capacity, monthly_N_need}`

---

#### E-2. `ALLOWED_SHIFTS_ISOLATES_NURSE`
**조건**: 특정 nurse의 `allowed_shifts`가 공집합이거나 `{O}`만 포함
```
∃ n:  allowed[n] ∩ S == ∅  ∧  active_days(n) > required_off_days[n]
```
> 이 사람은 근무 불가능한데 OFF 상한을 초과하는 활성 일수를 가져 확정 infeasible.
**evidence**: `{nurse_id, allowed, active_days, required_off_days}`

---

### F. Fixed assignment 관련

#### F-1. `FIXED_ASSIGN_EXCEEDS_NEED`
**조건**: 특정 (s,d)의 고정 배정 인원이 need 초과
```
∃ (s,d):  |{ n : fixed_assignment[n,d] == s }|  >  need[s,d]
```
**evidence**: `{shift, day, fixed_count, need}`

---

#### F-2. `FIXED_ASSIGN_VIOLATES_ALLOWED`
**조건**: 고정 배정이 nurse의 `allowed_shifts` 밖
```
∃ (n,d):  fixed_assignment[n,d] ∉ allowed[n] ∪ {"O"}
```
**evidence**: `{nurse_id, day, assigned_shift, allowed}`

---

#### F-3. `FIXED_ASSIGN_BREAKS_TEAM_MIN`
**조건**: 팀 내 고정 OFF들이 많아 해당 일자 팀 min을 못 채움
```
∃ (t,d):  |{ n ∈ N_team[t] : active[n,d] ∧ not fixed_off[n,d] }|  <  Σ_s team_min[t,s]
```
(B-3을 fixed_off까지 반영한 변형)
**evidence**: `{team_id, day, remaining_members, required_min_sum}`

---

## 3. 검사 순서 (권장)

비용이 낮은 것부터, 그리고 한 검사 실패가 다른 검사를 의미 없게 만드는 순서로 정렬:

```
1. D-1, D-2           (설정 정합성 — 가장 싸고 확정적)
2. E-2                (개인 allowed_shifts 공집합)
3. B-2                (팀 크기 부족 — 일자 순회 불필요)
4. C-1                (grade min 합계 산술 검사)
5. F-1, F-2           (고정 배정 정합성)
6. A-1, A-2, A-3      (전역 커버리지/공급 부족)
7. B-1, B-3, B-4      (팀 일자별 검사)
8. C-2, C-3, C-4      (grade 일자별 검사)
9. F-3                (팀 × 고정 배정 교차)
10. E-1               (공통 풀 N 월간 용량)
```

중단 정책 권장: **설정성 오류(D, E-2, F-1/F-2)는 즉시 중단, 데이터성 오류(나머지)는 모두 수집해 한 번에 표시**.

---

## 4. 엔드포인트 스펙 제안

```
POST /api/groups/{group_id}/roster/precheck
Body: {
  "year": 2026,
  "month": 4,
  "roster_config_overrides": { ... },       // optional, 임시 편집 중인 값
  "team_coverage_override": { ... },         // optional
  "grade_constraints_override": { ... }      // optional
}

Response: {
  "status": "OK" | "HAS_ISSUES",
  "issues": [
    {
      "reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
      "severity": "hard",
      "evidence": { "shift": "D", "day": 3, "teams_min_sum": 4, "global_need": 3 },
      "message_ko": "3일 D에 팀 최소 합(4)이 전역 요구(3)보다 큽니다."
    },
    ...
  ]
}
```

- `overrides`를 받는 이유: 사용자가 설정 화면에서 값을 바꾸는 도중 **저장 전에** "이 설정으로 생성 가능한가?"를 물을 수 있어야 함.
- 응답 `issues`는 배열. 프론트는 코드별로 그룹해 UI 카드로 표시.
- `status=="OK"`이면 생성 버튼 활성화. 단, `OK`는 확정 가능성을 뜻하지 않음(soft 제약·최적화 목적함수 영역) — **"확정 불가능은 아님"**을 의미.

---

## 5. Backend 반영 지점

Precheck를 프론트에서 돌려도, 마지막 안전망은 백엔드여야 함. 아래 위치에 동일 로직을 mirror:

| 백엔드 모듈 | 반영 내용 |
|---|---|
| `services/roster_create_service.py` 진입부 | 본 문서의 전체 카탈로그를 실행, `diagnostics.precheck.reason_codes`에 추가 |
| `services/constraints/grade_constraints.py` | `apply_shifts`를 `cfg.use_mid` 기반 동적화, max 제약 추가 |
| (신설) `services/constraints/team_constraints.py` | `team_min_shift` hard 제약 생성 |
| (신설) `services/precheck/team_grade_precheck.py` | 카탈로그 로직 단일 구현, 프론트/백 공용 |

---

## 6. reason_code 카탈로그 (추가)

기존 `INFEASIBLE_DIAGNOSTICS_FRONT_BACK_ARCHITECTURE.md` 카탈로그에 다음 추가:

```
GLOBAL_DAY_CAPACITY_SHORTAGE
GLOBAL_SHIFT_ALLOWED_SHORTAGE
TEAM_MIN_EXCEEDS_GLOBAL_NEED
TEAM_SIZE_INSUFFICIENT
TEAM_ACTIVE_MEMBERS_INSUFFICIENT
TEAM_SHIFT_ALLOWED_SHORTAGE
GRADE_MIN_SUM_EXCEEDS_NEED
GRADE_MAX_SUM_BELOW_NEED
GRADE_MIN_AVAILABLE_SHORTAGE
GRADE_ANTIPAIR_FORCES_SHORTAGE
MID_REQUIRED_MISSING
MID_DISABLED_BUT_USED
COMMON_POOL_NIGHT_CAPACITY
ALLOWED_SHIFTS_ISOLATES_NURSE
FIXED_ASSIGN_EXCEEDS_NEED
FIXED_ASSIGN_VIOLATES_ALLOWED
FIXED_ASSIGN_BREAKS_TEAM_MIN
```

---

## 7. Non-goals

- **연속근무·연속OFF 패턴 검사**(N전담 2~3연속, OFF≥2)는 포함하지 않음. 이는 nurse-level 확정 infeasible이 아니라 CP-SAT 탐색 실패 가능성 영역.
- **soft 제약/선호도/팀 정렬 공정성**은 precheck 범위 밖.
- **월중 팀 이동/인력 장단기 부재 등 시점 교차 이슈**는 현재 범위 제외.
