# roster 사용자 액션 로깅 카탈로그 (GTM식)

> 최초 생성 = 사전 수집 인벤토리(BE 변경 엔드포인트 111개 + FE CTA 151개) 기반 자동 생성.
> ★ 생성기 `scratchpad/build_catalog.py` 는 **리포에 없다**(scratchpad 는 커밋 대상이 아니었다).
> 따라서 지금은 **수기 갱신 문서**이며, 덮어쓰기 걱정 없이 편집한다.
> AWS 실측값은 조회 일자를 함께 적을 것 — 인프라가 문서보다 앞서가 있던 전례가 있다(3절).

## 1. 개요

- **목적**: 사용자가 실제로 무엇을 눌렀고 무엇이 바뀌었는지를 **사용성 분석(GTM식 제품 분석)** 관점에서 적재한다. 규정 준수용 **감사 로그가 아니다**(감사·보안 추적은 별도 관심사).
- **이원 로깅**: 동일한 사용자 행동을 두 계층에서 각각 포착한다.
  - **BE(call_history)**: 변경 요청(POST/PUT/PATCH/DELETE)이 서버에 도달한 사실 + 라벨(page/section/action) + 화이트리스트 값(changes/summary). 서버 진실.
  - **FE(ui_events)**: 버튼/CTA 클릭 등 화면 상호작용. 서버 호출로 이어지지 않는 액션(토글·네비게이션·로컬 편집)까지 포함해 **의도와 이탈**을 본다.
- 두 스트림은 공용 이벤트 파라미터 스키마(2절)로 정렬되어, `office_id`/`group_id`(테넌트) 기준으로 조인·집계된다.

## 2. 공용 이벤트 파라미터 스키마 (FE·BE 공통)

| 파라미터 | 의미 | 비고 |
|---|---|---|
| `office_id` | 오피스(병원) 테넌트 | **파티션키** |
| `group_id` | 병동(그룹) 테넌트 | **파티션키** |
| `actor.account_id` | 계정 ID | 행위자 |
| `actor.nurse_id` | 간호사 ID | 행위자 |
| `actor.name` | 이름 | 행위자 |
| `actor.role` | 역할(HN/ADM/간호사 등) | 행위자 |
| `page` | 화면(route 정본) | 라벨 |
| `section` | 화면 내 세부 영역 | 라벨 |
| `action` | 수행 액션 | 라벨 |
| `target` | 대상 식별자(nurse_id·schedule_id 등) | 라벨(JSON) |
| `params` / `changes` | 입력값/변경값 | FE=params, BE=changes(화이트리스트) |
| `path` `method` `status` `dur_ms` | HTTP 메타 | **BE 전용** |
| `ip` `ua` `req_id` `ts` | 요청 메타/타임스탬프 | 공통 |

### 페이지(route) 정본

| route | page |
|---|---|
| `/` | 홈 |
| `/roster_dashboard` | 대시보드 |
| `/roster_wanted` | 원티드 |
| `/roster_view` | 근무표보기 |
| `/head_nurse_management` | 근무자관리 |
| `/roster_create` | 근무표만들기 |
| `/roster_configure` | 설정 |
| `/myPage` | 마이페이지 |
| `/Account_management_mworks` | 계정관리 |
| `/support` | 고객센터 |

## 3. 파티션 구조 — `dt` / `office_id` / `group_id`

목표 파티션 레이아웃은 **3키**(`dt` → `office_id` → `group_id`)로, 날짜 스캔과 테넌트 스코프를 동시에 좁힌다.

### 현재 상태(2026-08-20 AWS 실조회)

- **Firehose 스트림 4개**: `roster-call-history`, `roster-call-history-dev`, `roster-ui-events`, `roster-ui-events-dev`.
  - 공용 버킷 `roster-call-history-702166530338`, 역할 `arn:aws:iam::702166530338:role/firehose-roster-call-history-role`, 버퍼 **64MB/60s**.
  - **동적 파티셔닝 = 켜짐(4개 전부)**. prefix 는 3키:
    `call_history/dt=!{timestamp:yyyy-MM-dd}/office_id=!{partitionKeyFromQuery:office_id}/group_id=!{partitionKeyFromQuery:group_id}/`
  - 테넌트 값은 정상 적재된다(`office_id=102243`·`102560`·`101358` 등). `none` 은 로그인 등 **인증 전** 요청.
- **Glue 테이블 8개**(`roster_analytics` DB): base 4 (`call_history`, `call_history_dev`, `ui_events`, `ui_events_dev`)
  + 뷰 4 (`v_` 접두).
  - `call_history` 컬럼(**21**): `ts, method, path, query, status, dur_ms, account_id, nurse_id, name, role, ip, ua, req_id,
    page, section, action, summary, office_id, group_id, target, changes`.
  - 파티션키 = `dt`(string) 만. Serde=OpenX JsonSerDe. projection `dt`=date, range `2026-07-01,NOW`, interval 1 DAY.
  - `ui_events` 컬럼(12): `event, page, section, session_id, account_id, nurse_id, props, ip, ua, ts, office_id, group_id`.
  - **뷰는 `SELECT *` 가 아니라 명시적 컬럼 목록**이다 → base 에 컬럼을 더해도 **뷰를 재정의해야** 노출된다.

### `target` / `changes` 추가 (2026-08-20 적용 완료)

미들웨어는 처음부터 두 필드를 JSON 에 실어 보냈으나 **Glue 에 컬럼이 없어 OpenX JsonSerDe 가 조용히 버렸다**
— `summary` 문자열만 보이고 필드 단위·대상 식별자로는 조회가 안 되던 원인. base 2 + 뷰 2 를 갱신했다.

```sql
ALTER TABLE roster_analytics.call_history_dev ADD COLUMNS (target string, changes string);
ALTER TABLE roster_analytics.call_history     ADD COLUMNS (target string, changes string);
CREATE OR REPLACE VIEW roster_analytics.v_call_history_dev AS SELECT ..., summary, target, changes, ... ;
CREATE OR REPLACE VIEW roster_analytics.v_call_history     AS SELECT ..., summary, target, changes, ... ;
```

- ★ 타입은 **`string`** 이다. `changes[].value` 는 bool·int·string 이 섞이므로(`_short()` 가 스칼라를 원형 유지)
  `array<struct<...,value:string>>` 로 잡으면 SerDe 가 타입 불일치로 떨군다. 원본 JSON 문자열로 받아
  `json_parse` + `UNNEST` 로 푼다.
- S3 원본에 이미 들어 있던 값이라 **과거 데이터도 함께 조회된다**(재적재 불필요).

```sql
SELECT action,
       json_extract_scalar(c, '$.label') AS label,
       json_extract_scalar(c, '$.value') AS value
FROM roster_analytics.v_call_history
CROSS JOIN UNNEST(CAST(json_parse(changes) AS array(json))) AS t(c)
WHERE dt = '2026-08-20' AND changes IS NOT NULL AND changes <> '[]'
```

### 남은 갭 — Athena 쪽 3키 승격은 아직

Firehose 는 3키로 쓰는데 **Glue 파티션키는 `dt` 단일**이다. `storage.location.template` 이 `dt=${dt}/` 라
하위 디렉터리를 재귀 스캔해 **조회는 되지만 프루닝이 안 된다** — `WHERE office_id=...` 를 걸어도 그 날짜 전체를 읽는다.

- **Athena**: `office_id`/`group_id` 를 **파티션키로 승격**(데이터 컬럼에서 제거 — JSON 엔 남지만 serde 가 무시).
  projection `office_id`/`group_id`=**injected**(쿼리에 `WHERE` 필수 → 테넌트 스코프가 자연히 강제됨).
- 승격은 **DROP+CREATE** 가 필요하다(파티션키는 ALTER 불가). 뷰도 함께 재정의한다.
- **FE ui_events 는 `office_id`/`group_id` 를 반드시 전송해야 한다**(파티션키). 현재 autotrack 보강 필요.

