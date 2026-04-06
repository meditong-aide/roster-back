# 목적 중심 API 명세

> API를 기술적 endpoint 나열이 아니라, **"어떤 운영 흐름에서 호출되는가"** 기준으로 재정리한다.  
> 권한: `Public` = 미인증, `Auth` = 인증 필수, `HN` = 수간호사, `ADM` = 마스터 관리자

---

## A. 초기 설정 흐름 (병원 최초 또는 신규 병동)

> **호출 순서**: A1 → A2 → A3 → A4 → A5 → A6 → A7

| # | API명 | Endpoint | Method | 설명 | 권한 |
|---|-------|----------|--------|------|------|
| A1 | 그룹 생성 | `/groups` | POST | 새 병동 생성. `{group_name}` | ADM |
| A2 | 관리자 지정 | `/groups/hn-admin` | PUT | 해당 그룹의 HN 배정. `{nurse_id, group_ids[]}` | ADM |
| A3 | 간호사 등록 (엑셀) | `/nurses/upload2-validate` | POST | 엑셀 검증 (1단계) | HN |
|    | 간호사 등록 확정 | `/nurses/upload2-confirm` | POST | 검증 결과 확정 저장 (2단계) | HN |
|    | 간호사 등록 (단건) | `/nurses/add-to-group` | POST | 기존 멤버를 그룹에 추가 | HN |
| A4 | 근무코드 설정 | `/shifts/add` | POST | D/E/N/O/M 등 코드 추가 | HN |
|    | 코드 가져오기 | `/shifts/import-to-group` | POST | 다른 그룹의 코드 복사 | HN |
| A5 | 인력배치 설정 | `/shift-manage/save` | POST | 직종별 슬롯당 인원 저장 | HN |
| A6 | 법규/내규 설정 | `/roster/config/save` | POST | RosterConfig 최초 저장 | HN |
| A7 | 팀 구성 | `/teams` | PUT | 팀 생성 + 멤버 배정 | Auth |

**선행 조건**: Office 등록 완료 (외부 시스템)  
**주의사항**: A2 후 해당 간호사는 재로그인 필요 (JWT 갱신)

---

## B. 월간 근무표 생성 사이클

> **호출 순서**: B1 → B2 → B3 → B4 → B5 → B6 → B7 → B8

### B1. 인력/설정 확인 및 조정

| # | API명 | Endpoint | Method | 설명 | 호출 시점 |
|---|-------|----------|--------|------|-----------|
| B1-1 | 인력배치 조회 | `/shift-manage/{class}` | GET | 현재 인력배치 확인 | 매월 초 |
| B1-2 | 일별 인력 초기화 | `/daily-shift` | GET | 월 데이터 조회 (없으면 자동 초기화) | 매월 초 |
| B1-3 | 일별 인력 조정 | `/daily-shift/daily` | PUT | 공휴일 등 날짜별 오버라이드 | 필요 시 |
| B1-4 | 월간 일괄 조정 | `/daily-shift/monthly` | PUT | 전체 일수에 동일값 적용 | 필요 시 |
| B1-5 | 공휴일 조회 | `/dates/holidays` | GET | 해당 월 공휴일 데이터 | 캘린더 렌더링 시 |
| B1-6 | 설정 버전 조회 | `/roster/config/versions` | GET | 기존 설정 버전 목록 | 설정 변경 검토 시 |

### B2. Wanted 오픈

| # | API명 | Endpoint | Method | 설명 | 호출 시점 |
|---|-------|----------|--------|------|-----------|
| B2-1 | Wanted 요청 | `/wanted/request` | POST | 선호 수집 기간 오픈 + 마감일 설정 | 생성 2~3주 전 |
| B2-2 | Wanted 설정 저장 | `/wanted/config` | POST | 일별 오프 제한 등 설정 | 오픈 전 |
| B2-3 | Wanted 상태 조회 | `/wanted/status` | GET | 현재 open/closed/expired | 수시 |

**Request Body (B2-1)**: `{year, month, exp_date}`  
**Response**: `{status: "requested", exp_date, message}`

### B3. 간호사 선호 제출

