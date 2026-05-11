# 근무표 인페이저빌리티 — 프론트엔드 통합 가이드

근무표 관련 두 엔드포인트(`PUT /nurses/monthly-limits`, `POST /roster_create/generate`)
가 사용자 입력의 산술 모순/조합형 충돌을 어떻게 사용자에게 노출하는지 정리한다.
프론트는 단일 `infeasibility` 객체만 파싱하면 된다.

---

## 1. 적용 엔드포인트

| 엔드포인트 | 용도 | 검증 시점 |
|---|---|---|
| `PUT /nurses/monthly-limits` | 개인별 월간 D/E/N/O min/max/exact 한도 저장 | save-time precheck |
| `POST /roster_create/generate` | 근무표 생성 | save-time precheck + solver + auto-soft |

두 엔드포인트가 **같은 `infeasibility` schema** 로 응답한다. 프론트는 한 번만 구현하면 양쪽 사용 가능.

---

## 2. HTTP status 매트릭스

| HTTP | 의미 | 근무표/저장 결과 |
|---|---|---|
| **200** | 성공 (정상 또는 일부 제약 자동 완화) | 저장됨 / 근무표 반환 |
| **500** | 산술 모순으로 차단, 또는 자동 완화도 실패 | 저장 실패 / 근무표 없음 |

> 실패 시에도 항상 구조화된 진단 객체가 응답된다. 프론트는 raw 문자열을 파싱할
> 필요 없이 `infeasibility` 객체만 읽으면 된다.

---

## 3. `infeasibility` 객체 — TypeScript 인터페이스

```ts
interface Infeasibility {
  /** UI 분기 신호 — 항상 셋 중 하나 */
  severity: "ok" | "warning" | "blocking";

  /** UI 토스트/배너에 그대로 표시 가능한 한 줄 요약 */
  summary_message_ko: string;

  /** save-time precheck에서 발견한 입력 단위 issue 목록 */
  preflight_issues: PreflightIssue[];

  /**
   * 솔버가 자동 완화한 제약 식별자.
   * 현재 가능한 값: ["grade_hard_to_soft"]
   * 빈 배열이면 자동 완화 없음.
   */
  applied_relaxations: string[];

  /** 사용자에게 보여줄 통합 액션 제안 (preflight_issues 기반) */
  fix_suggestions_ko: string[];

  /** 솔버 후 family별 위반 요약 (자연 soft 적용 시 채워짐) */
  violation_summary: {
    grade_min?:    { count: number; samples: ViolationSample[] };
    grade_max?:    { count: number; samples: ViolationSample[] };
    team_min?:     { count: number; samples: ViolationSample[] };
    coverage_min?: { count: number; samples: ViolationSample[] };
  };

  /** 자연 soft까지 실패한 경우의 원본 솔버 에러 reason (없으면 null) */
  last_error_reason: string | null;
}

interface PreflightIssue {
  reason_code: string;        // §7 카탈로그 참조
  severity: "hard" | "blocking";
  evidence: object;           // 코드별 키 (shift, day, grade, nurse_id 등)
  human_message_ko: string;   // 사용자에게 그대로 표시 가능한 한국어
  fix_suggestions_ko: string[];
}

interface ViolationSample {
  node_id: string;            // "grade_min:1:0:D" 등
  details: object;            // {day, shift, grade, need, assigned, ...}
  slack: number;              // 음수면 부족, 0이면 정확히 충족
}
```

---

## 4. HTTP status별 응답 위치

| HTTP | infeasibility 위치 |
|---|---|
| 200 | `response.infeasibility` (root) |
| 500 | `response.detail.infeasibility` |

> 파싱 한 줄로 통일:
> ```ts
> const inf = res.ok ? body.infeasibility : body.detail?.infeasibility;
> ```

---

## 5. severity 별 UI 처리

| severity | HTTP | 결과물 | applied_relaxations | UI 권장 |
|---|---|---|---|---|
| **ok** | 200 | 정상 | `[]` | 그냥 표시. 별도 알림 없음 |
| **warning** | 200 | 일부 제약 자동 완화 | `["grade_hard_to_soft"]` 등 | 노란 토스트 + summary_message_ko + violation_summary 펼쳐 보기 |
| **blocking** | 500 | 없음 | `[]` 또는 시도된 relaxations | 모달/에러 페이지 + summary_message_ko + preflight_issues + fix_suggestions_ko |