## 4. 백엔드 — `call_history`

### 미들웨어 동작 (app/main.py)

- **`_call_action_logger`** (전역 미들웨어): 변경 요청(POST/PUT/PATCH/DELETE)을 **Firehose → S3 → Athena `call_history`** 로 기록. 카탈로그 매칭 시 로그에 `page/section/action/changes/summary` 를 부착한다.
- **`_CallBodyTapMiddleware`**: 카탈로그에 매칭되는 경로에 한해 요청 본문을 **non-consuming tee** 로 캡처(32KB 캡). 본문을 소비하지 않으므로 라우트는 원본 본문을 무손상 수신한다.
- **PII**: `fields` 화이트리스트에 있는 값만 기록. `masked` 필드는 값 대신 `'***'`.

### 라벨 카탈로그 (app/call_action_catalog.py)

- 경로 → (page/section/action) 라벨 + 요청본문 화이트리스트 카탈로그. **116개 엔드포인트** 라벨링(2026-08-20 기준).
- `match(method, path)` → `(entry, path_params)`; `enrich()` → `{page, section, action, target, changes, summary}`.
- **검증 완료**: 본문 무손상(60KB 도 라우트 전량 수신) · PII 마스킹 · 미등록 경로 무보강.

#### 미등록 잔여 7건 (의도적 제외)

`/openapi.json` 의 변경 라우트 122개와 대조한 결과다. 나머지는 전부 라벨링돼 있다.

- `POST /agent/test/chat` · `POST /agent/test/setup-db` — 테스트 화면 전용.
- `POST /events` — UI 이벤트 **수신부 자신**(로깅 대상이 아니라 로깅 경로).
- `POST /member/send-{sms,push,mlink,email-background}-message` — 파라미터가 함수 안에 하드코딩된 테스트 스텁.
  ★ 네 경로 모두 `current_user` 의존성이 **없다** — 인증 없이 호출하면 실제 발송이 나간다. 정리 대상.

#### PII 판단 기준

`fields`(값까지 기록) vs `masked`(`'***'`) 를 가르는 실제 기준은 **자유 텍스트냐**가 아니라 **누가 읽을 것을 전제로 쓴 글이냐**다.

- `masked` — 사람 간 소통 내용: `/message/write` 의 `message`(쪽지), `/api/agent/chat/send` 의 `message`(에이전트 요청문).
  에이전트 요청문은 대상 지목이 목적이라 **실명이 필연적으로 들어간다**("김민지 원티드 취소하고 이영희로 대체해줘").
- `fields` — 업무 메모·사유: `nurse_memo`, `reason`, `note`, `issue_comment`, `monthly_memo`, `title` 등.
- 건강 관련 플래그는 `masked`: `/nurse-period/leave-flags` 의 `pregnant`(바꿨다는 사실만 남고 값은 가려진다).

### 엔드포인트 카탈로그 표

> 아래는 최초 인벤토리 시점의 변경 엔드포인트 **111개**(3개 BE 인벤토리 병합, page별 그룹핑)이라 **표 자체는 낡았다**.
> 현재 `call_action_catalog.py` 는 **116개**를 라벨링하며, 남은 7건은 위 「미등록 잔여」 항목대로 의도적으로 제외돼
> 일반 기록(method+path)만 남는다. 정본은 코드이고 이 표는 참고용이다.
> action 뒤 태그: `[DEPRECATED]` 대체됨 · `[테스트]` 테스트 스텁 · `[크론/내부]` FE 미호출/시스템 · `[읽기성]` DB 미변경(부작용 없음).

#### 홈 (1)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 스티커 저장 | 근무표 스티커 저장(삭제 후 입력) | POST | `/sticker/insert` | stcker_date, sticker_contents | — |

#### 원티드 (13)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 원티드 작성 요청 | 원티드 작성기간 오픈(작성요청 생성) | POST | `/wanted/request` | year, month, exp_date | — |
| 원티드 마감 | 원티드 작성 마감 | PATCH | `/wanted/close` | — | — |
| 원티드 마감일 변경 | 원티드 마감일 변경 | PATCH | `/wanted/deadline` | year, month, exp_date | — |
| 원티드 AI 반영 | 자연어 원티드 요청 분석·반영 | POST | `/wanted/invoke` | request, schema, case, year, month | — |
| 원티드 만료 자동마감 | 만료 원티드 일괄 마감 [크론/내부] | POST | `/wanted/close-expired` | — | — |
| 일자별 원티드 제한 설정 | 원티드 제한 설정 저장(upsert) | POST | `/wanted/config` | year, month, max_requests, target_date, shift_type | — |
| 일자별 원티드 제한 설정 삭제 | 원티드 제한 설정 삭제 | DELETE | `/wanted/config` | — | — |
| 원티드 제한 설정 월 일괄삭제(OFF) | 해당 월 원티드 설정 전체 삭제 | DELETE | `/wanted/config/toggle` | — | — |
| 원티드 제한 검증 | 원티드 요청 제한 검증(비변경) [읽기성] | POST | `/wanted/validate-limits` | — | — |
| 초과 OFF 삭제 | 간호사 초과 OFF 요청 삭제 | POST | `/wanted/delete-excess-off/{nurse_id}` | — | — |
| 확정 원티드(조정판) 저장 | 확정 원티드 저장 | POST | `/wanted/adjustment` | year, month, entries[].nurse_id, entries[].shift_date, entries[].shift_id, entries[].is_applied, entries[].reason, entries[].head_nurse_memo | — |
| 확정 원티드 항목 적용토글 | 확정 원티드 항목 적용/미적용 토글 | PATCH | `/wanted/adjustment/entry/{entry_id}/toggle` | — | — |
| 확정 원티드 재설정 | 해당 월 확정 원티드 삭제·원본 복원 | POST | `/wanted/adjustment/{year}/{month}/reset` | — | — |

#### 희망근무 (4)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 선호 입력 | 희망근무 초안 임시저장 | POST | `/preferences` | year, month, data, preference | — |
| 선호 제출 | 희망근무 최종 제출 | POST | `/preferences/submit` | year, month, data, preference | — |
| 선호 제출 | 빈 희망근무 최종 제출 | POST | `/preferences/submit/empty` | year, month | — |
| 선호 제출 | 희망근무 제출 철회(수정 위해) | POST | `/preferences/retract` | year, month | — |

#### 근무표만들기 (10)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 생성 설정 저장 | 근무표 생성 설정(프리셋) 저장 | POST | `/roster/config/save` | config_id, config_name, config_memo, config_version, group_id, day_req, eve_req, nig_req, min_exp_per_shift, req_exp_nurses, max_nig_per_month, three_seq_nig, two_offs_after_three_nig, two_offs_after_two_nig, banned_day_after_eve, max_conseq_work, off_days, not_one_night, use_mid, preceptor_gauge, preceptee_on, weekly_off_group, off_placement_mode, fixed_wanted_use_yn, off_first, off_swap_enabled, team_balance_enable, team_balance_gauge, team_balance_mode, show_level, show_preceptor | — |
| 설정 프리셋 관리 | 저장 설정 프리셋 미노출(version=NULL 되돌림) | DELETE | `/roster/config/{config_id}` | group_id | — |
| 생성 실행 | 근무표 생성 요청(비동기·SQS) | POST | `/roster_create/async` | year, month, group_id, config_id, config, grade_strategy, preceptor_gauge, advanced_inference, suggest_fixes, distribution_mode, oversupply_balance_gauge, monthly_preference_gauge, monthly_shift_preferences, not_one_night, use_fixed_wanted | — |
| 생성 실행 | 근무표 생성(동기) | POST | `/roster_create/generate` | year, month, group_id, config_id, grade_strategy, preceptor_gauge, distribution_mode, not_one_night | — |
| 재생성(제약 완화) | infeasibility 해결 옵션 적용 후 재생성 | POST | `/roster_create/apply-resolution` | year, month, grade_strategy, apply, treatment_ids, option_id, persist | — |
| 생성 요청 | 수간호사 근무표 생성 요청 기록 | POST | `/roster/request` | year, month, group_id, config_id, grade_strategy | — |
| 생성 실행 | 고정 셀 반영 근무표 생성 | POST | `/roster_create/hold_generate` | year, month, fixed_cells, config_id, distribution_mode, oversupply_balance_gauge, monthly_preference_gauge, monthly_shift_preferences | — |
| 사전 검사 | 생성 전 infeasibility 프리체크(팀×직급×풀) [읽기성] | POST | `/groups/{group_id}/roster/precheck` | num_days, nurses, teams, roster_config, team_coverage, grade_constraints, stop_on_config_error | — |
| 일자별 근무인원 | 월 일자별 근무인원(D/E/N/M) 일괄 교체 저장 | PUT | `/daily-shift` | office_id, group_id, year, month, date, max_enabled, apply_globally, month_summary, apply_summary_to_days | — |
| 제약 조정 | 제약 조정 미리보기(dry-run config diff) [읽기성] | POST | `/constraint_impact/preview_adjustments` | year, month, constraint_adjustments | — |

