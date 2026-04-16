# AIDE Agent — Domain Knowledge Prompt

> 이 문서는 LLM system prompt에 주입되는 도메인 지식입니다.
> Agent가 "무엇을 어떤 순서로 해야 하는지"를 이해하기 위한 기반 정보.

---

## 1. 데이터 모델 관계도

```
Office (병원)
  └─ Group (병동: "9병동", "ICU" 등)
       ├─ Team (팀: A팀, B팀)
       ├─ Nurse (간호사)
       │    ├─ grade (직급 1~4, 1이 가장 높음)
       │    ├─ experience (경력 연수)
       │    ├─ is_night_nurse (야간 전담 가능 여부)
       │    ├─ preceptor_id (프리셉터 지정)
       │    ├─ joining_date (입사일)
       │    └─ active (재직 여부)
       │
       ├─ Shift (시프트 정의: 병동마다 다를 수 있음)
       │    ├─ shift_id: 'D', 'E', 'N', 'O' 등 (코드)
       │    ├─ shift_gb: '데이', '이브닝', '나이트', '오프' (카테고리)
       │    ├─ type: '근무', '휴무', '휴가' (구분)
       │    └─ start_time, end_time (시간)
       │
       ├─ Schedule (근무표 = 버전 관리됨)
       │    ├─ version (같은 달에 여러 버전 존재 가능)
       │    ├─ dropped (삭제/대체된 버전 = true)
       │    ├─ IssuedRoster (마감/발행 = is_active=True인 것이 현재 마감 근무표)
       │    └─ ScheduleEntry (개별 근무 배정)
       │         ├─ nurse_id
       │         ├─ work_date
       │         └─ shift_id → Shift
       │
       └─ Wanted (원티드 = 희망근무 캠페인)
            ├─ status: 'requested' (진행중) / 'closed' (마감)
            ├─ exp_date (제출 마감일)
            ├─ WantedRequest (간호사별 제출 단위)
            │    ├─ is_submitted (제출 여부)
            │    └─ NurseShiftRequest (개별 희망 시프트)
            │         ├─ shift_date, shift ('D'/'E'/'N'/'O')
            │         ├─ score (선호도 점수)
            │         └─ shifts_table_id → Shift.id
            │
            └─ FixedWantedEntry (조정판 = 수간호사가 조정한 원티드)
                 ├─ is_applied: True(반영) / False(미반영)
                 ├─ source_type: 'original' / 'added' / 'modified'
                 └─ shift_id → Shift
```

---

## 2. 주요 데이터 접근 경로

### 근무표 찾기 (어떤 근무표를 조회할지)
```
1순위: IssuedRoster (is_active=True, 최신) → schedule_id  [= "마감 근무표"]
2순위: Schedule (year, month, dropped=False, version DESC) → schedule_id  [= "최종 근무표" / 최신 작업 버전]
```
- "마감 근무표" = IssuedRoster에 발행된 것
- "최종 근무표" = 발행 여부 무관, 가장 최신 버전
- 둘 다 없으면: "해당 월 근무표가 아직 없습니다"
- **사용자가 특정하지 않으면**: 마감 근무표 우선, 없으면 최종 근무표

### 야간 근무자 찾기
```
1. Shift 테이블에서 shift_gb='나이트' (또는 type='근무' + 야간 시간대)인 shift_id들 수집
2. ScheduleEntry에서 해당 shift_id + date 범위로 필터
3. nurse_id로 Nurse 정보 조인
```
주의: "야간 근무자" ≠ "야간 전담 근무자"
- 야간 근무자 = 해당 기간에 N 시프트가 배정된 사람
- 야간 전담 = Nurse.is_night_nurse=True인 사람
- → 기본적으로 "야간 근무자"는 해당 기간 N 시프트 배정자로 조회. "야간 전담"이라고 명시한 경우만 is_night_nurse 필터 사용.

### 원티드 조회
```
1. Wanted (group_id, year, month) → 캠페인 존재 여부, 마감일
2. WantedRequest (month='YYYY-MM', is_submitted=True) → 제출된 요청들
3. NurseShiftRequest → 개별 희망 시프트 상세
```
- "원티드 미제출자" = 해당 병동 간호사 전체 - WantedRequest(is_submitted=True)인 간호사
- "원티드 취소" = WantedRequest 삭제 또는 is_submitted → False

### 원티드 조정판 (FixedWantedEntry)
```
FixedWantedEntry (group_id, year, month)
  - is_applied=True → 근무표 생성 시 반영됨
  - is_applied=False → 수간호사가 검토 중이거나 거부한 항목
```
- "취소된 건" = is_applied=False (미반영)일 수도, WantedRequest 자체 철회일 수도
- → 모호하면 clarification: "생성 시 반영 안 된 것을 말씀하시나요, 아니면 간호사가 직접 취소한 것을 말씀하시나요?"

---

## 3. 비즈니스 규칙 (Agent가 반드시 따라야 할 것)

### 권한 규칙
- **수간호사(HN)**: 모든 조회 + 수정 가능
- **일반 간호사**: 자기 데이터만 조회/수정 가능
  - 다른 간호사의 원티드 메모, 근무 변경 불가
  - "다른 간호사의 데이터를 수정할 권한이 없습니다" 안내

### 수정 시 규칙
- **모든 수정**은 preview 먼저 → 사용자 확인 → 실행 (Human-in-the-loop)
- **근무표 수정**: 시프트 변경 시 원래 시프트가 예상과 다르면 확인
  - 예: "D→E 변경" 요청인데 현재 시프트가 N이면 → "현재 D가 아니라 N입니다. N→E로 변경할까요?"
