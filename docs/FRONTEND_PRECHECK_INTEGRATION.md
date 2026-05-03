# Precheck 프론트엔드 연동 가이드 (React + Vite)

> **대상**: React + Vite 기반 프론트엔드 개발자
> **엔드포인트**: `POST /groups/{group_id}/roster/precheck`
> **목적**: 근무표 생성(`POST /roster/generate`) **이전**에 결정적 infeasibility 를 감지해 UX 를 차단

---

## 1. 왜 필요한가

백엔드 CP-SAT 가 생성 시도를 해보고 실패해야 알 수 있던 "수학적으로 확정적인 불가능 조합" 을 미리 감지한다. 예시:

- 간호사 수가 일별 요구량보다 적음
- 팀이 3개인데 각 팀 최소 D 가 1 이고 전역 D 요구가 2 (→ 최소 합 3 > 요구 2)
- Grade 3 최대 N 배정이 1 인데 N 2명 필요 + Grade 3 이 아닌 N 허용자가 0

사용자가 "생성" 버튼을 누르기 전에 이 조합을 잡아서 **어디를 고쳐야 하는지** 알려준다.

---

## 2. 플로우

```
┌────────────────────────────────────────────────────────┐
│  설정 화면 (팀/등급/스케줄 설정, 인원 편집 등)            │
│    │                                                    │
│    │ 값 변경 시 debounced precheck 호출                  │
│    ▼                                                    │
│  POST /groups/{id}/roster/precheck                     │
│    │                                                    │
│    ├─ status=OK        → 생성 버튼 활성화              │
│    └─ status=HAS_ISSUES → issues[] 로 UI 표시          │
│                           생성 버튼 비활성화(hard 한정) │
│                                                         │
│  [생성] 버튼 → POST /roster/generate                    │
└────────────────────────────────────────────────────────┘
```

---

## 3. 엔드포인트 스펙

### 3.1 Request

```
POST /groups/{group_id}/roster/precheck
Content-Type: application/json
```

```json
{
  "num_days": 30,
  "nurses": [
    {
      "nurse_id": "n01",
      "grade": 2,
      "team_id": 1,
      "allowed_shifts": null,
      "join_day": 0,
      "leave_day": 29,
      "personal_off_adjustment": 0,
      "fixed_off_days": [5, 12],
      "fixed_shift_assignments": { "3": "D" }
    }
  ],
  "teams": [1, 2, 3],
  "roster_config": {
    "use_mid": false,
    "daily_shift_requirements": { "D": 3, "E": 3, "N": 2 },
    "daily_shift_requirements_by_day": null,
    "global_monthly_off_days": 3,
    "standard_personal_off_days": 8
  },
  "team_coverage": {
    "1": { "D": 1, "E": 1, "N": 0 },
    "2": { "D": 1, "E": 1, "N": 0 },
    "3": { "D": 1, "E": 1, "N": 0 }
  },
  "grade_constraints": {
    "minimum_by_shift": { "D": { "1": 1 }, "E": { "2": 1 } },
    "max_by_shift": { "N": { "3": 1 } }
  },
  "stop_on_config_error": false
}
```

### 3.2 Response

```json
{
  "status": "OK" | "HAS_ISSUES",
  "issues": [
    {
      "reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
      "severity": "hard",
      "evidence": { "shift": "D", "day": 0, "teams_min_sum": 3, "global_need": 2 }
    }
  ]
}
```

- `status == "OK"`: 모든 검사 통과 — 생성 버튼 활성화 가능 (단, "확정 가능" 의 뜻은 아님. soft 제약·최적화 영역은 백엔드 솔버가 담당)
- `status == "HAS_ISSUES"`: 최소 하나의 결정적 infeasibility 감지

### 3.3 Request 필드 상세

