# AIDE Agent QA 시나리오 — 2026-05-18

> 본 문서는 5월 18일자 test query 초안에 **남준 코멘트**를 반영한 QA 시나리오 정의서다.
> 채택/수정/제거 의사결정과 그 근거(코멘트)를 명시하고, 각 시나리오에 검증 포인트와
> pass/fail 기준을 부여한다. 자동화 (pytest) 변환 전 단계의 사양 문서로 사용한다.

---

## 0. 코멘트 기반 핵심 결정 요약

### 0.1 DROP — QA 시나리오에서 제외
> 이유: "수간호사가 안 할 질문" / "안 나올 질문"

| ID | 원본 query | 제외 근거 (남준) |
|---|---|---|
| A2 | "팀 밸런스 켜져 있어?" | 수간호사가 안 함 |
| B2 | "팀 밸런스 켜줘" | 동일 |
| B6 | "프리셉터 게이지 7로 조정" | 직접 게이지 조정은 없음 |
| H5 | "5월 한도 설정된 간호사 전체 보여줘" | 안 나올 질문 |
| I5 | "팀 밸런스 켜고 5월 근무표 다시 만들어" | 팀 밸런스는 묻지 않음 |
| J 섹션 전체 | ShiftManage 슬롯 | nurse_class는 `etc` 구분 외엔 안 씀 |

### 0.2 MODIFY — 기대 동작 변경
> 사용자 코멘트로 인해 원본 답안과 다르게 동작해야 하는 항목.

| ID | 변경 내용 |
|---|---|
| A1 | `max_nig_per_month` 외에 **`n_exact` 별도 안내** 필요 (OPEN — §0.4 참조) |
| A3 | "지금" = **오늘 날짜 기준 daily shift by day**로 해석 |
| **B1** | **변경 불가 정책**. UI상 max 15, 풀어도 17까지. agent는 **거절 + 정책 안내** |
| B3 | 가능: 4일 등 단축 가능 / 추후 **nurse별 제한** 검토 (OPEN) |
| C3 | 전체 + **개인 단위 한도**도 함께 고려 |
| C5 | **연/월 명확화**를 응답에 포함 |
| D1 | 해당 원티드 **없을 경우 처리** + **nurse 이름 매칭 검증** |
| D5 | 단일 항목 삭제도 **재확인 요구** |
| E1 | **`group_id` 필터 필수 적용** (신규 추가됨) |
| E2 | **연도 미지정 시 현재 연도 기본값** |
| E4 | **마감 후에도** 변경 내역 조회 허용 |
| E5 | **`group_id` 필터 적용** |
| F2 | UI 표현은 추후 디자인 (OPEN) |
| G3 | **산술 스킬** 추가 + 산술 에러 시 **불가 처리** |
| H1~H4 | 해당 시프트 **불가 nurse** 요청은 **코멘트만 + 저장 X** |
| I1 | 박혜미 5/10 원티드 없으면 **그대로 김민지 배치 + 사실 언급** |
| I2 | 빈도 낮으나 진행. **유사 패턴에도 일반화** |
| I4 | **개인별 마감일 연장 없음**. → "취소하고 5/25까지 연장할까요?" clarify |
| I6 | 이미 팀이 바뀌어 있어도 **재이동 + 그 사실 언급** |
| I7 | **수정 방법 없을 때 처리 정의** 필요 (OPEN) |
| K3 | 본인 wanted_max도 **거절** ("나"여도 변경 불가) |
| L4 | error 응답 대신 **"없습니다" 자연어 응답** |

### 0.3 KEPT — 원안 그대로
A4, A5, B4, B5, C1, C2, C4, D2, D3, D4, E3, F1, F3, F4, G1, G2, I3, I8, K1, K2, K4, L1, L2, L3