#### 근무표보기 (15)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 버전 관리 | 근무표(버전) 삭제·숨김(dropped=1) | DELETE | `/roster/{schedule_id}` | group_id | — |
| 발행 | 근무표 발행(마감) | POST | `/roster/publish` | schedule_id, issue_comment, group_id | — |
| 발행 | 근무표 발행 취소(마감 철회, issued→draft) | POST | `/roster/unpublish` | schedule_id, group_id | — |
| 편집 저장 | 근무표 셀 수동편집 저장 | POST | `/roster/save` | year, month, schedule_id, roster, memo, group_id | — |
| 검증 | 근무표 제약 위반 검증(부작용 없음) [읽기성] | POST | `/roster/validate` | year, month, roster, schedule_id, group_id | — |
| 버전 관리 | 근무표 버전 이름 변경 | PATCH | `/roster/{schedule_id}/name` | name, group_id | — |
| 버전 관리 | 근무표 새 버전으로 복사 | POST | `/roster/copy/{source_schedule_id}` | new_name, group_id | — |
| 버전 관리 | 빈 근무표 신규 버전 생성 | POST | `/roster/create-empty` | year, month, name, group_id | — |
| 버전 관리 | 주휴만 포함한 빈 근무표 생성 | POST | `/roster/create-with-weekly-off` | year, month, name, group_id | — |
| 대체 추천 | 대체·교체 간호사 추천 요청 | POST | `/roster/replacement/recommend` | schedule_id, mode, target_nurse_id, slots, absence_window, top_k, options, group_id | — |
| 공유 | 근무표 공유 링크 생성(이미지 URL 지정) | POST | `/roster/shares/schedules/{schedule_id}` | image_url, title, description, expires_in_days, group_id | — |
| 공유 | 이미지 업로드 후 공유 링크 생성 | POST | `/roster/shares/schedules/{schedule_id}/upload` | image_file, title, description, expires_in_days, group_id | — |
| 공유 | 자동 이미지 생성 후 공유 링크 생성 | POST | `/roster/shares/schedules/{schedule_id}/auto` | title, description, expires_in_days, group_id | — |
| 공유 | 캡처(data URL) 이미지 공유 링크 생성 | POST | `/roster/shares/schedules/{schedule_id}/capture` | image_data_url, title, description, expires_in_days, group_id | — |
| 공유 | 근무표 공유 링크 해제 | DELETE | `/roster/shares/{share_token}` | — | — |

#### 근무자관리 (26)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 월근무한도(나이트개수) | 간호사 월 근무한도 일괄 저장 | PUT | `/nurses/monthly-limits` | year, month, limits[].nurse_id, limits[].group_id, limits[].d_min, limits[].d_max, limits[].d_exact, limits[].e_min, limits[].e_max, limits[].e_exact, limits[].n_min, limits[].n_max, limits[].n_exact, limits[].o_min, limits[].o_max, limits[].o_exact | — |
| 월근무한도(나이트개수)/일괄적용 | 나이트 개수 병동 일괄 적용 | POST | `/nurses/monthly-limits/night-bulk` | group_id, year, month, kind, value | — |
| 간호사 순서·활성상태 | 간호사 순서·활성 단건 변경 | POST | `/nurses/sequence/save` | nurse_id, new_sequence, active, group_id | — |
| 간호사 순서 일괄재정렬 | 간호사 순서 일괄 재정렬 | POST | `/nurses/sequence/reorder` | active_order, inactive_order, group_id | — |
| 간호사 정보 일괄편집(그리드) | 간호사 정보 일괄 수정 | POST | `/nurses/bulk-update` | nurse_id, grade, team_id, role, level_, experience, allowed_shifts, work_shifts, fixed_shift, is_weekend_off, weekly_off_weekday, personal_off_adjustment, sequence, active, is_head_nurse, nurse_memo, exclusion_partner_id, wanted_max_requests, enable_aide, enable_nurse_pair_preference, resignation_date, resignation_reason, assignment | phone_number, email, birth_date |
| 엑셀 업로드2/검증 | 간호사 엑셀 업로드 검증 [읽기성] | POST | `/nurses/upload2-validate` | group_id | — |
| 엑셀 업로드2/저장 | 간호사 엑셀 업로드 저장 | POST | `/nurses/upload2-confirm` | group_id | rows |
| 근무자 추가(멤버 등록) | 선택 멤버를 병동 근무자로 추가 | POST | `/nurses/add-to-group` | nurse_ids, group_id | — |
| 엑셀 업로드(레거시)/검증 | 엑셀 데이터 검증 [읽기성] | POST | `/nurses/validate-excel` | include_rows | data |
| 엑셀 업로드(레거시)/저장 | 검증된 엑셀 데이터 저장 | POST | `/nurses/confirm-upload` | include_rows, new_groups_to_create | data |
| 직접입력 통합등록(계정+근무자) | 신규 직원 계정+근무자 통합 등록 | POST | `/nurses/integrated-register` | group_id | members |
| 배정 등록(DEPRECATED) | 배정 등록(파견/이동/휴직/퇴사/프리셉티) [DEPRECATED] | POST | `/nurses/assignments` | nurse_id, reason, source_group_id, target_group_id, start_date, expected_end_date, target_team_id, target_grade, target_fixed_shift | — |
| 배정 영향 미리보기(dry-run) | 배정 전 영향 분석 [읽기성] | POST | `/nurses/assignments/preview` | nurse_id, reason, start_date, target_group_id, exclude_id | — |
| 배정 수정(DEPRECATED) | 배정 수정(기간/상태/사유) [DEPRECATED] | PUT | `/nurses/assignments/{assignment_id}` | start_date, expected_end_date, end_date, status, reason, target_group_id, target_team_id, target_grade | — |
| 배정 취소(DEPRECATED) | 배정 취소 [DEPRECATED] | DELETE | `/nurses/assignments/{assignment_id}` | — | — |
| 간호사 상세/사이드프로필 | 간호사 정보 수정 | PATCH | `/nurses/{nurse_id}` | name, experience, role, level_, grade, team_id, gender, joining_date, resignation_date, resignation_reason, resignation_reason_memo, nurse_memo, is_head_nurse, preceptor_id, exclusion_partner_id, fixed_shift, weekly_off_enabled, weekly_off_weekday, weekly_off_type, is_weekend_off, allowed_shifts, work_shifts, enable_nurse_pair_preference, enable_aide, wanted_max_requests, assignment, assignments, preceptor_periods, preceptee_period | phone_number, email, birth_date |
| 간호사 삭제(휴지통) | 간호사 삭제 | DELETE | `/nurses/{nurse_id}` | — | — |
| 속성 이력(period) 백필 | 간호사 속성 period 초기 시드(백필) [크론/내부] | POST | `/nurse-period/backfill` | group_id, valid_from, attributes | — |
| 속성 시점변경 | 간호사 속성 시점 변경(close-before-open) [크론/내부] | POST | `/nurse-period/change` | attribute, nurse_id, valid_from, value, group_id, note | — |
| 속성 캐시 롤(as-of) | 속성 period→nurses 캐시 투영 [크론/내부] | POST | `/nurse-period/roll` | group_id, as_of, attributes | — |
| 팀설정(생성/이름변경/멤버편성) | 팀 일괄 동기화 | PUT | `/teams` | teams, delete_team_ids, year, month | — |
| 팀 자동분류 미리보기 | 원티드 기반 팀 자동분류 미리보기 [읽기성] | POST | `/teams/classify/preview` | year, month, group_id, participant_ids, pair_decisions | — |
| 팀 자동분류 적용 | 팀 자동분류 적용(1일 발효) | POST | `/teams/classify/apply` | year, month, group_id, assignments, pair_decisions, note | — |
| 병동 간 재분배 미리보기 | 병동 간 재분배 미리보기 [읽기성] | POST | `/teams/redistribute/preview` | group_ids, year, month, capacity_mode, target_sizes, size_tolerance, churn_weight, participant_ids, team_counts, allow_missing_g1 | — |
| 병동 간 재분배 적용 | 병동 간 재분배 적용 | POST | `/teams/redistribute/apply` | group_ids, year, month, assignments, note | — |
| 등급설정(그룹 등급 제약) | 그룹 등급 설정 저장/갱신 | POST | `/grade/config` | null_grade_policy, use_dynamic_scaling, allow_soft_fallback, constraints, constraints_max, grade_names, use_mid, default_shifts, group_id | — |