| 필드 | 타입 | 출처 | 비고 |
|---|---|---|---|
| `num_days` | `number` | 월 일수 계산 | 30/31/28/29 |
| `nurses[].nurse_id` | `string` | Nurse API | |
| `nurses[].grade` | `number \| null` | Nurse API `grade` | |
| `nurses[].team_id` | `number \| null` | Nurse API `team_id` | null = 공통풀 |
| `nurses[].allowed_shifts` | `string[] \| null` | Nurse API `is_night_nurse` | null/빈배열 → S 전체 허용 |
| `nurses[].join_day` | `number` | 파생 | 0-based. 월 시작일 ≤ joining_date 면 0 |
| `nurses[].leave_day` | `number` | 파생 | 0-based inclusive. 퇴사/파견 없으면 num_days-1 |
| `nurses[].personal_off_adjustment` | `number` | Nurse API | |
| `nurses[].fixed_off_days` | `number[]` | 파생 (고정 OFF/휴가/공가) | 0-based |
| `nurses[].fixed_shift_assignments` | `{ [day: string]: string }` | 파생 (고정 근무) | day 는 string key (JSON 호환) |
| `teams` | `any[]` | 참조용 (검증엔 영향 없음) | team_coverage 키에서 실제 팀 추출 |
| `roster_config.use_mid` | `boolean` | `GET /groups/{id}/roster-config` | 프론트가 수정 불가 — DB 값 그대로 |
| `roster_config.daily_shift_requirements` | `Record<string, number>` | Roster Config API | `{D,E,N}` 또는 `{D,E,N,M}` |
| `roster_config.daily_shift_requirements_by_day` | `Record<string,number>[] \| null` | 일자별 오버라이드(있으면 우선) | |
| `roster_config.global_monthly_off_days` | `number` | Roster Config API | |
| `roster_config.standard_personal_off_days` | `number` | Roster Config API | |
| `team_coverage` | `Record<string, Record<"D"\|"E"\|"N"\|"M", number>>` | Team Coverage API | `team_id → {shift: min}` |
| `grade_constraints.minimum_by_shift` | `Record<shift, Record<grade, number>>` | Grade API | |
| `grade_constraints.max_by_shift` | `Record<shift, Record<grade, number>>` | Grade API (anti-pair) | |
| `stop_on_config_error` | `boolean` | UX 옵션 | true 면 설정성 오류 발견 시 일자별 검사 스킵 |

> **use_mid 주의**: 프론트가 수정 요청에 실으면 안 됨. 항상 서버 저장값. `M` 시프트 관련 필드는 `use_mid === true` 일 때만 존재해야 함.

---

## 4. TypeScript 타입 정의

```ts
// types/precheck.ts

export type Shift = "D" | "E" | "N" | "M";

export interface PrecheckNursePayload {
  nurse_id: string;
  grade: number | null;
  team_id: number | null;
  allowed_shifts: string[] | null;
  join_day: number;
  leave_day: number;
  personal_off_adjustment: number;
  fixed_off_days: number[];
  fixed_shift_assignments: Record<string, string>;
}

export interface RosterConfigPayload {
  use_mid: boolean;
  daily_shift_requirements: Partial<Record<Shift, number>>;
  daily_shift_requirements_by_day: Array<Partial<Record<Shift, number>>> | null;
  global_monthly_off_days: number;
  standard_personal_off_days: number;
}

export interface GradeConstraintsPayload {
  minimum_by_shift?: Partial<Record<Shift, Record<string, number>>>;
  max_by_shift?: Partial<Record<Shift, Record<string, number>>>;
}

export interface PrecheckRequest {
  num_days: number;
  nurses: PrecheckNursePayload[];
  teams: Array<number | string>;
  roster_config: RosterConfigPayload;
  team_coverage: Record<string, Partial<Record<Shift, number>>>;
  grade_constraints: GradeConstraintsPayload;
  stop_on_config_error?: boolean;
}

export type ReasonCode =
  | "GLOBAL_DAY_CAPACITY_SHORTAGE"
  | "GLOBAL_SHIFT_ALLOWED_SHORTAGE"
  | "CAPACITY_TOTAL_SHORTAGE"
  | "TEAM_MIN_EXCEEDS_GLOBAL_NEED"
  | "TEAM_SIZE_INSUFFICIENT"
  | "TEAM_ACTIVE_MEMBERS_INSUFFICIENT"
  | "TEAM_SHIFT_ALLOWED_SHORTAGE"
  | "GRADE_MIN_SUM_EXCEEDS_NEED"
  | "GRADE_MAX_SUM_BELOW_NEED"
  | "GRADE_MIN_AVAILABLE_SHORTAGE"
  | "GRADE_ANTIPAIR_FORCES_SHORTAGE"
  | "TEAM_GRADE_INTERSECT_SHORTAGE"
  | "MID_REQUIRED_MISSING"
  | "MID_DISABLED_BUT_USED"
  | "MONTHLY_NIGHT_CAPACITY_SHORTAGE"
  | "ALLOWED_SHIFTS_ISOLATES_NURSE"
  | "FIXED_ASSIGN_EXCEEDS_NEED"
  | "FIXED_ASSIGN_VIOLATES_ALLOWED"
  | "FIXED_ASSIGN_BREAKS_TEAM_MIN"
  | "FIXED_OFF_EXCEEDS_SPAN";

export interface PrecheckIssue {
  reason_code: ReasonCode;
  severity: "hard";
  evidence: Record<string, unknown>;
}

export interface PrecheckResponse {
  status: "OK" | "HAS_ISSUES";
  issues: PrecheckIssue[];
}
```