### 0.4 OPEN ISSUES — 설계/구현 결정 필요
| # | 이슈 | 영향 시나리오 |
|---|---|---|
| O-1 | `n_exact` 별도 안내 방식 (UI/응답 포맷) | A1 |
| O-2 | "오늘 기준 daily shift by day" 조회를 어느 skill에 매핑? | A3 |
| O-3 | nurse별 연속 근무 제한 (`max_conseq_work` per-nurse) 지원 시점 | B3 |
| O-4 | wanted_config 개인 단위 한도 노출 | C3 |
| O-5 | 산술 검증 skill (서비스/라우터 레벨) 위치 | G3 |
| O-6 | 시프트 불가 nurse 판정 로직 (per-nurse 가능 시프트 메타) | H1-H4 |
| O-7 | repair 실패 시 사용자 응대 (대안 제시? 수동 가이드?) | I7 |
| O-8 | 1팀 nurse 목록 UI 표현 (텍스트 정렬? 카드?) | F2 |

---

## 1. QA 시나리오 (섹션별)

### 형식
- **ID**: 섹션 + 번호
- **Query**: 사용자 발화
- **기대 동작**: 코멘트 반영된 최종 정답
- **검증 포인트**: skill 호출/grounding/응답 조건
- **Pass 기준**: 모두 만족 시 통과
- **Fail 시그널**: 명백한 회귀
- **비고**: OPEN 이슈 / 제약

---

### A. 설정 조회 (settings_read)

#### A1 — 야간 최대 횟수 조회
- **Query**: "5월 야간 최대 횟수 설정 보여줘"
- **기대 동작**: `query_schedule(scope=constraint_config)` 호출. `max_nig_per_month` 값 + `n_exact`가 설정된 nurse 별도 안내 (개인 한도 우선 적용 명시)
- **검증 포인트**:
  - constraint_config scope 호출 1회
  - 응답에 정책값 + 개인 한도 우선 규칙 언급
- **Pass**: "현재 야간 최대 N회이며, 개인 한도(n_exact)가 설정된 간호사는 그 값을 우선합니다" 형태
- **Fail**: max_nig만 답하고 n_exact 언급 누락
- **비고**: O-1 — 응답 포맷 디테일은 OPEN

#### A3 — 데이 필요인원 (오늘 기준)
- **Query**: "데이 필요인원 지금 몇 명이지?"
- **기대 동작**: "지금" = 오늘 날짜 기준. 오늘의 요일/평일 여부에 따라 daily_shift_by_day의 day_req 값 응답
- **검증 포인트**:
  - 오늘 날짜 grounding (SessionContext.year/month + 오늘 day)
  - daily_shift_by_day 우선 조회, 없으면 일반 day_req fallback
- **Pass**: "오늘({date})은 평일/주말 기준 데이 필요인원 N명입니다"
- **Fail**: 정적 day_req만 답하고 오늘 날짜 미반영
- **비고**: O-2 — skill 매핑 결정 필요

#### A4 — 주2회 오프 보장
- **Query**: "주2회 오프 보장 설정 어떻게 되어있어?"
- **기대 동작**: `query_schedule(scope=constraint_config)` → `two_offs_per_week` (bool) 응답
- **Pass**: "네/아니오" 자연어
- **Fail**: bool 원본 노출

#### A5 — 정책 전체 보기
- **Query**: "스케줄 정책 전체 보여줘"
- **기대 동작**: RosterConfig 전 필드 그룹별 자연어 요약 (시프트별 필요인원 / 연속·휴무 / 구조정책 / 표시옵션)
- **검증 포인트**: 4개 그룹 모두 포함
- **Pass**: 그룹화된 요약 + 핵심 수치
- **Fail**: 단순 dict dump

---

### B. 설정 변경 (settings_update)