#### 설정 (9)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 주휴설정(그룹 정책) | 주휴 그룹 정책 저장 | PUT | `/weekly-off/settings` | activate, use_variable_cycle, cycle_type, cycle_start_date, cycle_interval, shift_variation, group_id | — |
| 주휴설정(간호사별) | 간호사별 주휴 설정 저장 | PUT | `/weekly-off/nurses` | items[].nurse_id, items[].weekly_off_enabled, items[].weekly_off_weekday | — |
| 병동(그룹) 생성 | 병동(그룹) 생성 | POST | `/groups` | group_name | — |
| 병동(그룹) 이름 수정 | 병동(그룹) 이름 수정 | PATCH | `/groups/{group_id}` | group_name | — |
| 그룹 관리자(HN) 지정/해제 | 간호사에게 그룹 관리자 권한 지정/해제 | PUT | `/groups/hn-admin` | nurse_id, group_ids | — |
| 그룹 전환 | 관리 그룹 전환(JWT 재발급) | POST | `/auth/switch-group` | target_group_id | — |
| 회원 엑셀 일괄등록 | 회원 엑셀 업로드·일괄 등록 | POST | `/setting/member_upload` | file | — |
| 부서 엑셀 일괄등록 | 부서 엑셀 업로드·일괄 등록 | POST | `/setting/division_upload` | file | — |
| 직위 엑셀 일괄등록 | 직위 엑셀 업로드·일괄 등록 | POST | `/setting/position_upload` | file | — |

#### 설정(근무코드) (8)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 근무코드 CRUD | 근무코드 추가 | POST | `/shifts/add` | shift_id, name, color, type, default_shift, shift_gb, start_time, end_time, duration, allday, auto_schedule, show_in_preference, off_swap_target, description, group_id | — |
| 근무코드 CRUD | 근무코드 수정 | POST | `/shifts/update` | id, shift_id, name, color, type, default_shift, shift_gb, start_time, end_time, duration, allday, auto_schedule, show_in_preference, off_swap_target, description, group_id | — |
| 근무코드 CRUD | 근무코드 삭제 | POST | `/shifts/remove` | shift_id, group_id | — |
| 근무코드 CRUD | 근무코드 순서 변경 | POST | `/shifts/move` | shift_id, new_sequence, group_id | — |
| 엑셀 일괄 | 근무코드 엑셀 업로드 검증 | POST | `/shifts/upload-validate` | file, group_id | — |
| 엑셀 일괄 | 근무코드 엑셀 업로드 확정 저장 | POST | `/shifts/upload-confirm` | rows, group_id | — |
| 근무코드 가져오기 | 타 병동 근무코드 현재 그룹으로 가져오기 | POST | `/shifts/import-to-group` | shift_ids, group_id | — |
| 시프트 관리 | 시프트 관리 슬롯(교대별 인원/코드) 저장 | POST | `/shift-manage/save` | class_name, slots, group_id | — |

#### 마이페이지 (15)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 기본정보 수정 | 마이페이지 기본정보 수정 | PATCH | `/nurses/personnel-basic-info` | experience | email |
| 프로필 이미지 | 프로필 이미지 업로드 | POST | `/nurses/profile-image` | — | — |
| 프로필 이미지 | 프로필 이미지 삭제 | DELETE | `/nurses/profile-image` | — | — |
| 비밀번호 변경 | 비밀번호 변경 | PUT | `/nurses/change-password` | — | current_password, new_password, confirm_password, verification_code |
| 휴대폰 변경/인증발송 | 휴대폰 변경 인증번호 발송 | POST | `/nurses/change-phone/send-code` | — | new_phone_number |
| 휴대폰 변경/검증 | 휴대폰 번호 변경 확정 | PUT | `/nurses/change-phone/verify` | — | new_phone_number, verification_code |
| 푸시 수신설정 변경 | 푸시 수신 여부 변경 | PATCH | `/push/setting` | push_yn | — |
| 쪽지 전송 | 쪽지 전송 | POST | `/message/write` | receiver_nurse_ids, message, message_img | — |
| 쪽지 삭제 | 쪽지 삭제 | DELETE | `/message/delete/{message_id}` | — | — |
| 회원정보 수정 | 회원정보 수정(연락처·주소·조직·비밀번호) | POST | `/member/edit` | EmpSeqNo, account_id, name, gender, JoinDate, Email, zipcode, Address1, Address2, mb_part_managerYN, mb_partName, OfficialTitleName, career, duty, is_head_nurse, nightkeep | CurMemberPass, MemberPass, MemberPassRe, PortableTel, Tel, DateOfBirth |
| 개발/테스트 | 테스트 이메일 발송 [테스트] | POST | `/member/send-email-background` | — | — |
| 개발/테스트 | 테스트 링크푸시 발송 [테스트] | POST | `/member/send-mlink-message` | — | — |
| 개발/테스트 | 테스트 앱푸시 발송 [테스트] | POST | `/member/send-push-message` | — | — |
| 개발/테스트 | 테스트 SMS 발송 [테스트] | POST | `/member/send-sms-message` | — | — |
| 개발/테스트 | 파일 업로드(테스트) [테스트] | POST | `/member/file-upload` | files | — |

#### 고객센터 (1)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 문의 작성 | 고객문의 등록·이메일 발송 | POST | `/contact/write` | username, Email, title, contents, files | PortableTel |

#### 인증 (6)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 로그인 | 로그인(엠웍스 인증·JWT 쿠키 발급) | POST | `/auth/login` | — | username, password |
| 로그아웃 | 로그아웃(쿠키 삭제) | POST | `/auth/logout` | — | — |
| 아이디 찾기 | 아이디 찾기 | POST | `/auth/find_id` | auth_method | EmployeeName, DateOfBirth, gender, PortableTel, Email |
| 비밀번호 찾기(임시발급) | 임시 비밀번호 발급·SMS/이메일 전송 | POST | `/auth/find_pw` | auth_method | memberID, EmployeeName, receivenum, email |
| 토큰 발급 | 머신 SSO 토큰 발급 | POST | `/token/` | — | clientId, clientSecret |
| 토큰 SSO 로그인 | 토큰+회원ID SSO 로그인(JWT 쿠키 발급) | POST | `/token/login` | — | token, MemberID |