---

## 5. API 호출 예시

### 5.1 fetch 기반 단순 함수 (axios 사용 시 변환 쉬움)

```ts
// api/precheck.ts
import type { PrecheckRequest, PrecheckResponse } from "@/types/precheck";

export async function runPrecheck(
  groupId: string,
  payload: PrecheckRequest
): Promise<PrecheckResponse> {
  const res = await fetch(`/groups/${groupId}/roster/precheck`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`precheck ${res.status}: ${await res.text()}`);
  return res.json();
}
```

### 5.2 React Query Hook

```tsx
// hooks/usePrecheck.ts
import { useMutation } from "@tanstack/react-query";
import { runPrecheck } from "@/api/precheck";
import type { PrecheckRequest, PrecheckResponse } from "@/types/precheck";

export function usePrecheck(groupId: string) {
  return useMutation<PrecheckResponse, Error, PrecheckRequest>({
    mutationFn: (payload) => runPrecheck(groupId, payload),
  });
}
```

### 5.3 설정 화면 통합 예시 (debounce)

```tsx
// pages/RosterPreviewPage.tsx
import { useEffect, useState, useMemo } from "react";
import { useDebounce } from "@/hooks/useDebounce";
import { usePrecheck } from "@/hooks/usePrecheck";
import { buildPrecheckRequest } from "@/utils/buildPrecheckRequest";
import { PrecheckIssueList } from "@/components/PrecheckIssueList";

export default function RosterPreviewPage({ groupId }: { groupId: string }) {
  const [nurses, teams, rosterConfig, teamCoverage, gradeConstraints] = useSetupState();

  const request = useMemo(
    () => buildPrecheckRequest({ nurses, teams, rosterConfig, teamCoverage, gradeConstraints }),
    [nurses, teams, rosterConfig, teamCoverage, gradeConstraints],
  );
  const debouncedRequest = useDebounce(request, 400);

  const { mutate, data, isPending } = usePrecheck(groupId);

  useEffect(() => {
    if (debouncedRequest) mutate(debouncedRequest);
  }, [debouncedRequest, mutate]);

  const canGenerate = data?.status === "OK" && !isPending;

  return (
    <>
      <PrecheckIssueList loading={isPending} response={data} />
      <button disabled={!canGenerate} onClick={handleGenerate}>
        근무표 생성
      </button>
    </>
  );
}
```

---

## 6. reason_code 카탈로그 — 한국어 메시지 + UI 가이드

각 코드에 `messageKo` 템플릿과 `actionHint`(사용자 action 유도 문구)를 매핑해 두면 유지보수가 쉽다.