---

## 6. 파싱 예시 (TypeScript)

```ts
async function callRosterApi(method: string, url: string, body?: object) {
  const res = await fetch(url, {
    method,
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  const inf = res.ok ? data.infeasibility : data.detail?.infeasibility;

  if (!inf) {
    throw new Error(typeof data?.detail === "string" ? data.detail : "Unknown error");
  }
  return { ok: res.ok, data, inf };
}

// 근무표 생성
const { ok, data, inf } = await callRosterApi(
  "POST", "/roster_create/generate",
  { year: 2026, month: 4, grade_strategy: "COMBINED" }
);
switch (inf.severity) {
  case "ok":
    return showSchedule(data);
  case "warning":
    showWarningToast(inf.summary_message_ko);
    showViolations(inf.violation_summary);
    return showSchedule(data);
  case "blocking":
    showErrorModal(inf);
    return;
}

// 월 한도 저장 — 같은 schema, 같은 분기
const { ok: ok2, inf: inf2 } = await callRosterApi(
  "PUT", "/nurses/monthly-limits", payload
);
if (!ok2) showErrorModal(inf2);  // preflight_issues에 nurse별 모순 노출
```

---

## 7. `reason_code` 카탈로그

각 코드는 `human_message_ko`에 한국어로 풀려있어 UI는 그것만 표시해도 충분하다.
아래는 코드별 의미와 주요 evidence 키.

### 7.1 설정/입력 정합성

| reason_code | 의미 | 주요 evidence |
|---|---|---|
| `MID_REQUIRED_MISSING` | use_mid=true인데 M 일별 요구치 0 | `daily_shift_requirements` |
| `MID_DISABLED_BUT_USED` | use_mid=false인데 M 관련 설정 존재 | - |
| `ALLOWED_SHIFTS_ISOLATES_NURSE` | 어느 nurse의 가능 시프트가 공집합 | `nurse_id` |
| `FIXED_OFF_EXCEEDS_SPAN` | 고정 OFF가 활성 기간 초과 | `nurse_id`, `span`, `fixed_off` |

### 7.2 팀 (team_min)

| reason_code | 의미 | 주요 evidence |
|---|---|---|
| `TEAM_SIZE_INSUFFICIENT` | 팀 멤버 수 < 팀 최소 인원 | `team_id`, `members`, `min` |
| `TEAM_MIN_EXCEEDS_GLOBAL_NEED` | 팀별 min 합 > 일별 요구 | `shift`, `day`, `teams_min_sum`, `global_need` |
| `TEAM_ACTIVE_MEMBERS_INSUFFICIENT` | 일자별 활성 팀 멤버 < 팀 min | `team_id`, `day`, `shift`, `active`, `min` |
| `TEAM_SHIFT_ALLOWED_SHORTAGE` | 팀 내 그 시프트 가능자 < min | `team_id`, `day`, `shift`, `available`, `min` |

### 7.3 Grade

| reason_code | 의미 | 주요 evidence |
|---|---|---|
| `GRADE_MIN_SUM_EXCEEDS_NEED` | shift별 grade min 합 > 일별 요구 | `shift`, `day`, `min_sum`, `need` |
| `GRADE_MAX_SUM_BELOW_NEED` | grade max 합 < 일별 요구 | `shift`, `day`, `max_sum`, `need` |
| `GRADE_MIN_AVAILABLE_SHORTAGE` | 일자/시프트별 grade 가능 풀 < min | `shift`, `day`, `grade`, `available`, `min` |
| `GRADE_ANTIPAIR_FORCES_SHORTAGE` | grade anti-pair + min 결합 모순 | `shift`, `day`, `grade` |

### 7.4 Coverage / 가용 capacity

| reason_code | 의미 | 주요 evidence |
|---|---|---|
| `GLOBAL_DAY_CAPACITY_SHORTAGE` | 어느 일자 활성 인원 < 요구 합 | `day`, `active`, `total_need` |
| `GLOBAL_SHIFT_ALLOWED_SHORTAGE` | 일자/시프트별 가능 풀 < 요구 | `day`, `shift`, `available`, `need` |
| `CAPACITY_TOTAL_SHORTAGE` | 월간 가용 일수 합 < 월간 요구 | `monthly_demand`, `monthly_capacity` |
| `MONTHLY_NIGHT_CAPACITY` | 월 N 요구 > N 가능 nurse 월 capacity 합 | `monthly_demand`, `monthly_capacity` |

