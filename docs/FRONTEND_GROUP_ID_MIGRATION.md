# 프론트 작업 요청 — 토큰 group_id → 요청 파라미터 전환

> 대상: 프론트엔드 팀
> 배경 커밋: `3baf26c` (feat/even-de-agent-qa-merge)
> 작성 근거: 백엔드 라우터 전수 조사 (이 문서의 엔드포인트 표는 코드에서 직접 추출)

---

## 1. 무엇이 바뀌었나 (1줄 요약)

백엔드가 **그룹/권한을 더 이상 JWT 토큰에서 읽지 않고, `nurse_id`로 DB(`nurses`)와 `groups.hn_id`에서 해석**하도록 바뀌었습니다.
그 결과 **"어느 병동을 대상으로 하느냐"는 토큰이 아니라 요청의 `group_id` 파라미터로 정해집니다.**

- 예전: 그룹 전환 = 토큰 재발행(`/switch-group`) → 토큰 안의 group_id가 대상
- 지금: 그룹 전환 = **요청마다 `group_id`를 실어 보냄** (토큰 재발행 불필요)

### 핵심 규칙 (이것만 지키면 됩니다)
> **사용자가 화면에서 선택한 병동(group_id)을, 해당 요청에 반드시 같이 보낸다.**

`group_id`를 **안 보내면** 백엔드는 **호출자의 DB 소속 병동(home)** 으로 폴백합니다.
- 일반 간호사 / 단일 병동 수간호사 → home == 실제 병동이라 문제 없음.
- **그룹관리자(HN, 여러 병동 관리)** → 안 보내면 **항상 home 병동만** 보게 되어, **선택한 다른 관리 병동이 안 보이거나 수정 불가**. ← 지금 "근무자관리에서 타 병동 안 보임" 증상의 원인.
- **마스터 관리자(ADM)** → home이 없으므로 대부분 엔드포인트에서 **group_id 미전송 시 400** 발생.

---

## 2. 선택한 group_id를 어디서 가져오나

이미 프론트에 선택 상태가 있습니다(확인 필요):
- `localStorage.rc_create_selected_group`
- `localStorage.selectedGroupId`

이 값을 아래 표의 위치(쿼리/바디)에 실어 보내면 됩니다. 파라미터 이름은 전부 **`group_id`** (배열인 재분배만 `group_ids`).

---

## 3. 엔드포인트 전수 표

표기:
- **위치**: `query` = URL 쿼리스트링 `?group_id=...`, `body` = 요청 본문 필드, `path` = 경로
- **필수**: ✅ = 없으면 400, ⬜ = 선택(없으면 home 폴백)
- 모든 엔드포인트 공통: 관리하지 않는 group_id를 보내면 **403**

### 3-1. 근무표 (`/roster`)
| Method | Path | group_id 위치 | 필수 |
|---|---|---|---|
| POST | `/roster/config/save` | query | ⬜ |
| GET | `/roster/config/versions` | query | ⬜ |
| GET | `/roster/config/version/{config_version}` | query | ⬜ |
| GET | `/roster/latest` | query | ⬜ |
| GET | `/roster/issued` | query | ⬜ |
| GET | `/roster/issued_roster` | query | ⬜ |
| GET | `/roster/issued_roster/me` | — (본인 전용) | — |
| GET | `/roster/status` | query | ⬜ |
| DELETE | `/roster/{schedule_id}` | query | ⬜ |
| GET | `/roster/schedule/{schedule_id}` | query | ⬜ |
| GET | `/roster/{year}/{month}/versions` | query | ⬜ |
| GET | `/roster/{year}/{month}` | query | ⬜ |
| GET | `/roster/{year}/{month}/prev-tail` | query | ⬜ |
| POST | `/roster/publish` | query | ⬜ |
| POST | `/roster/unpublish` | query | ⬜ |
| POST | `/roster/save` | query | ⬜ |
| GET | `/roster/{year}/{month}/submissions` | query | ⬜ |
| POST | `/roster/validate` | query | ⬜ |
| PATCH | `/roster/{schedule_id}/name` | query | ⬜ |
| GET | `/roster/schedule/{schedule_id}/export` | query | ⬜ |
| POST | `/roster/copy/{source_schedule_id}` | query | ⬜ |
| POST | `/roster/create-empty` | query | ⬜ |
| POST | `/roster/create-with-weekly-off` | query | ⬜ |
| POST | `/roster/replacement/recommend` | query | ⬜ |
| POST | `/roster/shares/schedules/{schedule_id}` (+`/upload` `/auto` `/capture`) | query | ⬜ |