```ts
// constants/precheckMessages.ts
import type { ReasonCode } from "@/types/precheck";

type Meta = {
  title: string;
  messageKo: (e: Record<string, any>) => string;
  actionHint: string;
  group: "config" | "capacity" | "team" | "grade" | "fixed" | "crossover";
};

export const PRECHECK_META: Record<ReasonCode, Meta> = {
  MID_REQUIRED_MISSING: {
    title: "M 시프트 요구량 미설정",
    messageKo: () => "use_mid 가 켜져 있지만 daily_shift_requirements.M 이 비어 있습니다.",
    actionHint: "M 시프트 요구 인원 수를 지정하거나 use_mid 를 끄세요.",
    group: "config",
  },
  MID_DISABLED_BUT_USED: {
    title: "use_mid=false 인데 M 제약 설정됨",
    messageKo: (e) => `다음 설정에 M 이 포함되어 있습니다: ${(e.offending_keys as string[]).join(", ")}`,
    actionHint: "M 관련 설정을 제거하거나 use_mid 를 켜세요.",
    group: "config",
  },
  ALLOWED_SHIFTS_ISOLATES_NURSE: {
    title: "근무 가능 시프트 없음",
    messageKo: (e) => `간호사 ${e.nurse_id} 의 근무 가능 시프트가 없습니다 (active ${e.active_days}일 > 필요 OFF ${e.required_off_days}일).`,
    actionHint: "해당 간호사의 야간 전담/가능 시프트 설정을 확인하세요.",
    group: "config",
  },
  FIXED_OFF_EXCEEDS_SPAN: {
    title: "고정 OFF 가 근무 기간 전체",
    messageKo: (e) => `간호사 ${e.nurse_id} 의 고정 OFF ${e.fixed_off_count}일이 근무 기간 ${e.span}일 전체를 점유합니다.`,
    actionHint: "고정 OFF 를 줄이거나 근무 기간을 조정하세요.",
    group: "fixed",
  },
  TEAM_SIZE_INSUFFICIENT: {
    title: "팀 크기 부족",
    messageKo: (e) => `팀 ${e.team_id} 인원(${e.team_size}) < 팀 최소 합(${e.team_min_sum}).`,
    actionHint: "팀에 인원을 추가하거나 team_min 을 낮추세요.",
    group: "team",
  },
  GRADE_MIN_SUM_EXCEEDS_NEED: {
    title: "등급별 최소 합이 요구량 초과",
    messageKo: (e) => `${e.day+1}일 ${e.shift}: 등급 최소 합(${e.min_sum}) > 전역 요구(${e.need}).`,
    actionHint: "등급별 최소 인원을 낮추거나 전역 요구량을 늘리세요.",
    group: "grade",
  },
  FIXED_ASSIGN_EXCEEDS_NEED: {
    title: "고정 배정이 요구량 초과",
    messageKo: (e) => `${e.day+1}일 ${e.shift}: 고정 배정 ${e.fixed_count}명 > 요구 ${e.need}명.`,
    actionHint: "해당 일자의 고정 근무를 줄이세요.",
    group: "fixed",
  },
  FIXED_ASSIGN_VIOLATES_ALLOWED: {
    title: "고정 배정이 허용 시프트 밖",
    messageKo: (e) => `간호사 ${e.nurse_id} 의 ${e.day+1}일 ${e.assigned_shift} 고정 배정이 허용 시프트 ${JSON.stringify(e.allowed)} 밖.`,
    actionHint: "고정 배정 또는 허용 시프트를 수정하세요.",
    group: "fixed",
  },
  GLOBAL_DAY_CAPACITY_SHORTAGE: {
    title: "일자별 총 인원 부족",
    messageKo: (e) => `${e.day+1}일: 요구 ${e.required_total}명 > 근무 가능 ${e.available_nurses}명.`,
    actionHint: "해당 일자에 인원을 추가하거나 요구량을 줄이세요.",
    group: "capacity",
  },
  GLOBAL_SHIFT_ALLOWED_SHORTAGE: {
    title: "시프트별 허용 인원 부족",
    messageKo: (e) => `${e.day+1}일 ${e.shift}: 요구 ${e.required}명 > 허용 인원 ${e.allowed_nurses}명.`,
    actionHint: "해당 시프트 허용 간호사를 늘리거나 요구량을 줄이세요.",
    group: "capacity",
  },
  CAPACITY_TOTAL_SHORTAGE: {
    title: "월 전체 근무 용량 부족",
    messageKo: (e) => `월 요구 ${e.required_total} 일·명 > 용량 ${e.capacity_total} 일·명 (${e.nurse_count}명 * ${e.num_days}일 기반).`,
    actionHint: "간호사를 추가하거나 OFF 일수를 줄이거나 요구량을 낮추세요.",
    group: "capacity",
  },
  TEAM_MIN_EXCEEDS_GLOBAL_NEED: {
    title: "팀 최소 합이 전역 요구 초과",
    messageKo: (e) => `${e.day+1}일 ${e.shift}: 모든 팀 최소 합(${e.teams_min_sum}) > 전역 요구(${e.global_need}).`,
    actionHint: "팀별 최소값을 낮추거나 전역 요구량을 늘리세요.",
    group: "team",
  },
  TEAM_ACTIVE_MEMBERS_INSUFFICIENT: {
    title: "팀 활성 인원 부족",
    messageKo: (e) => `팀 ${e.team_id} ${e.day+1}일: 활성 ${e.active_count}명 < 최소 합 ${e.required_min_sum}명.`,
    actionHint: "팀 인원/일정을 재조정하세요.",
    group: "team",
  },
  TEAM_SHIFT_ALLOWED_SHORTAGE: {
    title: "팀 내 시프트 허용자 부족",
    messageKo: (e) => `팀 ${e.team_id} ${e.day+1}일 ${e.shift}: 요구 ${e.required}명 > 허용자 ${e.allowed_count}명.`,
    actionHint: "팀원의 허용 시프트를 확장하거나 팀 최소값을 낮추세요.",
    group: "team",
  },
  GRADE_MAX_SUM_BELOW_NEED: {
    title: "등급 상한 합이 요구량 미달",
    messageKo: (e) => `${e.day+1}일 ${e.shift}: 상한 합(${e.capped_sum}) + free(${e.free_capacity}) < 요구(${e.need}).`,
    actionHint: "등급 상한을 완화하거나 해당 시프트 요구량을 줄이세요.",
    group: "grade",
  },
  GRADE_MIN_AVAILABLE_SHORTAGE: {
    title: "등급별 허용 인원 부족",
    messageKo: (e) => `${e.day+1}일 ${e.shift} Grade ${e.grade}: 요구 ${e.required}명 > 허용 ${e.available}명.`,
    actionHint: "해당 등급의 허용 시프트를 확인하세요.",
    group: "grade",
  },
  GRADE_ANTIPAIR_FORCES_SHORTAGE: {
    title: "Anti-pair 상한이 요구량 차단",
    messageKo: (e) => `${e.day+1}일 ${e.shift} Grade ${e.grade} 상한(${e.max})으로는 요구(${e.need}) 못 채움. 비해당 등급 허용자 ${e.non_grade_available}명.`,
    actionHint: "Grade 상한을 완화하거나 비해당 등급의 해당 시프트 허용자를 늘리세요.",
    group: "grade",
  },
  TEAM_GRADE_INTERSECT_SHORTAGE: {
    title: "팀 × 등급 교차 불가",
    messageKo: (e) => `팀 ${e.team_id} ${e.day+1}일 ${e.shift}: 등급 상한을 고려한 유효 배정 가능 ${e.effective_capacity}명 < 요구 ${e.required}명.`,
    actionHint: "팀 구성을 분산하거나 등급 상한을 완화하세요.",
    group: "crossover",
  },
  MONTHLY_NIGHT_CAPACITY_SHORTAGE: {
    title: "월간 N 근무 용량 부족",
    messageKo: (e) => `N 허용 ${e.night_allowed_count}명의 월 용량(${e.night_capacity}) < 월 N 요구(${e.monthly_N_need}).`,
    actionHint: "N 허용 간호사를 추가하거나 N 요구량을 낮추세요.",
    group: "capacity",
  },
  FIXED_ASSIGN_BREAKS_TEAM_MIN: {
    title: "팀 고정 OFF 과다",
    messageKo: (e) => `팀 ${e.team_id} ${e.day+1}일: 가용 ${e.remaining_members}명 < 최소 합 ${e.required_min_sum}명.`,
    actionHint: "고정 OFF 를 분산하거나 팀 최소값을 낮추세요.",
    group: "crossover",
  },
};
```

