# 프론트 연동 계약 — 원티드 팀 분류 / 병동 재분배 / 전출자 가시성

작성: 2026-06-04
대상: `nurse_rostering_front/roster_front`
관련 백엔드: `routers/teams.py`, `team_classify_service.py`, `ward_redistribute_service.py`, `nurse_service.py`

> 공통: 인증은 **쿠키 `access_token=Bearer <jwt>`** (기존과 동일). 모든 응답은 JSON.
> 핵심 UX 패턴: **preview(read-only) → 화면에 리스트업 → 유저 확인 → apply(DB 기록)**.

---

## 1. 옵션1 — 병동 내 원티드 팀 분류

### 1-1. `POST /teams/classify/preview` (read-only)
**Request**
```json
{ "year": 2026, "month": 8, "group_id": "1019076bd1f7" }
```
- `group_id` 선택(미지정 시 호출자 관리 그룹 1개로 자동, 여러 개면 400). master_admin은 지정 필요.

**Response 200**
```json
{
  "target_month": "2026-08",
  "num_teams": 3,
  "num_pool": 17,
  "num_excluded_night": 2,
  "excluded_night": [{ "nurse_id": "...", "name": "..." }],
  "teams": { "4": ["nid1", "nid2"], "5": ["..."], "6": ["..."] },
  "changes": [{ "nurse_id": "nid1", "name": "주지현", "from": "4", "to": "5" }],
  "num_changed": 11,
  "stats": { "objective": 633.3, "overlap_total": 1, "grade_dev_total": 2.66 }
}
```
- `teams`: **team_id(문자열) → 간호사 ID 배열**. 이게 "어느 팀에 누구누구" 리스트업 소스.
- `changes`: 현재팀(`from`) 대비 제안팀(`to`)이 바뀌는 간호사만. 배지/하이라이트용.

**렌더 가이드**: 팀별 카드에 소속 간호사 표시 + `changes`로 이동 표시. **DB 변경 없음**.

### 1-2. `POST /teams/classify/apply`
**Request**
```json
{
  "year": 2026, "month": 8, "group_id": "1019076bd1f7",
  "assignments": [{ "nurse_id": "nid1", "team_id": 5 }],
  "note": "8월 팀분류"
}
```
- 보통 preview의 `teams`를 평탄화해 전달. 현재팀과 같은 건 서버가 skip.

**Response 200**
```json
{ "created": 11, "skipped": 6, "effective_date": "2026-08-01" }
```
- `created`건이 `permanent_change` 이벤트로 발행됨. **대상월 1일 발효**(그 전엔 현재 team_id 유지).

---

## 2. 옵션2 — 병동 간 재분배 (group_id 변경)

### 2-1. `POST /teams/redistribute/preview` (read-only)
**Request**
```json
{
  "group_ids": ["1019076bd1f7", "1019077d4e67"],
  "year": 2026, "month": 8,
  "capacity_mode": "explicit",
  "target_sizes": { "1019076bd1f7": 15, "1019077d4e67": 14 },
  "size_tolerance": 2,
  "churn_weight": 500.0
}
```
- `group_ids`: 화면에서 고른 그룹들(2개 이상, 전부 관리 그룹이어야 함).
- `capacity_mode`: `"even"`(균등) | `"explicit"`(그룹별 목표). explicit이면 `target_sizes` 필수.
- `size_tolerance`/`churn_weight`: 생략 가능(기본 2 / 500). churn_weight↑ = 이동 최소화.