### 7.5 고정 셀(원티드/휴가) ↔ 다른 제약 충돌

| reason_code | 의미 | 주요 evidence |
|---|---|---|
| `FIXED_ASSIGN_EXCEEDS_NEED` | 고정 배정만으로 일별 요구 초과 | `shift`, `day`, `fixed`, `need` |
| `FIXED_ASSIGN_VIOLATES_ALLOWED` | nurse 가능 시프트 밖에 고정 배정 | `nurse_id`, `day`, `shift` |
| `FIXED_ASSIGN_BREAKS_TEAM_MIN` | 고정 배정이 팀 min 충족 차단 | `team_id`, `day`, `shift` |
| `TEAM_GRADE_INTERSECT_SHORTAGE` | 팀 × grade 교집합 부족 | `team_id`, `shift`, `day`, `grade` |

### 7.6 개인 월간 한도(`PUT /nurses/monthly-limits`) 저장 시점

#### nurse 단위

| reason_code | 의미 | 주요 evidence |
|---|---|---|
| `MONTHLY_LIMIT_VALUE_EXCEEDS_ACTIVE_DAYS` | 단일 시프트 min/exact > 그 nurse의 가용일 | `nurse_id`, `shift`, `field`, `value`, `active_days` |
| `MONTHLY_LIMIT_SUM_MAX_BELOW_SUM_MIN` | sum(max) < sum(min) — 정의역 빈집합 | `nurse_id`, `sum_min`, `sum_max` |
| `MONTHLY_LIMIT_SUM_MAX_BELOW_ACTIVE_DAYS` | 모든 시프트 max 합이 가용일 미만 | `nurse_id`, `sum_max`, `active_days` |
| `MONTHLY_LIMIT_NIGHT_DEDICATED_HAS_DAY_OR_EVENING` | N 전담 nurse에 D/E min/exact > 0 | `nurse_id`, `shift`, `min`, `exact` |
| `MONTHLY_LIMIT_NOT_IN_WORK_SHIFTS` | nurse work_shifts 밖 시프트에 양수 | `nurse_id`, `shift`, `allowed` |
| `MONTHLY_LIMIT_O_MAX_BELOW_FORCED_OFF` | o_max < 강제 OFF(주말휴무+weekly_off, vacation 제외) | `nurse_id`, `o_max`, `forced_off_count` |
| `MONTHLY_LIMIT_WORK_MIN_EXCEEDS_AVAILABLE_WEEKDAYS` | D/E/N min 합 > 가용일 - 강제 OFF | `nurse_id`, `work_min_sum`, `work_capacity` |

#### 그룹 N 풀 (cross-nurse, save-time만)

| reason_code | 의미 | 주요 evidence |
|---|---|---|
| `MONTHLY_LIMIT_GROUP_N_CAPACITY_BELOW_DEMAND` | Σ(forced n_exact) + Σ(free n_max or active) < 월간 N 요구 | `group_id`, `forced_n_sum`, `free_capacity_sum`, `monthly_n_demand` |
| `MONTHLY_LIMIT_GROUP_N_FORCED_SUM_BELOW_DEMAND` | 모든 N 가능 nurse가 n_exact로 잠긴 채 합이 부족 | `group_id`, `forced_n_sum`, `nurses_forced` |
| `MONTHLY_LIMIT_GROUP_N_FORCED_EXCEEDS_DAILY_CAP` | 강제 N 합 > 일별 N max 합(설정 시) | `group_id`, `forced_n_sum`, `daily_n_max_total` |

#### 그룹/월 합산

| reason_code | 의미 | 주요 evidence |
|---|---|---|
| `MONTHLY_LIMIT_GROUP_EXACT_SUM_EXCEEDS` | 그룹별 exact 합 > 그룹 가용일 | `nurse_id`, `group_id`, `exact_sum`, `cap_days` |
| `MONTHLY_LIMIT_GROUP_MIN_SUM_EXCEEDS` | 그룹별 min 합 > 그룹 가용일 | `nurse_id`, `group_id`, `min_sum`, `cap_days` |
| `MONTHLY_LIMIT_MONTH_EXACT_SUM_EXCEEDS` | 월 합산 exact > 월 가용일 | `nurse_id`, `total_exact`, `month_active` |
| `MONTHLY_LIMIT_MONTH_MIN_SUM_EXCEEDS` | 월 합산 min > 월 가용일 | `nurse_id`, `total_min`, `month_active` |