### 메시지/액션 힌트 작성 시 주의

- `day` 는 0-based 이므로 UI 표시는 `day + 1` 또는 `format(base + day, "MM-dd")` 로 변환
- `evidence` 값은 정수/문자열로만 구성됨 (파싱 불필요)
- 같은 원인이 여러 일자에 반복 감지되면 UI 에서 **코드별 그룹핑 + 일자 리스트** 로 접는 것을 권장

---

## 7. UI 컴포넌트 예시

```tsx
// components/PrecheckIssueList.tsx
import { PRECHECK_META } from "@/constants/precheckMessages";
import type { PrecheckResponse, PrecheckIssue } from "@/types/precheck";

type GroupedIssues = Record<string, PrecheckIssue[]>;

function groupByCode(issues: PrecheckIssue[]): GroupedIssues {
  return issues.reduce<GroupedIssues>((acc, cur) => {
    (acc[cur.reason_code] ??= []).push(cur);
    return acc;
  }, {});
}

export function PrecheckIssueList({
  loading,
  response,
}: {
  loading: boolean;
  response?: PrecheckResponse;
}) {
  if (loading) return <div>검증 중…</div>;
  if (!response) return null;
  if (response.status === "OK") {
    return <div className="ok">생성 가능 ✓</div>;
  }

  const grouped = groupByCode(response.issues);

  return (
    <div className="precheck-issues">
      <h3>근무표 생성 전 확인 필요: {response.issues.length}건</h3>
      {Object.entries(grouped).map(([code, issues]) => {
        const meta = PRECHECK_META[code as keyof typeof PRECHECK_META];
        if (!meta) return null;
        return (
          <div key={code} className={`issue issue--${meta.group}`}>
            <div className="issue__title">{meta.title} ({issues.length})</div>
            <ul>
              {issues.slice(0, 5).map((i, idx) => (
                <li key={idx}>{meta.messageKo(i.evidence)}</li>
              ))}
              {issues.length > 5 && <li>… 외 {issues.length - 5}건</li>}
            </ul>
            <div className="issue__action">→ {meta.actionHint}</div>
          </div>
        );
      })}
    </div>
  );
}
```