#### 알림 (3)

| section | action | method | path | 로깅필드(화이트리스트) | PII 마스킹 |
|---|---|---|---|---|---|
| 알림 읽음처리(코드기준) | 코드 조건 알림 읽음처리 | PATCH | `/push/read` | — | — |
| 알림 단건 읽음처리 | 알림 1건 읽음처리 | PATCH | `/push/read/one` | fk_idx | — |
| 알림 전체 읽음처리 | 안읽은 알림 전체 읽음처리 | PATCH | `/push/read/all` | — | — |

## 5. 프론트엔드 — CTA 인벤토리 (`ui_events`)

> 6개 FE 인벤토리 병합·중복제거(event 기준). page별 표. **kind=mutation**(서버 변경)을 상단 정렬·강조. `toggle`/`nav`/`ui`/`state`/`action`/`export`/`interaction` 은 하위. 레거시/미마운트 컴포넌트는 비고에 표기.

#### 홈 (1 · mutation 0)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| 이동 | 근무표 만들기(카드) | `home.create_roster.navigate` | nav | — | — |

#### 원티드 (14 · mutation 11)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| 신청 | 에이드(AI) 원티드 작성 제출 | `wanted.aide.submit` | **mutation** | POST /wanted/invoke → POST /preferences | — |
| HN 일자별 설정 | 일자별 신청수 입력 | `wanted.calendar.daily_config_change` | **mutation** | 콜백(dailyConfigs, 저장은 /wanted/config) | — |
| 캘린더 | 날짜 근무코드 셀/삭제 | `wanted.calendar.date_edit` | **mutation** | 콜백(setSelectedCalendarData, 저장은 index.tsx 임시저장/제출) | — |
| 캘린더 | 사유 저장/삭제 | `wanted.calendar.reason_edit` | **mutation** | 콜백(comment) | — |
| 초기화 | 초기화 | `wanted.calendar.reset` | **mutation** | POST /wanted/invoke → POST /preferences | — |
| 저장 | 임시저장 | `wanted.calendar.save` | **mutation** | POST /wanted/invoke → POST /preferences | — |
| 희망동료 | 동료 삭제/비번확인 | `wanted.preference.edit` | **mutation** | 콜백(로컬, 저장은 제출) | — |
| 수간호사 원티드 요청 | 원티드 요청 마감하기 | `wanted.request.close` | **mutation** | PATCH /wanted/close | — |
| 수간호사 원티드 요청 | 요청하기/마감일 변경 | `wanted.request.submit` | **mutation** | requestWantedDeadline/saveChangeWantedDeadline(POST /wanted/request \| PATCH /wanted/deadline) | — |
| 제출 | 제출하기(최종) | `wanted.submit.final` | **mutation** | POST /preferences/submit \| /preferences/submit/empty | — |
| 취소 | 수정하기(제출 철회) | `wanted.submit.retract` | **mutation** | POST /preferences/retract | — |
| 신청 | 캘린더 날짜 선택(근무 배치) | `wanted.calendar.date_select` | interaction | — | — |
| 신청 | 근무 유형 선택 | `wanted.shift_type.select` | ui | — | — |
| 조회 | 전체 근무자 원티드 현황 보기 | `wanted.overview.open` | nav | — | — |

#### 근무표보기 (7 · mutation 1)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| 공유 | 근무표 공유 링크 생성 | `roster_view.share.create` | **mutation** | POST /roster/shares/schedules/{id}/capture | — |
| 다운로드 | 근무표 엑셀 다운로드 | `roster_view.download.excel` | action | GET /roster/schedule/{id}/export | — |
| 다운로드 | 내 근무표 이미지 다운로드 | `roster_view.download.image` | action | — | — |
| 공유 | 공유 링크 복사 | `roster_view.share.copy_link` | action | — | — |
| (조회) 발행월 이동 | 이전/다음/월선택 | `roster_view.calendar.month_nav` | nav | — | — |
| (조회) 탭 | 내 근무표/전체 근무표 전환 | `roster_view.tab.navigate` | nav | — | — |
| 필터 | 병동 선택 | `roster_view.ward.select` | nav | — | — |