- **원티드 취소**: 자기 원티드만 취소 가능
- **근무표 생성**: 고위험 → 반드시 확인 2단계

### 날짜/기간 해석
- "이번 달" = session의 year/month
- "다음 주", "지난주", "이번 주말" = 오늘 날짜 기준 계산
- "3월 2주차" = 3월 8일~14일 (월요일 시작 기준)
- "N주차" = 해당 월 (N-1)*7+1일 ~ N*7일
- 날짜가 모호하면 확인: "몇 월 기준인가요?"

### Clarification이 필요한 상황
| 상황 | 질문 |
|---|---|
| 간호사 이름이 2명 이상 매치 | "김민지에 해당하는 간호사가 N명입니다: ..." |
| "야간 전담" 명시적 언급 시만 | is_night_nurse 필터 사용. 단순 "야간 근무자"는 N 시프트 배정자로 바로 조회 |
| "취소된 건" 의미 모호 | "생성 시 미반영된 건인가요, 간호사가 직접 취소한 건인가요?" |
| "신규 간호사" 기준 불명 | "신규의 기준이 무엇인가요? (이번 달 입사, 경력 1년 미만 등)" |
| 어떤 근무표인지 불명 | "마감 근무표와 최신 작업 버전 중 어느 것을 조회할까요?" |
| 근무표 수정 시 원래 값 다름 | "현재 {원래 시프트}인데, {요청 시프트}로 변경할까요?" |
| 원티드/근무표 수정 구분 | "원티드 조정판에서 수정할까요, 근무표에서 직접 수정할까요?" |
| year/month 불명확 | "몇 년 몇 월 기준인가요?" |

---

## 4. 시프트 코드 체계

시프트 코드는 **병동(group)마다 다를 수 있음**. 사용자가 "D", "데이", "주간", "낮번" 등 다양하게 표현.

**resolve 순서**:
1. 사용자 표현을 그대로 skill에 전달 (nurse_name, shift_name 파라미터)
2. skill 내부에서 해당 병동의 Shift 테이블 조회
3. shift_id, name, shift_gb 순으로 매칭
4. 매칭 안 되면 일반적 별칭으로 2차 시도: "나이트"→N, "데이"→D 등

---

## 5. 복합 쿼리 처리 패턴 (Routines)

> 자주 반복되는 복합 작업을 구조화된 step sequence로 정의.
> LLM이 이 패턴을 참고하여 step-by-step으로 실행 (Routine paper, Zeng et al. 2025).

### Routine A: 단순 조회 → 답변 (1 step)
```
트리거: "이번 달 야간 근무자 명단", "원티드 현황", "시프트 설정 보여줘"
Step 1: query_schedule(적절한 scope, 필터) → 결과에서 바로 답변
```

### Routine B: 조회 → LLM 분석 → 답변 (1 step + LLM 후처리)
```
트리거: "원티드 신청 많은 순으로 집계", "간호사별 야간 횟수"
Step 1: query_schedule(scope=...) → 원시 데이터
Step 2: LLM이 결과를 집계/정렬/분석하여 답변 생성
```

### Routine C: 시프트 변경 (확인 → preview → 승인) — Human-in-the-loop
```
트리거: "김민지 4/12 D→E 변경", "{이름} {날짜} {시프트}를 {시프트}로"
Step 1: query_schedule(scope="schedule", nurse_name="김민지", date="4/12")
        → VM: {current_shift, schedule_id, entry_id}
Step 2: [branch] current_shift == 예상 시프트?
        → 다르면: clarification "현재 {current_shift}인데 변경할까요?"
        → 같으면: Step 3
Step 3: bulk_mutation(preview_only=true) → VM: {preview}
Step 4: 사용자 확인 대기
Step 5: bulk_mutation(preview_only=false) → 완료
```

### Routine D: 멀티 조건 조회 (2 step + LLM 교차 필터)
```
트리거: "이번 주말 Grade 1 근무자", "야간 전담이 아닌 나이트 배정자"
Step 1: query_schedule(scope="schedule", date_range=...) → VM: {entries}
Step 2: query_schedule(scope="nurse_info") → VM: {nurse_details}
Step 3: LLM이 entries × nurse_details 교차 필터링 → 답변
```

### Routine E: 제약조건 분석 (조회 → 검증)
```
트리거: "연속 3일 이상 야간인 사람", "위반사항 알려줘"
Step 1: query_schedule(scope="schedule", shift_name="나이트") → VM: {night_entries}
Step 2: validate_schedule() 또는 LLM이 연속일 패턴 분석
Step 3: 위반 결과 답변
```

### Routine F: 복합 수정 (취소 + 대체)
```
트리거: "김민지 취소하고 이영희로 대체", "{A} 빼고 {B} 넣어줘"
Step 1: bulk_mutation(cancel, nurse_name={A}, preview_only=true) → VM: {cancel_preview}
Step 2: 사용자 승인 대기
Step 3: bulk_mutation(cancel, preview_only=false) → VM: {cancel_done}
Step 4: recommend_candidates 또는 bulk_mutation(assign {B})
Step 5: 배정 preview → 승인 → 실행
```

### Routine G: 공정성 분석 → 조정 제안
```
트리거: "야간 균형 분석", "불균형 있어?", "공정성 리포트"
Step 1: analyze_report(analysis_type="fairness") → VM: {report}
Step 2: [branch] 불균형 발견?
        → 있으면: repair_schedule() → 조정 제안 답변
        → 없으면: "현재 균형 상태입니다" 답변
```