### CSS 그룹 색상 권장

```css
.issue--config    { border-left: 4px solid #e53935; }  /* 빨강: 설정 오류 */
.issue--capacity  { border-left: 4px solid #f57c00; }  /* 주황: 용량 부족 */
.issue--team      { border-left: 4px solid #1976d2; }  /* 파랑: 팀 */
.issue--grade     { border-left: 4px solid #7b1fa2; }  /* 보라: 등급 */
.issue--fixed     { border-left: 4px solid #0097a7; }  /* 청록: 고정 배정 */
.issue--crossover { border-left: 4px solid #6d4c41; }  /* 갈색: 교차 */
```

---

## 8. payload 조립 유틸

프론트가 이미 갖고 있는 DTO 를 PrecheckRequest 로 변환하는 빌더 예시:

```ts
// utils/buildPrecheckRequest.ts
import type { PrecheckRequest } from "@/types/precheck";
import { differenceInDays, parseISO, startOfMonth, endOfMonth } from "date-fns";

export function buildPrecheckRequest(input: {
  year: number;
  month: number;
  nurses: NurseDTO[];
  teams: TeamDTO[];
  rosterConfig: RosterConfigDTO;
  teamCoverage: TeamCoverageDTO;
  gradeConstraints: GradeConstraintsDTO;
  fixedAssignments: FixedAssignmentDTO[];
}): PrecheckRequest {
  const monthStart = startOfMonth(new Date(input.year, input.month - 1, 1));
  const monthEnd = endOfMonth(monthStart);
  const num_days = differenceInDays(monthEnd, monthStart) + 1;

  const byNurse = groupFixedByNurse(input.fixedAssignments, monthStart);

  return {
    num_days,
    nurses: input.nurses.map((n) => {
      const joiningDate = n.joining_date ? parseISO(n.joining_date) : monthStart;
      const resignDate = n.resignation_date ? parseISO(n.resignation_date) : null;
      const join_day = Math.max(0, differenceInDays(joiningDate, monthStart));
      const leave_day = resignDate
        ? Math.min(num_days - 1, differenceInDays(resignDate, monthStart))
        : num_days - 1;
      const nf = byNurse.get(n.nurse_id) ?? { off_days: [], shifts: {} };
      return {
        nurse_id: n.nurse_id,
        grade: n.grade ?? null,
        team_id: n.team_id ?? null,
        allowed_shifts: (n.is_night_nurse as string[] | null) ?? null,
        join_day,
        leave_day,
        personal_off_adjustment: n.personal_off_adjustment ?? 0,
        fixed_off_days: nf.off_days,
        fixed_shift_assignments: nf.shifts,
      };
    }),
    teams: input.teams.map((t) => t.team_id),
    roster_config: {
      use_mid: input.rosterConfig.use_mid,
      daily_shift_requirements: input.rosterConfig.daily_shift_requirements,
      daily_shift_requirements_by_day: input.rosterConfig.daily_shift_requirements_by_day ?? null,
      global_monthly_off_days: input.rosterConfig.global_monthly_off_days,
      standard_personal_off_days: input.rosterConfig.standard_personal_off_days,
    },
    team_coverage: input.teamCoverage,
    grade_constraints: {
      minimum_by_shift: input.gradeConstraints.minimum_by_shift ?? {},
      max_by_shift: input.gradeConstraints.max_by_shift ?? {},
    },
    stop_on_config_error: false,
  };
}
```

---

## 9. UX 상세 가이드

### 9.1 언제 호출하는가