#### B1 — 야간 최대 변경 ❌ 거절
- **Query**: "야간 최대 7회로 바꿔줘"
- **기대 동작**: **거절**. agent 응답: "현재 정책상 야간 최대(`max_nig_per_month`)는 시스템 정책으로 고정되어 있어 변경할 수 없습니다. 개인별 한도가 필요하시면 `update_monthly_limit`로 nurse별 설정이 가능합니다."
- **검증 포인트**:
  - update_constraint **호출 안 함**
  - 대안 (per-nurse limit) 제시
- **Pass**: 정책 거절 메시지 + 대안 안내
- **Fail**: update_constraint(preview_only=True) 진입 → 회귀
- **비고**: 🔴 회귀 발생 시 critical (운영 정책 사고)

#### B3 — 연속 근무 제한
- **Query**: "연속 근무 최대 5일로 제한"
- **기대 동작**: `update_constraint(field=max_conseq_work, value=5, preview_only=True)` → confirm 후 적용. 4일 등 단축도 동일 허용.
- **검증 포인트**:
  - preview → confirm → apply 3단계
  - 값 범위 검증 (1 이상)
- **Pass**: 정상 preview/apply 흐름
- **Fail**: 즉시 apply (preview 누락)
- **비고**: O-3 — nurse별 제한은 미지원. 사용자가 nurse 지정 시 "현재 병동 전체 정책만 지원" 안내 필요

#### B4 — 모호한 야간 요청
- **Query**: "야간 늘려줘"
- **기대 동작**: clarification: "야간 최대 횟수와 야간 필요인원 중 어느 쪽을 의미하시나요?"
  - "최대 횟수" 답변 → B1 거절 흐름으로 라우팅
  - "필요인원" 답변 → update_constraint(nig_req) preview
- **Pass**: 모호성 인식 + clarify 발화
- **Fail**: 임의 추측

#### B5 — banned_day_after_eve 해제
- **Query**: "이브닝 다음날 데이 금지 해제해줘"
- **기대 동작**: `update_constraint(banned_day_after_eve=false, preview_only=True)` → confirm 후 적용

---

### C. 원티드 설정 (wanted_config)

#### C1 — 원티드 마감일 변경
- **Query**: "5월 원티드 마감일 5월 25일로 변경"
- **기대 동작**: `bulk_mutation(scope=wanted_submissions, action=update_deadline, new_deadline='2026-05-25', preview_only=True)` → confirm 후 적용
- **Pass**: preview 응답에 기존 마감 vs 새 마감 표시

#### C2 — 마감일 조회 🟡 gap
- **Query**: "5월 원티드 마감일 언제야?"
- **기대 동작**: `query_schedule(scope=wanted_config)` 미구현 — `wanted_submissions` scope로 fallback하여 마감일 추출. 또는 명확한 미지원 안내.
- **Pass**: 마감일 응답 또는 명시적 미지원 안내

#### C3 — AIDE 기능 on/off (전체+개인)
- **Query**: "원티드 AIDE 기능 켜져 있나?"
- **기대 동작**: `wanted_config.enable_aide` (병동 전체) + 개인별 override 존재 여부 함께 응답
- **검증 포인트**: 전체값 + 개인 한도 모두 언급
- **Pass**: "병동 전체 AIDE는 ON/OFF, 개인 단위로 별도 설정된 간호사: ..." 형태
- **비고**: O-4 — 개인 단위 한도 노출 구현 필요

#### C4 — 모호한 한도 질의
- **Query**: "원티드 한도 설정 어떻게 되어 있어?"
- **기대 동작**: clarify ("병동 전체 한도? 특정 간호사 한도?") → 답변 라우팅

#### C5 — 작성 가능 기간 (연/월 명확화)
- **Query**: "원티드 작성 가능 기간 알려줘"
- **기대 동작**: clarify "몇 월 기준입니까?" 또는 SessionContext의 (year, month)로 기본값 채워 응답. 응답 자체에 "{year}년 {month}월 기준" 명시.
- **Pass**: 월 명시 포함 응답

---

### D. 원티드 확정반영 (wanted_adjustment)