### 3-2. 원티드 (`/wanted`)
| Method | Path | group_id 위치 | 필수 |
|---|---|---|---|
| POST | `/wanted/request` | query | ⬜ |
| GET | `/wanted/status` | query | ⬜ |
| GET | `/wanted/{year}/{month}/submissions` | query | ⬜ |
| GET | `/wanted/all` | query | ⬜ |
| PATCH | `/wanted/close` | query | ⬜ |
| PATCH | `/wanted/deadline` | query | ⬜ |
| GET | `/wanted/config` | query | ⬜ |
| POST | `/wanted/config` | query | ⬜ |
| DELETE | `/wanted/config` | query | ⬜ |
| DELETE | `/wanted/config/toggle` | query | ⬜ |
| POST | `/wanted/validate-limits` | query | ⬜ |
| GET | `/wanted/over-limit-nurses` | query | ⬜ |
| POST | `/wanted/delete-excess-off/{nurse_id}` | — (대상 nurse로 관리 판정) | — |
| GET | `/wanted/adjustment/{year}/{month}` | query | ⬜ |
| POST | `/wanted/adjustment` | query | ⬜ |
| PATCH | `/wanted/adjustment/entry/{entry_id}/toggle` | query (`caller_group_id`) | ⬜ |
| POST | `/wanted/adjustment/{year}/{month}/reset` | query | ⬜ |
| GET | `/wanted/fixed/{year}/{month}` | query | ⬜ |
| GET | `/wanted/{year}/{month}/shift-requests` | query | ⬜ |

### 3-3. 간호사 (`/nurses`)
| Method | Path | group_id 위치 | 필수 |
|---|---|---|---|
| GET | `/nurses` (근무자관리 목록) | query | ⬜ ※ |
| GET | `/nurses/monthly-limits` | query | ✅ |
| PUT | `/nurses/monthly-limits` | 요청에 포함 | ✅ |
| POST | `/nurses/sequence/save` | query | ⬜ |
| POST | `/nurses/sequence/reorder` | query | ⬜ |
| POST | `/nurses/bulk-update` | query | ⬜ |
| POST | `/nurses/upload2-validate` | query | ✅ |
| POST | `/nurses/upload2-confirm` | query | ✅ |
| GET | `/nurses/available-members` | query | ✅ |
| POST | `/nurses/add-to-group` | body (`group_id`) | ✅ |
| GET | `/nurses/assignments` | query | ⬜ |
| PATCH | `/nurses/{nurse_id}` | query (`group_id`=view group) | ⬜ |

> ※ **`GET /nurses`가 "근무자관리에서 HN이 타 관리 병동을 못 보던" 바로 그 엔드포인트**입니다. 선택 group_id를 query로 보내면 해결됩니다(백엔드는 이미 정상 — group_id 주면 해당 병동 반환 실측 완료).

### 3-4. 근무코드 (`/shifts`, `/shift-manage`)
| Method | Path | group_id 위치 | 필수 |
|---|---|---|---|
| GET | `/shifts` | query | ⬜ |
| POST | `/shifts/add` | query | ⬜ |
| POST | `/shifts/update` | query | ⬜ |
| POST | `/shifts/remove` | query | ⬜ |
| POST | `/shifts/move` | query | ⬜ |
| POST | `/shifts/upload-validate` | query | ✅ |
| POST | `/shifts/upload-confirm` | query | ✅ |
| GET | `/shifts/available-imports` | query | ✅ |
| POST | `/shifts/import-to-group` | body (`group_id`) | ✅ |
| GET | `/shift-manage/{class_name}` | query | ⬜ |
| POST | `/shift-manage/save` | query | ⬜ |