| # | API명 | Endpoint | Method | 설명 | 호출 시점 |
|---|-------|----------|--------|------|-----------|
| B3-1 | 임시 저장 | `/preferences` | POST | 선호 draft 저장 (여러 번 가능) | 수시 |
| B3-2 | 최종 제출 | `/preferences/submit` | POST | 선호 확정 제출 | 마감 전 |
| B3-3 | 빈 선호 제출 | `/preferences/submit/empty` | POST | 선호 없이 제출 (참여 의사 표시) | 선호 없을 때 |
| B3-4 | 제출 철회 | `/preferences/retract` | POST | 제출 취소 후 수정 가능 | 마감 전 |
| B3-5 | 내 선호 조회 | `/preferences/latest` | GET | 최근 제출/draft 조회 | 수시 |

**Request Body (B3-2)**: `PreferenceData {year, month, data: {shifts, pairs, ...}}`  
**선행 조건**: Wanted가 open 상태  
**주의사항**: 제출된 shift_id가 그룹의 shifts에 존재하는지 서버에서 검증

### B4. 마감 및 조정

| # | API명 | Endpoint | Method | 설명 | 호출 시점 |
|---|-------|----------|--------|------|-----------|
| B4-1 | 제출 현황 | `/wanted/{year}/{month}/submissions` | GET | 간호사별 제출 여부 | 마감 전후 |
| B4-2 | 마감일 변경 | `/wanted/deadline` | PATCH | 연장 또는 단축 | 필요 시 |
| B4-3 | 조기 마감 | `/wanted/close` | PATCH | 즉시 마감 | 수집 충분 시 |
| B4-4 | 전체 선호 조회 | `/preferences/all` | GET | 그룹 전원의 선호 확인 | 마감 후 |
| B4-5 | 초과 요청 확인 | `/wanted/over-limit-nurses` | GET | WantedConfig 제한 초과 간호사 | 마감 후 |
| B4-6 | 초과분 삭제 | `/wanted/delete-excess-off/{nurse_id}` | POST | 초과 오프 요청 제거 | 필요 시 |
| B4-7 | 조정판 조회 | `/wanted/adjustment/{year}/{month}` | GET | 확정/수정 대상 목록 | 마감 후 |
| B4-8 | 조정 저장 | `/wanted/adjustment` | POST | 확정 항목 저장 (FixedWantedEntry) | 조정 후 |
| B4-9 | 항목 토글 | `/wanted/adjustment/entry/{id}/toggle` | PATCH | 개별 항목 활성/비활성 | 수시 |
| B4-10 | 조정 초기화 | `/wanted/adjustment/{year}/{month}/reset` | POST | 전체 리셋 | 처음부터 재조정 시 |

### B5. 근무표 생성

| # | API명 | Endpoint | Method | 설명 | 호출 시점 |
|---|-------|----------|--------|------|-----------|
| B5-1 | 동기 생성 | `/roster_create/generate` | POST | 즉시 생성 (응답 대기) | 소규모 그룹 |
| B5-2 | 비동기 생성 | `/roster_create/async` | POST | SQS → 워커 (대규모) | 대규모 그룹 |
| B5-3 | 작업 상태 확인 | `/jobs/status/latest` | GET | 비동기 작업 진행률 | 비동기 생성 후 폴링 |

**Request Body (B5-1/B5-2)**: 
```json
{
  "year": 2026, "month": 5,
  "config_id": 42,
  "distribution_mode": "hybrid",
  "grade_strategy": "BASE",
  "oversupply_balance_gauge": 6,
  "monthly_preference_gauge": 3,
  "use_fixed_wanted": true,
  "not_one_night": false
}
```

**Response (B5-1)**: 생성된 Schedule + ScheduleEntry 전체  
**Response (B5-2)**: `{job_id, status: "QUEUED"}`

### B6. 검토 및 편집