**Response 200**
```json
{
  "target_month": "2026-08",
  "ward_ids": ["1019076bd1f7", "1019077d4e67"],
  "num_wards": 2, "num_pool": 29, "num_excluded_night": 2,
  "excluded_night": [{ "nurse_id": "...", "name": "...", "group_id": "..." }],
  "capacity_mode": "explicit",
  "size_bounds": { "mode": "explicit", "tolerance": 2, "targets": {"...": 15} },
  "warnings": ["선택 그룹에 역할이 섞여 있습니다(['AN','RN']). ..."],
  "wards": {
    "1019076bd1f7": {
      "name": "41병동-RN", "size": 16, "nurse_ids": ["..."],
      "teams": {
        "4": [{ "nurse_id": "...", "name": "..." }],
        "5": [{ "nurse_id": "...", "name": "..." }]
      }
    }
  },
  "moves": [
    { "nurse_id": "...", "name": "...",
      "from": "1019077d4e67", "from_name": "71병동",
      "to": "1019076bd1f7", "to_name": "41병동-RN" }
  ],
  "num_moved": 1,
  "stats": { "objective": 4900.0, "overlap_total": 4, "grade_dev_total": 22.5 }
}
```
- `wards[gid].teams`: **그룹 → 팀 → 간호사(이름 포함)** 중첩. 요청한 "어느 그룹의 어느 팀에 누구누구" 그대로.
  - 팀 라벨이 `"전체"`면 그 병동은 팀 구조 미정의/분해 불가 → 단일 묶음으로 표시.
- `moves`: 병동이 바뀌는 간호사(이동 미리보기). `num_moved`로 규모 경고.
- `warnings`: 역할 혼합 등 — 그대로 노출 권장.

**Response 422 (G1 미설정 — 시니어 지정 유도)**
```json
{
  "detail": {
    "message": "다음 병동에 시니어(grade-1)가 지정되어 있지 않습니다: 71병동. ...",
    "needs_g1_setup": [{ "group_id": "1019077d4e67", "name": "71병동" }]
  }
}
```
- **렌더 가이드**: `needs_g1_setup`의 병동들에 "시니어(G1) 먼저 지정" 안내/딥링크. 재분배 진행 차단.

### 2-2. `POST /teams/redistribute/apply`
**Request**
```json
{
  "group_ids": ["1019076bd1f7", "1019077d4e67"],
  "year": 2026, "month": 8,
  "assignments": [
    { "nurse_id": "nidA", "to_group_id": "1019077d4e67", "team_id": 4 },
    { "nurse_id": "nidB", "to_group_id": "1019076bd1f7", "team_id": null }
  ],
  "note": "8월 재분배"
}
```
- `to_group_id`: 그 간호사가 갈 병동. 현재와 같으면 (팀만 바뀌면 permanent_change, 동일하면 skip).
- `team_id`: 정수만 적용(없거나 `"전체"` 라벨이면 `null`).

**Response 200**
```json
{ "transfers": 1, "team_changes": 0, "skipped": 1, "effective_date": "2026-08-01" }
```
- `transfers`: 병동이동(transfer) 이벤트 수. `team_changes`: permanent_change 수. **대상월 1일 발효**.

---

## 3. 전출자 과거병동 가시성 (근무자 관리 리스트)

기존 `GET /nurses` (그룹 내 간호사 목록) 응답의 간호사별 필드를 그대로 활용. 신규 엔드포인트 없음.

- 각 간호사: `inbound: [{ reason, status, startDate, endDate, source_group_id, target_group_id, target_group_name, ... }]`
- **방향 판단(프론트)**:
  - `source_group_id == 내 그룹` 인 `reason="병동이동"` 항목 → **전출자**(이 병동을 떠난/떠날 사람). 회색 처리 + 상세 진입 차단(현재 속성은 도착 병동 기준이라 노출 금지).
  - `target_group_id == 내 그룹` → 전입자(기존 동작).
- completed 전출도 내려오므로(소속이 source일 때만), "예전에 우리 병동이었던" 사람도 명단에서 식별 가능.

---

## 4. 공통 에러

| 코드 | 의미 | detail |
|---|---|---|
| 400 | 잘못된 입력(그룹 2개 미만, explicit인데 target_sizes 누락, 정원밴드로 풀 못담음 등) | 문자열 |
| 403 | 비관리자, 또는 관리 권한 없는 그룹 포함 | 문자열 |
| 422 | (재분배 preview) G1 미설정 병동 존재 | `{message, needs_g1_setup:[{group_id,name}]}` |

---

## 5. 권한 요약
- **옵션1**: 관리자(ADM/수간호사/hn_auth) + 대상 그룹이 관리 그룹(`resolve_managed_group_ids`)에 포함.
- **옵션2**: 위 + `group_ids` **전부** 관리 그룹에 포함.
- 분류·재분배는 모두 **선택 그룹 안에서만** 동작. 옵션1은 group_id 불변(team_id만), 옵션2만 group_id 변경.