#### D1 — 단건 승인 (없을 경우 처리)
- **Query**: "김민지 5월 15일 원티드 승인해줘"
- **기대 동작**:
  1. nurse 이름 grounding (동명이인 시 clarify)
  2. 5/15 김민지 wanted 존재 여부 확인
  3. 존재 → preview → confirm → apply
  4. **부재 → "김민지 간호사의 5/15 원티드 신청이 없습니다" 안내**
- **검증 포인트**:
  - nurse 미존재 시 명확한 안내
  - 존재하지만 잘못 grounding한 경우 검증
- **Pass**: 존재 분기/부재 분기 모두 정상
- **Fail**: 없는데도 success 응답

#### D2 — 단건 거부
- **Query**: "박혜미 5월 10일 원티드 거부"
- **기대 동작**: 동일 흐름의 거부 처리

#### D3 — 시프트 변경 + 승인
- **Query**: "김민지 5월 15일 D를 N으로 변경 후 승인"
- **기대 동작**: `bulk_mutation(scope=wanted_submissions, action=change_shift, preview_only=True)` → D→N modify + approve flag 함께 preview

#### D4 — 대량 일괄 승인 ⚠️
- **Query**: "이번 달 미승인된 원티드 전부 승인 처리"
- **기대 동작**:
  1. 영향 nurse 수 / 항목 수 명시
  2. preview 결과 표시
  3. 사용자 confirm 후 일괄 적용
- **검증 포인트**: 영향 규모 사전 고지

#### D5 — 단건 삭제 (재확인 필수)
- **Query**: "박혜미 5월 5일 원티드 삭제"
- **기대 동작**:
  1. preview 응답
  2. **명시적 재확인 발화** ("정말 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다.")
  3. 사용자 재확인 후 apply
- **Pass**: 재확인 단계 포함
- **Fail**: 단일 preview 후 즉시 apply

---

### E. 원티드 조회 (wanted_read)

#### E1 — 미제출자 조회 (group_id 필수)
- **Query**: "5월 원티드 미제출자 누구야?"
- **기대 동작**: `query_schedule(scope=wanted_submissions, operation=count, year=2026, month=5, group_id={ctx.group_id})`
- **검증 포인트**:
  - **`group_id` 필터 필수**
  - 다른 그룹 nurse 포함되면 fail
- **Pass**: ctx.group_id 소속 nurse만 응답
- **Fail**: group_id 누락 → 다른 그룹 데이터 노출 (🔴 PHI 유출)

#### E2 — 개인 원티드 조회 (현재 연도 기본)
- **Query**: "김민지 5월 원티드 신청 내용"
- **기대 동작**: 연도 미지정 → SessionContext의 **현재 연도** 자동 적용. nurse_name → nurse_id 해석 후 wanted 조회.
- **Pass**: 응답에 "{current_year}년 5월" 명시

#### E3 — 시프트별 신청자
- **Query**: "5월에 N 원티드 신청한 사람"
- **기대 동작**: `query_schedule(scope=wanted_submissions, shift_name='나이트', month=5)` → 신청자 + 날짜 리스트

#### E4 — 마감 후 변경 내역 조회 허용
- **Query**: "이번 달 원티드 변경 내역 보여줘"
- **기대 동작**: `query_schedule(scope=wanted_adjustment, year=2026, month=5)` → 마감 여부와 무관하게 FixedWantedEntry source_type별 변경 내역 응답
- **검증 포인트**: 마감된 월에도 정상 응답
- **Fail**: "마감되어 조회 불가" 응답

#### E5 — 특정 일자 신청자 (group_id 필수)
- **Query**: "5월 15일 원티드 신청자 누구야?"
- **기대 동작**: `query_schedule(scope=wanted_submissions, date='2026-05-15', group_id={ctx.group_id})`
- **Pass**: ctx.group_id 소속만
- **Fail**: group_id 누락

---

### F. 팀 조정 (team)