| # | API명 | Endpoint | Method | 설명 | 호출 시점 |
|---|-------|----------|--------|------|-----------|
| B6-1 | 근무표 조회 | `/roster/{year}/{month}` | GET | 월별 전체 근무표 | 생성 후 |
| B6-2 | 버전 목록 | `/roster/{year}/{month}/versions` | GET | 여러 번 생성 시 버전 비교 | 생성 후 |
| B6-3 | 이전 달 이월 | `/roster/{year}/{month}/prev-tail` | GET | 전월 마지막 N일 (연속 제약 검증) | 생성/편집 시 |
| B6-4 | 고정셀 재생성 | `/roster_create/hold_generate` | POST | 특정 셀 고정 후 나머지 재생성 | 편집 후 |
| B6-5 | 근무표 복사 | `/roster/copy/{source_id}` | POST | 기존 근무표 복사 후 수정 | 대안 비교 시 |
| B6-6 | 빈 근무표 생성 | `/roster/create-empty` | POST | 완전 수동 작성용 | 드문 케이스 |
| B6-7 | 이름 변경 | `/roster/{schedule_id}/name` | PATCH | 근무표 이름 지정 | 식별 편의 |
| B6-8 | 삭제 | `/roster/{schedule_id}` | DELETE | 불필요 버전 삭제 | 정리 시 |
| B6-9 | 대체 추천 | `/roster/replacement/recommend` | POST | 결근 시 대체 인력 AI 추천 | 긴급 교체 시 |

### B7. 발행

| # | API명 | Endpoint | Method | 설명 | 호출 시점 |
|---|-------|----------|--------|------|-----------|
| B7-1 | 발행 | `/roster/publish` | POST | 근무표 공식 공개. Snapshot 생성 | 확정 후 |
| B7-2 | 발행 취소 | `/roster/unpublish` | POST | 발행 철회 (is_active=False) | 오류 발견 시 |
| B7-3 | 발행 목록 | `/roster/issued` | GET | 발행 이력 조회 | 이력 확인 |

**Request Body (B7-1)**: `{schedule_id, issue_comment}`

### B8. 공유 및 분석

| # | API명 | Endpoint | Method | 설명 | 호출 시점 |
|---|-------|----------|--------|------|-----------|
| B8-1 | 공유 생성 | `/roster/shares/schedules/{id}/auto` | POST | 자동 이미지 공유 링크 | 발행 후 |
| B8-2 | 공유 삭제 | `/roster/shares/{token}` | DELETE | 공유 폐기 | 필요 시 |
| B8-3 | 만족도 요약 | `/dashboard/summary` | GET | 그룹 전체 만족도 | 발행 후 |
| B8-4 | 개인 만족도 | `/dashboard/individual` | GET | 간호사별 상세 | 리뷰 시 |
| B8-5 | 월별 추세 | `/dashboard/trends` | GET | 최대 12개월 만족도 추이 | 분기 리뷰 |
| B8-6 | 엑셀 내보내기 | `/roster/schedule/{id}/export` | GET | 근무표 Excel 다운로드 | 인쇄/공유 |

---

## C. 일상 운영 API

### C1. 인증

| API명 | Endpoint | Method | 설명 | 호출 시점 |
|-------|----------|--------|------|-----------|
| 로그인 | `/auth/login` | POST | 쿠키 JWT 발급 | 앱 진입 |
| SSO 토큰 발급 | `/token/` | POST | clientId/Secret으로 토큰 | SSO 연동 |
| SSO 로그인 | `/token/login` | POST | 토큰 + MemberID로 인증 | SSO 연동 |
| 내 정보 | `/auth/me` | GET | 현재 사용자 정보 | 페이지 로드 |
| 그룹 전환 | `/auth/switch-group` | POST | HN이 다른 그룹으로 전환 | 다중 그룹 관리 시 |
| 로그아웃 | `/auth/logout` | POST | 쿠키 삭제 | 종료 |
| ID 찾기 | `/auth/find_id` | POST | 이름+생년/전화/이메일로 ID 조회 | 계정 분실 |
| PW 찾기 | `/auth/find_pw` | POST | 임시 비밀번호 발급 (SMS/Email) | 비밀번호 분실 |

### C2. 개인 근무표 확인

| API명 | Endpoint | Method | 설명 |
|-------|----------|--------|------|
| 월별 조회 | `/roster/{year}/{month}` | GET | 내 근무 + 전체 |
| 최신 조회 | `/roster/latest` | GET | 가장 최근 근무표 |
| 발행 상세 | `/roster/issued_roster` | GET | 발행된 근무표 상세 |
| 스케줄 상태 | `/roster/status` | GET | 상태 + 내 선호 제출 여부 |
| 내 주휴 | `/weekly-off/my` | GET | 내 주휴 날짜/요일 |