#### 근무자관리 (60 · mutation 37)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| 사이드프로필-파견 설정 모달 | 추가/수정(확인) | `nurse_mgmt.dispatch.commit` | **mutation** | PATCH /nurses/{id}(즉시) 또는 로컬 | 저장된 배정은 즉시 PATCH, 신규는 프로필 저장 시 |
| 사이드프로필-파견 삭제 확인 | 삭제 확인 | `nurse_mgmt.dispatch.remove` | **mutation** | PATCH /nurses/{id} cancel 또는 로컬 | — |
| 근무자 엑셀 업로드(구) | 파일 업로드/저장 | `nurse_mgmt.excel.upload_confirm` | **mutation** | excel.upload / excel.confirm_upload(→/nurses/confirm-upload) | — |
| 근무자 엑셀 업로드 | 저장 | `nurse_mgmt.excel2.confirm` | **mutation** | POST /nurses/upload2-confirm | 오류 0건일 때만 |
| 근무자 엑셀 업로드 | 검증 | `nurse_mgmt.excel2.validate` | **mutation** | POST /nurses/upload2-validate | — |
| Grade 관리 모달 | +Grade 추가/이름수정/삭제/저장 | `nurse_mgmt.grade.manage` | **mutation** | 콜백(초안→저장 시 반영) | 최대 11개. 저장은 GradeSettingContents 반영 |
| Grade 등급 이동표 | 간호사 등급 이동(→/←) | `nurse_mgmt.grade.move_nurses` | **mutation** | 콜백(로컬, 저장 시 등급 반영) | — |
| Grade 설정-저장 확인 | 저장 확인 | `nurse_mgmt.grade.save` | **mutation** | POST /grade/config + POST /nurses/bulk-update | ★등급 설정 실제 저장 |
| Grade 설정 모달 | 저장/종료전 저장 | `nurse_mgmt.grade.save_trigger` | **mutation** | POST /grade/config, /nurses/bulk-update | — |
| 상단 병동/그룹 드롭다운 | 병동/그룹 선택(전환) | `nurse_mgmt.group_list.switch_group` | **mutation** | ADMIN:navigate+clear / HN:POST /auth/switch-group | HN은 토큰 교체 |
| 비활성 근무자(DnD) | 활성/비활성 전환(드래그드롭) | `nurse_mgmt.inactive.dnd_toggle_active` | **mutation** | saveChangeFullNurseSchema | — |
| 비활성 근무자 설정-저장 확인 | 저장 확인 | `nurse_mgmt.inactive_set.save` | **mutation** | saveChangeFullNurseSchema | 선택=비활성(0), 나머지=활성(1) |
| 레거시 | 팀 추가/수정/삭제 등 | `nurse_mgmt.legacy.*` | **mutation** | PUT /teams / POST /nurses/bulk-update | 와일드카드 집합 이벤트 · 라이브 미사용(트윈이 feature/head_nurse_mgmt) |
| 레거시(팀/등급 지정) | 추가/수정/삭제/해제 | `nurse_mgmt.legacy.team_grade_ops` | **mutation** | 콜백 | ★레거시 컴포넌트(현재 페이지 미연결 추정) |
| 정렬 | 경력/Grade/이름 정렬(순서저장) | `nurse_mgmt.list.sort` | **mutation** | POST /nurses/sequence/reorder | — |
| 사이드프로필-월개수제한 모달 | 저장 | `nurse_mgmt.monthly_limit.save` | **mutation** | useSaveNurseMonthlyLimits(→PUT /nurses/monthly-limits) | 나이트 최대 15 검증 |
| 월근무한도(나이트일괄) | 나이트 개수 일괄 저장 | `nurse_mgmt.night_bulk.apply` | **mutation** | POST /nurses/monthly-limits/night-bulk | — |
| 상단액션바 | 전체 삭제(본인 제외) | `nurse_mgmt.nurse.delete_all` | **mutation** | POST /nurses/bulk-update | — |
| 근무자 추가 모달 | 선택한 N명 저장 | `nurse_mgmt.nurse_add.save` | **mutation** | POST /nurses/add-to-group | — |
| 근무자 관리 표 신규행 | 저장 | `nurse_mgmt.nurse_new.save` | **mutation** | saveChangeFullNurseSchema | — |
| 근무자목록 | 행 삭제(휴지통) | `nurse_mgmt.nurse_row.delete` | **mutation** | DELETE /nurses/{id} \| 콜백(deleteNurse) | — |
| 근무자삭제확인 | 삭제 확인('동의합니다') | `nurse_mgmt.nurse_row.delete_confirm` | **mutation** | DELETE /nurses/{id} | — |
| 순서변경 | 아래로 이동(▼) | `nurse_mgmt.nurse_row.reorder_down` | **mutation** | POST /nurses/sequence/reorder | — |
| 순서변경 | 위로 이동(▲) | `nurse_mgmt.nurse_row.reorder_up` | **mutation** | POST /nurses/sequence/reorder | — |
| 근무자목록 | 행 저장 | `nurse_mgmt.nurse_row.save` | **mutation** | PATCH /nurses/{id} (+신규행 POST /nurses/bulk-update, 나이트변경 PUT /nurses/monthly-limits) \| saveChangeFullNurseSchema(→POST /nurses/bulk-update) | 수간호사 최소 1명 가드 |
| 원티드 기반 재배치 모달 | {month}부터 적용 | `nurse_mgmt.redistribute.apply` | **mutation** | POST /teams/classify/apply \| /teams/redistribute/apply | 선택 월 발효 |
| 원티드 기반 재배치 모달 | 미리보기 | `nurse_mgmt.redistribute.preview` | **mutation** | POST /teams/classify/preview \| /teams/redistribute/preview | — |
| Shift 최소인원 설정 | 저장/추가/이름수정/삭제 | `nurse_mgmt.shift_req.edit` | **mutation** | 콜백 | Grade/Team Shift 공용. GradeShiftConfig 현재 비활성 |
| 사이드프로필-파견 즉시저장 | 파견 즉시 저장 | `nurse_mgmt.side_profile.dispatch_immediate_save` | **mutation** | PATCH /nurses/{id} | — |
| 근무자 사이드프로필-저장 확인 | 저장 확인 | `nurse_mgmt.side_profile.save` | **mutation** | PATCH /nurses/{id} (+PUT /groups/hn-admin) | ★profile diff + assignment/preceptor period 병합. 근무자관리+근무표생성 공용 |
| Team 관리 모달 | +추가/이름수정/삭제/저장 | `nurse_mgmt.team.manage` | **mutation** | 콜백(초안→저장 반영) | — |
| Team 설정-이동표 | 간호사 팀 이동 | `nurse_mgmt.team.move_nurses` | **mutation** | 콜백(로컬,저장 시 반영) | — |
| Team 설정-저장 확인 | 저장 확인 | `nurse_mgmt.team.save` | **mutation** | PUT /teams (year/month 발효) | ★팀 설정 실제 저장. add/remove/delete_team_ids |
| Team 설정 모달 | 저장/종료전 저장 | `nurse_mgmt.team.save_trigger` | **mutation** | PUT /teams | — |
| 근무자 관리 팀 테이블(DnD) | 활성/비활성 전환(드래그드롭) | `nurse_mgmt.team_table.dnd_toggle_active` | **mutation** | saveChangeFullNurseSchema | — |
| 팀설정/팀이동 | 좌/우 팀으로 이동(팀 배정) | `nurse_mgmt.team_transfer.move` | **mutation** | 상위 TeamSetting 저장(PUT /teams) | — |
| 부서(병동) 추가/변경 모달 | 확인 | `nurse_mgmt.ward.save` | **mutation** | req.create_group / req.update_group | 신규=create_group, 수정=update_group |
| 근무자 추가 표 행 | 행 선택 토글 | `nurse_mgmt.available_member.toggle_select` | toggle | 콜백 | — |
| 사이드프로필-고정근무/주말휴무 | 고정근무 선택/주말휴무 스위치 | `nurse_mgmt.fixed_shift.select` | toggle | 콜백(저장 시 반영) | — |
| 사이드프로필-관리그룹 | 그룹 체크박스 토글 | `nurse_mgmt.managed_group.toggle` | toggle | 콜백(저장 시 반영) | — |
| 월근무한도(나이트일괄) | 고정/최대 종류 선택 | `nurse_mgmt.night_bulk.kind_toggle` | toggle | — | — |
| 근무자목록 | 행 수정 진입(연필) | `nurse_mgmt.nurse_row.edit_open` | toggle | — | — |
| 근무자목록 | 나이트 고정/최대 전환(인라인) | `nurse_mgmt.nurse_row.night_kind_toggle` | toggle | 저장 시 PUT /nurses/monthly-limits | — |
| 사이드프로필-근무상태 | 근무상태 탭(병동이동/파견/휴직/퇴사) | `nurse_mgmt.nurse_status.select_reason` | toggle | — | — |
| 사이드프로필-프리셉터/프리셉티 | 모드/프리셉티/프리셉터 선택·기간·삭제 | `nurse_mgmt.preceptor.edit` | toggle | 콜백(저장 시 PATCH nested) | — |
| 근무형태편집 | 근무코드 체크/고정근무 선택(행편집) | `nurse_mgmt.shift_edit.change` | toggle | 커밋은 행 저장 PATCH /nurses/{id} | — |
| 사이드프로필-전담 근무형태 | 메인/서브 근무형태 토글 | `nurse_mgmt.shift_type.toggle` | toggle | 콜백(allowed_shifts) | — |
| 사이드프로필-그룹관리자 | 그룹관리자 스위치 | `nurse_mgmt.side_profile.toggle_group_admin` | toggle | 저장 시 PUT /groups/hn-admin | — |
| 월/보기컨트롤 | 전체보기/팀별보기 전환 | `nurse_mgmt.view.toggle_view_mode` | toggle | — | — |
| 사이드프로필-주휴 | 주휴 요일 선택 | `nurse_mgmt.weekly_off.select` | toggle | 콜백(저장 시 PATCH) | — |
| 상단액션바 | 근무자 엑셀 업로드(모달) | `nurse_mgmt.excel.upload_open` | nav | — | — |
| 근무자 엑셀 업로드 | 전체 근무자 정보 가져오기 | `nurse_mgmt.excel2.export_members` | nav | GET /nurses/export-members | 마스터관리자 전용 |
| 상단액션바 | Grade 설정(모달) | `nurse_mgmt.grade.open` | nav | — | — |
| 상단 병동/그룹 드롭다운 | +부서(병동)추가 / 편집 | `nurse_mgmt.group_list.open_add_edit` | nav | AddGroupModal | ADMIN 전용 |
| 상단액션바 | 비활성 근무자 설정(모달) | `nurse_mgmt.inactive.open` | nav | — | — |
| 월/보기컨트롤 | 이전달/다음달 | `nurse_mgmt.month.shift` | nav | — | — |
| 상단액션바 | 근무자 추가(모달) | `nurse_mgmt.nurse.add_open` | nav | — | — |
| 근무자목록 | 이름 클릭(사이드프로필) | `nurse_mgmt.nurse_row.open_side_profile` | nav | 콜백 | — |
| 상단액션바 | 팀 설정(모달) | `nurse_mgmt.team.open` | nav | — | — |
| 상단액션바 | 병동 재분배(모달) | `nurse_mgmt.ward_redistribute.open` | nav | — | — |