#### F1 — 팀 이동 + preceptor 검증
- **Query**: "박혜미 1팀으로 이동"
- **기대 동작**: `update_person_attr(team_id=1, preview_only=True)` → preceptor 팀 일치 검증 → 충돌 시 추가 confirm → apply

#### F2 — 1팀 명단 조회
- **Query**: "1팀 간호사 목록 알려줘"
- **기대 동작**: `query_schedule(scope=nurse_info, team_id=1)` → 이름/grade/역할 목록
- **비고**: O-8 — UI 표현 (텍스트 vs 카드) 디자인 OPEN

#### F3 — 팀별 야간 분포
- **Query**: "팀별 야간 배분 분석"
- **기대 동작**: `analyze_report(group_by='team', shift='나이트')` → 분포 + variance

#### F4 — 팀 생성 🔴 gap
- **Query**: "3팀 새로 만들어줘"
- **기대 동작**: 명확한 미지원 안내 + 수동 등록 가이드

---

### G. 그레이드 (grade)

#### G1 — 개인 grade 변경
- **Query**: "김지은 그레이드 3으로"
- **기대 동작**: `update_person_attr(grade=3, preview_only=True)` → confirm → apply

#### G2 — grade 분포
- **Query**: "그레이드별 분포 보여줘"
- **기대 동작**: `analyze_report(group_by='grade')` → grade별 인원 카운트

#### G3 — 최소 경력자 정책 (산술 검증)
- **Query**: "최소 경력자 시프트당 2명 이상으로"
- **기대 동작**:
  1. 산술 검증 skill 호출 — 현 nurse pool로 시프트당 grade≥2 nurse 2명 배치 feasibility 확인
  2. **불가능 시**: "현재 grade≥2 인원이 부족해 적용 시 infeasibility 발생합니다. 적용을 중단합니다." → update 호출 안 함
  3. 가능 시: preview → confirm → apply
- **검증 포인트**: 산술 fail 시 update_constraint 호출 안 함
- **비고**: O-5 — 산술 skill 위치 (서비스/라우터) 결정 필요

---

### H. 개인별 월 한도 (monthly_limit)

#### 공통 사전조건 — 시프트 불가 nurse 검증
> H1~H4 모두: 요청된 시프트가 해당 nurse의 **가능 시프트가 아닌 경우**, 코멘트만 남기고 **저장하지 않음**.
> 예: "야간 불가" nurse에게 `n_exact=4` 요청 → "○○ 간호사는 야간 시프트가 불가능합니다. 한도를 저장하지 않습니다." 응답.

#### H1 — N exact 설정
- **Query**: "김민지 5월 N 4번으로 맞춰줘"
- **기대 동작**:
  1. 김민지의 가능 시프트에 N 포함 여부 확인
  2. 가능 → `update_monthly_limit(n_exact=4, preview_only=True)` → confirm → apply
  3. 불가 → 코멘트 응답, 저장 X

#### H2 — D min
- **Query**: "박혜미 5월 D 최소 8회"
- **기대 동작**: 동일 사전조건 + `update_monthly_limit(d_min=8)`

#### H3 — N max
- **Query**: "이영희 5월 N 최대 5회로 제한"
- **기대 동작**: 동일 사전조건 + `update_monthly_limit(n_max=5)`

#### H4 — 한도 조회
- **Query**: "김민지 5월 N 몇 번 설정?"
- **기대 동작**: `query_schedule(scope=monthly_limit, ...)` → 설정값 또는 "미설정" 응답

---

### I. 복합 질의 (compound)

#### I1 — 원티드 취소 + 대체 배치 (없을 경우 분기)
- **Query**: "박혜미 5월 10일 원티드 취소하고 김민지를 대신 배치"
- **기대 동작**:
  - **케이스 A (박혜미 5/10 원티드 존재)**:
    1. cancel preview → confirm → apply
    2. 김민지 add_shift preview → confirm → apply
  - **케이스 B (박혜미 5/10 원티드 없음)**:
    1. **"박혜미 간호사의 5/10 원티드 신청이 없습니다. 김민지 간호사를 5/10에 배치하겠습니다."** 명시
    2. 김민지 add_shift preview → confirm → apply