### C3. 메시지/알림

| API명 | Endpoint | Method | 설명 |
|-------|----------|--------|------|
| 메시지 발송 | `/message/write` | POST | 1:N 메시지 |
| 메시지 목록 | `/message/list` | GET | 수신/발신 |
| 메시지 읽음 | `/message/view/{id}` | GET | 읽음 처리 |
| 알림 목록 | `/push/list` | GET | 푸시 목록 |
| 알림 개수 | `/push/listcnt` | GET | 미읽음 수 |
| 전체 읽음 | `/push/read/all` | PATCH | 전체 읽음 처리 |
| 알림 설정 | `/push/setting` | GET/PATCH | 수신 ON/OFF |

### C4. 마이페이지

| API명 | Endpoint | Method | 설명 |
|-------|----------|--------|------|
| 내 정보 조회 | `/nurses/personnel-basic-info` | GET | 개인정보 |
| 내 정보 수정 | `/nurses/personnel-basic-info` | PATCH | 이메일/경력 수정 |
| 비밀번호 변경 | `/nurses/change-password` | PUT | 현재 PW → 새 PW |
| 전화번호 인증요청 | `/nurses/change-phone/send-code` | POST | SMS 인증코드 발송 |
| 전화번호 변경 | `/nurses/change-phone/verify` | PUT | 코드 검증 후 변경 |
| 프로필 이미지 업로드 | `/nurses/profile-image` | POST | 이미지 업로드 |
| 프로필 이미지 조회 | `/nurses/profile-image` | GET | 이미지 URL |
| 프로필 이미지 삭제 | `/nurses/profile-image` | DELETE | 이미지 제거 |

### C5. 시스템/운영

| API명 | Endpoint | Method | 설명 |
|-------|----------|--------|------|
| 헬스체크 | `/health/comprehensive` | GET | 전체 시스템 상태 |
| ALB 체크 | `/health/alb` | GET | 로드밸런서용 |
| K8s Readiness | `/health/ready` | GET | K8s 프로브 |
| K8s Liveness | `/health/live` | GET | K8s 프로브 |
| 공지 목록 | `/contact/notice/list` | GET | 공지사항 |
| 문의 등록 | `/contact/write` | POST | 고객 문의 |

---

## D. 프론트엔드 → 백엔드 매핑 요약

| 프론트 페이지 | 주요 API 그룹 | 비고 |
|--------------|--------------|------|
| `Nav.tsx` (그룹 관리) | A1~A2 | ADM 전용 |
| `ConfTab0` (기본 설정) | A5, A6, B1-1~B1-4 | HN 전용 |
| `ConfTab1` (근무코드) | A4 | HN 전용 |
| `ConfTab3` (주휴) | 주휴 API 5개 | HN 전용 |
| `ConfTab4` (원티드 설정) | B2-2 | HN 전용 |
| `Head_nurse_management.tsx` | A3, 간호사/팀/그레이드 API | HN 전용 |
| `Roster_wanted.tsx` | B2~B4 | HN + 간호사 |
| `Roster_create.tsx` | B5~B6 | HN 전용 |
| `Roster_view.tsx` | C2 | 전체 |
| `myPage.tsx` | C4 | 전체 (ADM 제외) |
| `Account_management_mworks.tsx` | 엑셀 업로드 API 6개 | ADM 전용 |
| `support/` | C5 (공지/문의) | 전체 |

---

## E. 에러 코드 패턴

| HTTP Status | 상황 | 메시지 패턴 |
|-------------|------|-------------|
| 400 | 잘못된 입력 | "year/month 필수", "shift_id 중복" 등 |
| 401 | 인증 실패 | "Invalid token", "Token expired" |
| 403 | 권한 부족 | "권한 없음", "마스터 관리자만 접근 가능", "수간호사 권한 필요" |
| 404 | 리소스 없음 | "Schedule not found", "Nurse not found" |
| 409 | 충돌 | "이미 마감됨", "이미 발행됨" |
| 500 | 서버 오류 | "Internal Server Error" |