| 상황 | precheck 호출 | 사용자에 대한 효과 |
|---|---|---|
| 설정 화면 진입 (최초 로드) | 호출 | 전체 상태 파악 |
| 팀 편성 변경 | debounced 호출 (~400ms) | 실시간 피드백 |
| Grade 제약 수정 | debounced 호출 | 실시간 피드백 |
| Roster Config 편집 | debounced 호출 | 실시간 피드백 |
| 고정 OFF/배정 추가 | debounced 호출 | 실시간 피드백 |
| 생성 버튼 클릭 직전 | 동기 호출 (debounce 무시) | 최종 확인 |

### 9.2 생성 버튼 제어

```ts
const canGenerate =
  data?.status === "OK" ||
  // 선택: hard 가 아닌 severity 가 미래에 추가되면 allow
  (data?.issues?.every((i) => i.severity !== "hard") ?? true);
```

현재 모든 issue 는 `severity: "hard"` — OK 가 아닌 이상 전체 비활성화 권장.

### 9.3 로딩 스켈레톤

debounced call 이 in-flight 일 때는 이전 결과를 희미하게(opacity 0.6) 표시하되 생성 버튼은 disabled 유지.

### 9.4 에러 처리

- `4xx/5xx`: 토스트로 "검증 서버 통신 실패" 표시. 생성 버튼은 **비활성** (unknown 상태에서 생성 허용 금지).
- 네트워크 실패: 재시도 버튼 제공.

---

## 10. Q&A

**Q1. 같은 원인이 30일 내내 반복되면 UI 가 너무 길어지지 않나?**
A. `groupByCode` + "외 N건" 접기 추천. 상위 5개 + 접기 패턴.

**Q2. `GRADE_ANTIPAIR_FORCES_SHORTAGE` 와 `GRADE_MAX_SUM_BELOW_NEED` 가 같이 뜰 것 같은데?**
A. 백엔드에서 dedup — 같은 `(shift, day)` 에서는 `ANTIPAIR` 만 남고 `MAX_SUM_BELOW_NEED` 는 suppress. 프론트 추가 처리 불필요.

**Q3. `use_mid` 값을 프론트에서 수정하고 싶다면?**
A. 본 엔드포인트는 `use_mid` 를 **읽기 전용** 으로 가정. 수정 UI 가 필요하면 별도 `POST /groups/{id}/roster-config` 호출 후 precheck 재실행.

**Q4. payload 가 너무 큼. 최소화 가능?**
A. `teams` 필드는 현재 검증에 사용되지 않아 빈 배열 전달 가능. `fixed_assignments` 가 없는 간호사는 `fixed_off_days: []`, `fixed_shift_assignments: {}` 로 생략할 수 있음.

**Q5. 결과가 "OK" 인데 생성 시 실패할 수 있나?**
A. 가능. precheck 는 **결정적 infeasibility** 만 감지. soft 제약 충돌·연속근무 패턴 등 솔버 탐색 실패는 precheck 범위 밖. 이 경우 백엔드가 별도 diagnostics 를 반환한다.

**Q6. 번역이 필요하다면?**
A. `messageKo` 를 i18n 키로 대체하면 된다. evidence 는 i18n 인자로 전달:
```ts
t("precheck.TEAM_MIN_EXCEEDS_GLOBAL_NEED", { day: e.day+1, shift: e.shift, sum: e.teams_min_sum, need: e.global_need })
```

---

## 11. 체크리스트 (프론트 개발자용)

- [ ] 타입 정의 복사 (`types/precheck.ts`)
- [ ] API 함수 추가 (`api/precheck.ts`)
- [ ] React Query Hook 래퍼 (`hooks/usePrecheck.ts`)
- [ ] payload 빌더 (`utils/buildPrecheckRequest.ts`) — DTO → PrecheckRequest
- [ ] 메시지 맵 (`constants/precheckMessages.ts`) — 모든 reason_code 커버
- [ ] IssueList 컴포넌트 (`components/PrecheckIssueList.tsx`)
- [ ] 설정 화면 debounce 통합
- [ ] 생성 버튼 disabled 바인딩
- [ ] 에러 핸들링 & 재시도
- [ ] i18n 준비 (선택)

---

## 12. 문서 히스토리

| 날짜 | 변경 |
|---|---|
| 2026-04-17 | 최초 작성. 20개 reason_code 커버 (E-1 rename, G-1/F-4 신설, dedup 정책 포함) |