- **Pass**: 두 분기 모두 정상 처리 + 케이스 B에서 사실 언급
- **Fail**: 케이스 B에서 침묵하거나 cancel 시도 후 실패 응답

#### I2 — 분포 진단 + 정책 조정
- **Query**: "5월 야간 분포 보고 균형 안 맞으면 max_nig 조정해줘"
- **기대 동작**:
  1. analyze_report 응답 + variance 평가
  2. 사용자 판단 후 update_constraint (B1 정책으로 거절될 수 있음)
- **비고**: 유사 패턴 (다른 분포 + 다른 정책) 일반화 적용

#### I3 — 한도 변경 + 재생성
- **Query**: "김민지 5월 N 4번으로 맞추고 근무표 다시 돌려"
- **기대 동작**: H1 흐름 + generate_schedule preview → confirm → 잡 등록

#### I4 — 미제출자 처리 (개인 연장 X)
- **Query**: "5월 미제출자 확인하고 그 사람들 마감일 5월 25일까지 연장"
- **기대 동작**:
  1. 미제출자 명단 조회
  2. **clarify**: "개인별 마감일 연장은 지원하지 않습니다. 병동 전체 마감일을 5/25까지 연장할까요?"
  3. yes → `bulk_mutation(update_deadline=2026-05-25)` → confirm → apply
  4. no → 다음 행동 확인
- **Pass**: 개인 연장 거절 + 전체 연장 clarify 흐름
- **Fail**: 개인 단위 연장 시도

#### I6 — 다중 팀 이동 (이미 옮겨진 경우 처리)
- **Query**: "박혜미 1팀으로 옮기고 김민지를 2팀으로"
- **기대 동작**:
  - 박혜미 team_id 이미 1인 경우에도 **그 사실을 언급하고 의도된 상태 유지 진행**
  - 김민지 team 이동 진행
  - preceptor consistency check
- **Pass**: 이미 동일 상태인 nurse에 대해 사실 언급 포함

#### I7 — 위반 진단 + 자동 수정 (불가 시 처리)
- **Query**: "5월 근무표 위반사항 보여주고 자동으로 고쳐줘"
- **기대 동작**:
  1. validate_schedule → 위반 진단
  2. repair_schedule → 수정 제안
  3. **수정 불가능 시**: "자동으로 수정 가능한 방법을 찾지 못했습니다. 다음 옵션을 권장합니다: (a) 제약 완화 …, (b) 인원 보강 …, (c) 수동 조정 …"
- **비고**: O-7 — 수정 불가 시 대안 가이드 구체화 OPEN

#### I8 — 분포 분석 + 개인 한도 조정
- **Query**: "5월 야간 분포 분석하고 인원 부족하면 박혜미 5월 N 한도 늘려"
- **기대 동작**: analyze_report → 사용자 결정 → update_monthly_limit(n_max=X) preview → confirm → apply (H 공통 사전조건 적용)

---

### K. 권한 거절 (user_role='nurse')

#### K1 — nurse가 정책 변경
- **Query**: "야간 최대 7회로 바꿔줘"
- **기대 동작**: permission_denied. "병동 전체 설정 변경은 수간호사(HN) 또는 관리자(ADM) 권한이 필요합니다."
- **비고**: K1은 B1과 별개로, role 거절이 정책 거절보다 먼저 발동

#### K2 — nurse가 타인 grade 변경
- **Query**: "박혜미 그레이드 3으로"
- **기대 동작**: permission_denied. "다른 간호사의 데이터를 수정할 권한이 없습니다."