---

## 8. `applied_relaxations` 값

자동 완화된 제약 식별자.

| 코드 | 의미 | 트리거 |
|---|---|---|
| `grade_hard_to_soft` | Grade min/max를 hard에서 soft로 자동 전환 | precheck 통과 후 솔버 NO_ASSIGNMENT |

빈 배열(`[]`)이면 자동 완화 없이 사용자 입력대로 솔버가 만족함.

---

## 9. `violation_summary` 구조 (자연 soft 적용 시)

```json
{
  "grade_min": {
    "count": 30,
    "samples": [
      {
        "node_id": "grade_min:1:0:D",
        "details": { "day": 1, "shift": "D", "grade": 1, "need": 5, "assigned": 3 },
        "slack": -2
      }
    ]
  }
}
```

family 키:
- `grade_min` / `grade_max` — Grade 분배 위반
- `team_min` — 팀별 최소 인원 위반
- `coverage_min` — 일별 시프트 요구 위반

---

## 10. 동작 매트릭스 (PUT /nurses/monthly-limits)

| 시나리오 | HTTP | severity | 사용자 안내 |
|---|---|---|---|
| 정상 입력 | 200 | ok (응답 자체는 schedule 객체와 다름; 별도 infeasibility 없음) | 그대로 저장 |
| nurse 단위 산술 모순 | 500 | blocking | `human_message_ko` + fix_suggestions |
| 그룹 N 풀 부족 | 500 | blocking | "그룹 N 가용 합 X가 월 요구 Y에 부족합니다" |

> 참고: `PUT /nurses/monthly-limits` 응답은 정상 시 `NurseMonthlyLimitListResponse` 객체로,
> 실패 시 `{detail: {infeasibility: ...}}` 객체로 응답한다.

---

## 11. 동작 매트릭스 (POST /roster_create/generate)

| 시나리오 | HTTP | severity | 비고 |
|---|---|---|---|
| 솔버 primary 성공 | 200 | ok | 정상 schedule |
| primary INFEASIBLE → fallback 성공 | 200 | ok 또는 warning | `solver_status="fallback"`, coverage_gaps 가능 |
| 자연 soft 적용 | 200 | warning | `applied_relaxations=["grade_hard_to_soft"]` + violation_summary |
| precheck blocking | 500 | blocking | 솔버 호출 안 함 |
| 자연 soft 실패 | 500 | blocking | `last_error_reason` 포함 |

---

## 12. UI 권장 워딩 예시

### 케이스 A — `severity = "ok"`
별도 메시지 없이 결과만 표시.

### 케이스 B — `severity = "warning"`
**토스트(노란색):**
> {summary_message_ko}

**상세 펼침:**
> 일부 제약이 자동 완화되었습니다.
> - 자동 완화: Grade 최소 인원 (hard → soft)
> - 위반: Grade 최소 30건
> - 권장:
>   - {fix_suggestions_ko[0]}
>   - {fix_suggestions_ko[1]}

### 케이스 C — `severity = "blocking"`
**모달:**
> ## 입력에 문제가 있어 처리할 수 없습니다
>
> {summary_message_ko}
>
> **발견된 문제:**
> - {preflight_issues[0].human_message_ko}
> - {preflight_issues[1].human_message_ko}
>
> **다음 중 하나를 시도해보세요:**
> - {fix_suggestions_ko[0]}
> - {fix_suggestions_ko[1]}

---

## 13. 응답 예시

### 13.1 ok (HTTP 200, 근무표 생성 정상)
```json
{
  "year": 2026, "month": 4, "schedule_id": "abc",
  "days_in_month": 30,
  "nurses": [...],
  "infeasibility": {
    "severity": "ok",
    "summary_message_ko": "",
    "preflight_issues": [],
    "applied_relaxations": [],
    "fix_suggestions_ko": [],
    "violation_summary": {},
    "last_error_reason": null
  }
}
```

### 13.2 warning (HTTP 200, 자연 soft 적용)
```json
{
  "year": 2026, "month": 4, "schedule_id": "...",
  "nurses": [...],
  "infeasibility": {
    "severity": "warning",
    "summary_message_ko": "입력하신 Grade 최소 인원 요구 대비 가용 인원이 제한적이어서, 가능한 범위에서 최적의 근무표를 생성했습니다.",
    "preflight_issues": [],
    "applied_relaxations": ["grade_hard_to_soft"],
    "fix_suggestions_ko": [],
    "violation_summary": {
      "grade_min": {
        "count": 50,
        "samples": [{"node_id":"grade_min:1:0:D","details":{"day":1,"shift":"D","grade":1,"need":3,"assigned":2},"slack":-1}]
      }
    },
    "last_error_reason": null
  }
}
```