### 3-5. 팀/병동 (`/teams`)
| Method | Path | group_id 위치 | 필수 |
|---|---|---|---|
| GET | `/teams` | query | ⬜ (ADM 미전송 시 office 전체) |
| PUT | `/teams` | query | ⬜ |
| POST | `/teams/classify/preview` | body (`group_id`) | ⬜ |
| POST | `/teams/classify/apply` | body (`group_id`) | ⬜ |
| POST | `/teams/redistribute/preview` | body **`group_ids[]`** | ✅ (2개↑) |
| POST | `/teams/redistribute/apply` | body **`group_ids[]`** | ✅ |

### 3-6. 등급 (`/grade`)
| Method | Path | group_id 위치 | 필수 |
|---|---|---|---|
| GET | `/grade/config` | query | ⬜ |
| POST | `/grade/config` | query | ⬜ |

### 3-7. 근무표 생성 (신규 — **body에 `group_id` 추가**)
생성 요청 DTO(`RosterRequest`)에 **`group_id` 필드가 새로 생겼습니다.** 선택 병동을 본문에 넣어야 HN이 home이 아닌 관리 병동으로 생성할 수 있습니다. 미전송 시 home 폴백.

| Method | Path | group_id 위치 | 필수 |
|---|---|---|---|
| POST | `/roster_create/async` (비동기 생성) | body (`group_id`) | ⬜ |
| POST | `/roster_create/generate` (동기 생성) | body (`group_id`) | ⬜ |
| POST | `/roster/request` | body (`group_id`) | ⬜ |

> 비동기 생성은 제출 시점의 group_id가 워커까지 그대로 전달되므로, **생성 버튼 누를 때 선택 병동을 꼭 넣어주세요.** 안 넣으면 요청자의 home 병동 근무표가 생성됩니다.

---

## 4. 백엔드가 현재 **home 고정**인 곳 (참고 — 필요 시 추가 요청)

아래는 group_id 파라미터를 받지 않고 **항상 호출자 home 병동**으로 동작합니다. HN이 타 관리 병동을 봐야 한다면 백엔드에 group_id 파라미터 추가를 별도 요청해 주세요.
- `GET /dashboard/summary`, `/dashboard/individual`, `/dashboard/trends`
- `constraint_impact` 계열 (`/constraint_impact/...`)
- `preferences` 제출 계열 (본인 희망근무 — self 용도라 home이 맞음)

---

## 5. 에러 처리 (공통)

| 상태 | 의미 | 프론트 처리 |
|---|---|---|
| **400** | ADM인데 group_id를 안 보냄 / 그룹 결정 불가 | "병동을 선택하세요" 유도 |
| **403** | 관리하지 않는 group_id를 보냄 | "해당 병동 접근 권한 없음" 안내 |
| **401** | 인증 없음 | 로그인 유도 |

---

## 6. 그룹 전환 UX 변경

- 기존 `/switch-group` **토큰 재발행 호출 제거 가능** — 더 이상 그룹 전환에 토큰을 다시 받을 필요가 없습니다.
- 대신 **병동 셀렉터에서 고른 group_id를 전역 상태(localStorage)에 저장하고, 위 표의 모든 요청에 주입**하면 됩니다.
- 권한(수간호사/그룹관리자/ADM)·소속 병동은 백엔드가 DB에서 판정하므로, 토큰이 오래되어도(승급/병동이동 후 재로그인 전) 즉시 반영됩니다.

---

## 7. 작업 체크리스트

- [ ] 공통 API 클라이언트에 "선택 group_id 자동 주입" 레이어 추가 (쿼리/바디)
- [ ] `GET /nurses` 에 group_id 전송 → 근무자관리 타 병동 노출 복구
- [ ] 근무표 생성(`/roster_create/*`, `/roster/request`) 본문에 `group_id` 추가
- [ ] `✅ 필수` 표시된 엔드포인트는 group_id 없이 호출되지 않도록 가드
- [ ] 400/403 응답 사용자 메시지 처리
- [ ] `/switch-group` 토큰 재발행 의존 제거 (선택 사항이지만 권장)
- [ ] ADM 계정: 병동 미선택 상태에서 400 나는 화면들 점검