#### K3 — nurse가 본인 wanted_max 변경 ❌
- **Query**: "나 원티드 최대 5건으로"
- **기대 동작**: **거절**. "원티드 한도 조정은 수간호사 권한입니다." (본인이어도 불가)
- **Pass**: update_person_attr 호출 안 함
- **Fail**: 본인이라고 허용

#### K4 — nurse가 마감일 변경
- **Query**: "5월 원티드 마감일 5월 25일로"
- **기대 동작**: permission_denied. ward-wide action.

---

### L. Edge case

#### L1 — 없는 간호사 조회
- **Query**: "없는간호사 5월 원티드 보여줘"
- **기대 동작**: grounding 실패 → clarify. "해당 이름의 간호사를 찾을 수 없습니다. 정확한 이름을 알려주세요."

#### L2 — invalid month
- **Query**: "13월 근무표 생성해줘"
- **기대 동작**: skill 호출 전 거절. "13월은 유효하지 않습니다. 1~12 중 선택해주세요."

#### L3 — 자기 자신 preceptor
- **Query**: "김민지 프리셉터를 김민지 본인으로 설정"
- **기대 동작**: preceptor consistency check → self-reference 감지. "본인을 본인의 프리셉터로 지정할 수 없습니다."

#### L4 — 근무표 없는 월 위반 조회
- **Query**: "이번 달 위반사항 알려줘"
- **기대 동작**: validate_schedule 호출 결과:
  - 근무표 있고 위반 없음 → "위반사항이 없습니다."
  - 근무표 있고 위반 있음 → 종류별 violation 리스트
  - **근무표 없음 → "이번 달 근무표가 아직 생성되지 않았습니다." (자연어, error 톤 X)**
- **Pass**: error 응답 노출 없이 "없습니다" 자연어
- **Fail**: "No schedule found" 등 raw error 노출

---

## 2. 회귀 방지 핵심 시나리오 (🔴 Critical)

다음 항목은 회귀 시 운영/규제 사고 가능. CI 우선 보호.

| ID | 시나리오 | 사고 유형 |
|---|---|---|
| **B1** | max_nig 변경 거절 | 운영 정책 위반 |
| **E1, E5** | group_id 필터 누락 | PHI 유출 (개인정보보호법) |
| **D5** | 삭제 재확인 누락 | 데이터 손실 |
| **K1~K4** | 권한 분리 실패 | 권한 우회 |
| **L4** | raw error 노출 | UX 저하 + 내부 구조 노출 |

---

## 3. 자동화 변환 가이드 (pytest 시)

각 시나리오를 다음 구조로 변환:

```python
# tests/agent/test_qa_scenarios.py 예시
@pytest.mark.parametrize("scenario_id,query,expected", QA_SCENARIOS)
def test_agent_scenario(scenario_id, query, expected, deterministic_llm):
    result = run_agent(query, ctx=fixture_session_context())
    assert_skill_called(result, expected.skill)
    assert_response_contains(result.response, expected.must_contain)
    assert_no_skill_called(result, expected.must_not_call)
```

검증 도우미:
- `assert_skill_called(scope, action, **args)` — 호출/논호출/인자
- `assert_response_contains(text, patterns)` — 응답 자연어 일치
- `assert_group_id_filter(query)` — E1/E5 필수
- `assert_confirmation_required(result)` — D5, mutation 류

---

## 4. 후속 작업

1. **OPEN ISSUES (§0.4) 결론 도출** — 8건 모두 product/eng 합의 필요
2. **B1 정책 단호화 테스트** — 회귀 즉시 fail
3. **group_id 필터 통합 회귀 스위트** — wanted 관련 모든 read scope에 적용
4. **deterministic LLM double** — 시나리오별 fixture 작성
5. **시나리오 → pytest 변환**

---

> 본 문서는 코멘트가 달린 시점(2026-05-18) 기준이며, OPEN 이슈 결론에 따라 §0.2/0.4를 업데이트한다.