### 13.3 blocking — generate (HTTP 500)
```json
{
  "detail": {
    "infeasibility": {
      "severity": "blocking",
      "summary_message_ko": "팀별 최소 인원 합계가 해당 시프트의 일별 요구 인원을 초과합니다. (시프트=주간(D), 일자=1일)",
      "preflight_issues": [
        {
          "reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
          "severity": "hard",
          "evidence": {"shift":"D","day":0,"teams_min_sum":6,"global_need":3},
          "human_message_ko": "팀별 최소 인원 합계가 해당 시프트의 일별 요구 인원을 초과합니다. (시프트=주간(D), 일자=1일)",
          "fix_suggestions_ko": [
            "팀별 최소 인원을 낮추세요.",
            "일별 요구 인원을 늘리세요."
          ]
        }
      ],
      "applied_relaxations": [],
      "fix_suggestions_ko": ["팀별 최소 인원을 낮추세요.", "일별 요구 인원을 늘리세요."],
      "violation_summary": {},
      "last_error_reason": null
    }
  }
}
```

### 13.4 blocking — monthly-limits 저장 (HTTP 500)
```json
{
  "detail": {
    "infeasibility": {
      "severity": "blocking",
      "summary_message_ko": "그룹 N 가용 합(56)이 월간 N 요구(60)에 부족합니다. (강제 합 56 + 자유 한도 합 0)",
      "preflight_issues": [
        {
          "reason_code": "MONTHLY_LIMIT_GROUP_N_CAPACITY_BELOW_DEMAND",
          "severity": "blocking",
          "evidence": {
            "group_id": "10135890c287",
            "monthly_n_demand": 60,
            "forced_n_sum": 56,
            "free_capacity_sum": 0,
            "total_capacity": 56
          },
          "human_message_ko": "그룹 N 가용 합(56)이 월간 N 요구(60)에 부족합니다. (강제 합 56 + 자유 한도 합 0)",
          "fix_suggestions_ko": [
            "일부 nurse의 n_exact를 늘리세요.",
            "n_exact를 빼고 자유 nurse를 늘리세요.",
            "야간 가능 간호사를 추가 배치하세요."
          ]
        }
      ],
      "applied_relaxations": [],
      "fix_suggestions_ko": [
        "일부 nurse의 n_exact를 늘리세요.",
        "n_exact를 빼고 자유 nurse를 늘리세요.",
        "야간 가능 간호사를 추가 배치하세요."
      ],
      "violation_summary": {},
      "last_error_reason": null
    }
  }
}
```

---

## 14. 백엔드 동작 메모 (참고)

프론트가 알 필요는 없지만 디버깅 시 도움 되는 백엔드 동작:

- **솔버 hard 제약 일관성**: nurse-level 월간 한도(d/e/n/o min/max/exact)는
  primary(`cp_sat_basic`)와 fallback(`fallback_lex` 모든 stage)에서 동일하게
  hard로 등록된다. 즉 fallback 경유 시에도 사용자 입력이 silent 무시되지 않는다.
- **`grade_strategy` semantics**:
  - 데이터(team_min/grade_config) 존재 시 자동 활성화
  - `TEAM`/`GRADE` 값은 weight tilt(우선도 boost)로만 작용
  - 역사적 게이트(BASE에선 비활성 등) 제거됨
- **자연 soft (auto-relax)**: precheck 통과 후 솔버 NO_ASSIGNMENT 시
  `_force_grade_max_soft_fallback=True`로 1회 재시도. grade hard → soft 전환.
  team_min은 자연 soft 대상 아님(사용자 의도 보존).

---

## 15. 변경 이력

| 날짜 | 변경 |
|---|---|
| 2026-05-07 | 초안: precheck + 자연 soft + structured infeasibility 응답 통합 |
| 2026-05-07 | monthly-limits 저장 검증 추가, group N pool 검사 추가, 솔버 hard 제약 모듈화 (primary+fallback 공유), reason_code 카탈로그 §7.6 추가 |