#### 근무표만들기 (29 · mutation 20)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| 생성설정모달 | 프리셋 삭제 | `roster_create.config.preset_delete` | **mutation** | DELETE /roster/config/{id} | — |
| 긴급대체 | 설정완료(적용) | `roster_create.emergency.apply` | **mutation** | 콜백 onApplyReplacement(로컬 셀 반영, 저장은 발행/생성) | — |
| 긴급대체 | 대체 근무자 찾기(당일/기간) | `roster_create.emergency.recommend` | **mutation** | POST /roster/replacement/recommend | — |
| 생성/발행(라이브 훅) | 생성/발행/발행취소/설정저장/복사/주휴생성/일별교체 | `roster_create.hooks.*` | **mutation** | POST /roster_create/async·/roster/publish·/roster/unpublish·/roster/config/save·/roster/copy·/roster/create-with-weekly-off·PUT /daily-shift | 와일드카드 집합 이벤트 · 트리거 버튼은 ButtonGroup/모달, API 호출부는 pages/roster-create 훅 |
| 일자별 근무인원 | 전체 반영(월기본→일자) | `roster_create.manpower.apply_monthly` | **mutation** | 로컬 draft | — |
| 일자별 근무인원 | 저장하기 | `roster_create.manpower.save` | **mutation** | PUT /daily-shift | — |
| [테스트변형] 마법사 | 기본/고도화 생성·전략선택·토글 | `roster_create.quick_config.*` | **mutation** | POST /roster_create/async | 와일드카드 집합 이벤트 · host-gate 미마운트 변형 |
| 상단버튼바 | 근무표 복사 | `roster_create.roster.copy` | **mutation** | POST /roster/copy/{id} | — |
| 상단버튼바 | 빈 근무표 생성 | `roster_create.roster.create_empty` | **mutation** | POST /roster/create-with-weekly-off | — |
| HN편집 | 삭제 | `roster_create.roster.delete` | **mutation** | DELETE /roster/{id} | — |
| 생성설정모달 | 근무표 만들기(생성) | `roster_create.roster.generate` | **mutation** | POST /roster_create/async (또는 저장방식 선택 후) | — |
| 생성설정모달 | 새 설정으로 저장/기존 업데이트+생성 | `roster_create.roster.generate_save` | **mutation** | useSyncRosterCreateConfig(POST /roster/config/save)+onGenerate | — |
| 마감 | 마감/마감 철회 | `roster_create.roster.publish_toggle` | **mutation** | POST /roster/publish \| /roster/unpublish | — |
| HN편집 | 저장 | `roster_create.roster.save` | **mutation** | POST /roster/save | — |
| 버전 | 버전 이름 저장 | `roster_create.version.rename_save` | **mutation** | mutate.saveScheduleVersionName (PATCH /roster/{id}/name) | — |
| 원티드 | 원티드 요청 마감하기 | `roster_create.wanted.close_request` | **mutation** | PATCH /wanted/close | — |
| 원티드 | 요청하기/마감일 변경 | `roster_create.wanted.submit_request` | **mutation** | requestWantedDeadline / saveChangeWantedDeadline (POST /wanted/request \| PATCH /wanted/deadline) | — |
| 원티드 관리보드 | 저장 | `roster_create.wanted_board.save` | **mutation** | POST /wanted/adjustment | — |
| 원티드 관리보드 | 전체 반영/전체 미반영(reset) | `roster_create.wanted_board.toggle_all_or_reset` | **mutation** | POST /wanted/adjustment/{y}/{m}/reset | — |
| 원티드 설정 | 저장하기 | `roster_create.wanted_config.save` | **mutation** | ConfTab4(POST /nurses/bulk-update, /wanted/config) | — |
| 상단버튼바 | 근무표 다운로드(엑셀) | `roster_create.roster.download_excel` | export | GET /roster/schedule/{id}/export | — |
| 수동조정 | 근무 셀 클릭(수정) | `roster_create.manual_edit.cell_edit` | state | — | 로컬 편집, 저장은 roster.save/publish |
| 생성설정모달 | 설정 draft 편집(토글/숫자값 다수) | `roster_create.config.draft_edit` | toggle | 로컬(생성 시 반영) | — |
| 일자별 근무인원 | M(MID) 사용/최대인원 토글 | `roster_create.manpower.toggle` | toggle | onUseMidChange | — |
| HN편집 | 수정(편집모드) | `roster_create.roster.edit_mode_toggle` | toggle | — | — |
| 긴급대체 | 긴급대체(모달) | `roster_create.emergency.open` | nav | — | — |
| 생성 | 근무표 만들기(설정 모달) | `roster_create.roster.open_generate` | nav | — | — |
| 버전 | 버전 항목 선택 | `roster_create.version.select` | nav | — | — |
| 원티드 | 원티드(선택 모달) | `roster_create.wanted.open_selector` | nav | — | — |

#### 설정 (12 · mutation 6)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| (근무코드) 근무코드 목록 행 | 삭제(휴지통) | `roster_config.shiftcode.delete` | **mutation** | deleteShift(확인모달)+saveShiftManage(POST /shift-manage/save) | — |
| (근무코드) 근무코드 삭제확인 | 예(삭제 확정) | `roster_config.shiftcode.delete_confirm` | **mutation** | deleteMutation(POST /shifts/remove) | — |
| (근무코드) 근무코드 추가/수정 | 추가하기/저장 | `roster_config.shiftcode.submit` | **mutation** | POST /shifts/add \| /shifts/update (+ /shift-manage/save) | — |
| (원티드) 원티드 설정 일자별 | 일괄적용 | `roster_config.wanted.bulk_apply` | **mutation** | 로컬(저장 시 반영) | — |
| (원티드) 원티드 설정 | 저장하기 | `roster_config.wanted.save` | **mutation** | POST /nurses/bulk-update, DELETE /wanted/config/toggle, POST /wanted/config | — |
| (주휴·레거시) 주휴 설정 | 저장하기 등 | `roster_config.weeklyoff.*` | **mutation** | useSaveWeeklyOffSettings/Nurses(PUT /weekly-off/settings·/nurses) | 와일드카드 집합 이벤트 · ★ConfTab3 현재 미배선(레거시). 라이브 주휴 저장은 사이드프로필 경유 |
| (레거시) 기본/법정/부서내규 토글 | 각 제약 스위치(연속근무·ND/ED·나이트·OFF·MID 등) | `roster_config.legacy.toggle_*` | toggle | POST /roster/config/save (ConfTab0 저장) | 와일드카드 집합 이벤트 · ★ConfTab0 현재 미렌더(Roster_configure는 ConfTab1만). 제약 config 값은 생성설정모달로 이동 |
| (근무코드) 근무코드 설정 툴바 | 코드 추가(폼 열기) | `roster_config.shiftcode.add_open` | toggle | 실제 생성은 ShiftCodeFormDialog | — |
| (근무코드) 근무코드 목록 행 | 수정(연필) | `roster_config.shiftcode.edit_open` | toggle | 실제 저장 ShiftCodeFormDialog | — |
| (근무코드) 근무코드 폼 필드 | 구분/색상/타입/원티드반영/OFF대체 등 | `roster_config.shiftcode.field_edit` | toggle | 로컬(제출 시 반영) | — |
| (원티드) 원티드 설정 항목 | Aide/시크릿/요청제한/일자제한 스위치 | `roster_config.wanted.toggle_*` | toggle | 로컬(저장 시 반영) | 와일드카드 집합 이벤트(여러 개별 액션 포함) |
| (인력) 인력설정 | 근무코드 수정하기 | `roster_config.manpower.edit_shiftcodes` | nav | 콜백 | — |

#### 마이페이지 (7 · mutation 6)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| 기본정보 | 이메일/총경력 저장 | `mypage.basic_info.save` | **mutation** | PATCH /nurses/personnel-basic-info | — |
| 보안 | 비밀번호 변경 | `mypage.password.change` | **mutation** | PUT /nurses/change-password | — |
| 연락처 | 인증번호 발송/재발송 | `mypage.phone.send_code` | **mutation** | POST /nurses/change-phone/send-code | — |
| 연락처 | 연락처 변경(인증 확인) | `mypage.phone.verify_change` | **mutation** | PUT /nurses/change-phone/verify | — |
| 프로필 | 프로필 이미지 삭제 | `mypage.profile_image.delete` | **mutation** | DELETE /nurses/profile-image | — |
| 프로필 | 프로필 이미지 업로드 | `mypage.profile_image.upload` | **mutation** | POST /nurses/profile-image | — |
| 프로필 | 이모지 선택 | `mypage.emoji.select` | ui | — | — |

#### 계정관리 (5 · mutation 3)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| 부서 등록 엑셀 | 부서 등록 엑셀 업로드 | `account_mgmt.division.excel_upload` | **mutation** | excel.upload_mworks_division | — |
| 직원 등록 엑셀 | 직원 등록 엑셀 업로드 | `account_mgmt.member.excel_upload` | **mutation** | excel.upload_mworks_member | — |
| 직위 등록 엑셀 | 직위 등록 엑셀 업로드 | `account_mgmt.position.excel_upload` | **mutation** | excel.upload_mworks_position | — |
| 조회 | 탭 전환(직위/부서/직원) | `account.tab.navigate` | nav | — | — |
| 템플릿 | 템플릿 다운로드/파일 선택 | `account_mgmt.excel.template_or_pick` | nav | templateUrl / onFileUpload | — |

#### 고객센터 (5 · mutation 4)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| 문의내역 행 | 삭제 | `customer.inquiry.delete` | **mutation** | DELETE /contact/{no} | — |
| 문의폼 | 문의하기(제출) | `customer.inquiry.submit` | **mutation** | POST /contact/write | — |
| 문의삭제 | 문의내역 삭제 | `support.inquiry.delete` | **mutation** | DELETE /contact/{no} | — |
| 문의작성 | 문의 작성 등록 | `support.inquiry.write` | **mutation** | POST /contact/write | — |
| 문의내역 | 문의하기(모달 열기) | `customer.inquiry.open` | nav | — | — |

#### 공용/기타(shared) (11 · mutation 7)

| section | cta | event (data-track) | kind | 연결 API | 비고 |
|---|---|---|---|---|---|
| (AI위젯) 에이전트 채팅 | 보내기 | `aide.chat.send` | **mutation** | POST /api/agent/chat/send | 현재 미마운트 |
| (AI위젯) 개발위젯 | query/resume 전송 | `aide.dev.*` | **mutation** | POST /aide/query, /aide/resume, GET /aide/pending | 와일드카드 집합 이벤트 · dev 전용·미마운트 |
| (간호사) 간호사 정보 폼 | 저장하기 | `nurse.form.save` | **mutation** | 콜백(상위 간호사 수정 뮤테이션) | — |
| (교환/이동) 좌우 이동 | 오른쪽/왼쪽으로 이동(팀/등급) | `nurse_swap.move` | **mutation** | 콜백(GradeSetting/TeamTransfer 저장 시 반영) | — |
| (공용) 근무표 셀(편집) | 근무코드 셀 배정 | `roster_table.cell.assign` | **mutation** | 콜백(modDayShift, 저장은 상위 roster.save/publish) | 근무표 편집 핵심 셀 |
| (공용) 근무표 셀 | 긴급대체 슬롯 선택(당일/기간) | `roster_table.cell.emergency_pick` | **mutation** | 콜백 | — |
| (공용) 근무표 행 | 행 위/아래 이동(순서) | `roster_table.row.reorder` | **mutation** | 콜백 onMoveRow(상위 saveNurseSequence) | — |
| (교환/이동) 버킷/행 선택 | 버킷 선택·간호사 선택 | `nurse_swap.select` | toggle | — | — |
| (공용) 정렬·위반토글·위반메뉴 | 이름/Grade/경력 정렬·위반 표시 토글·위반 항목 점프 | `roster_table.header_violation.*` | toggle | 콜백(로컬 정렬/표시) | 와일드카드 집합 이벤트(여러 개별 액션 포함) |
| (공용) 상단 통계/인력/메모 | 인력설정 열기·통계 접기/펼치기·메모편집·프리셉티 포함 | `roster_table.toolbar.*` | toggle | 콜백 | 와일드카드 집합 이벤트(여러 개별 액션 포함) |
| (공용) 근무표 행 | 간호사 이름(사이드프로필) | `roster_table.row.open_profile` | nav | — | — |

## 6. `data-track` 규약

- **이벤트키 형식**: `'<page_slug>.<section_slug>.<action>'` — dot 구분 + snake_case. 예) `nurse_mgmt.nurse_row.save`, `roster_create.roster.generate`, `wanted.submit.final`.
- FE 는 버튼/CTA 에 **`data-track` 속성**을 부착한다 → **autotrack** 이 클릭 시 `event` 와 `params` 를 수집해 `ui_events` 로 전송한다.
- `params` 는 `data-track-params`(또는 dataset) 로 전달. 값 스키마는 2절 공용 파라미터를 따르고, `office_id`/`group_id` 는 파티션키로 **항상** 포함.
- **와일드카드 이벤트**(`…*`, 예 `roster_create.hooks.*`, `nurse_mgmt.legacy.*`)는 여러 구체 액션을 묶은 집합/훅 계층 표기로, 레지스트리에서 `notes` 로 성격을 남긴다.

## 7. 커버리지 · 주의

- **PII 정책**: 값 기록은 BE 화이트리스트(`fields`)에 한한다. `masked`(비밀번호·현재/새 비밀번호·휴대폰·이메일·생년월일·자격증명·업로드 원행 등)는 값 대신 `'***'`. FE 도 PII 파라미터는 `(PII)` 로 표시해 전송 제외/마스킹 대상임을 명시.
- **레거시 컴포넌트**(라이브 미사용): `nurse_mgmt.legacy.*`, `nurse_mgmt.legacy.team_grade_ops`(TeamBtnContainer/MgmtTableRow/GradeTabs/MgmtGroup*), `roster_config.legacy.toggle_*`(ConfTab0 미렌더), `roster_config.weeklyoff.*`(ConfTab3 미배선 — 라이브 주휴 저장은 사이드프로필 경유).
- **미마운트**(코드 존재·미장착): AIDE 위젯 `aide.chat.send`·`aide.dev.*`, 생성 마법사 변형 `roster_create.quick_config.*`(host-gate).
- **공통 UI 프리미티브**(modal/common/grid/ui)는 개별 인벤토리 대상이 아니다 — 대부분 feature CTA 를 래핑하므로 상위 feature 이벤트로 포착된다.
- **BE 예외**: 테스트 스텁(`/member/send-*`, `/member/file-upload`)은 로깅 제외 권장. DEPRECATED 배정 라우트(`/nurses/assignments*`)는 유지되나 `PATCH /nurses/{id}` 로 대체됨. 크론/내부 라우트(`/nurse-period/*`, `/wanted/close-expired`)는 FE 미호출.
- **수치 정합**: 인벤토리 변경 엔드포인트 **111개** vs 배포 카탈로그 라벨 약 106개 — 차이는 위 테스트 스텁/DEPRECATED/크론(비-enrich) 경로.

---
_생성: build_catalog.py · BE 111 엔드포인트 · FE 151 이벤트(mutation 95)_
