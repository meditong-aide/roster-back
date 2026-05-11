# 월별 개인 근무제한(Exact-Only) 프론트엔드 연동 가이드

> **대상**: Frontend 개발자 (React/Vue 무관)
> **엔드포인트**: `GET /nurses/monthly-limits`, `PUT /nurses/monthly-limits`
> **목적**: 월별 개인 OFF/근무 개수 제한을 안전하게 저장·조회하고, 저장 전 불가능 케이스를 즉시 안내

---

## 1. 핵심 정책 (반드시 이해)

### 1) Override-only 정책
- 기본값은 전원 `roster_config.off_days` 사용
- `nurse_monthly_limits`에 **row가 있는 간호사만** 월별 개인 제한 override 적용
- 해제하려면 해당 row를 삭제(또는 all-null 입력으로 서버가 삭제 처리)

### 2) Exact-only 우선 운영
- 현재 운영 정책은 `exact` 중심
- 예: `n_exact=2`, `o_exact=8`
- 서버는 `exact`가 있으면 내부적으로 `min=max=exact`로 정규화

### 3) 30% 권장 정책 (차단 아님)
- 개별 제한 설정 대상 비율이 30%를 넘으면 저장은 되지만 `warnings` 반환
- 프론트는 경고 배너/토스트를 띄우고 사용자가 확인 후 진행하게 UX 구성

---

## 2. 인증 방식

이 API는 `Authorization` 헤더가 아니라 **Cookie 기반** 인증을 사용합니다.

- Cookie key: `access_token`
- 값 형식: `Bearer <JWT>`

브라우저 환경에서는 일반적으로 `credentials: "include"` 설정이 필요합니다.

---

## 3. API 스펙

## 3.1 GET `/nurses/monthly-limits`

### Query
- `year` (required)
- `month` (required, 1~12)
- `group_id` (optional)

### 권한/스코프
- 일반 HN/멤버: 자신의 그룹만 조회 가능
  - 다른 `group_id`로 요청 시 `403`
- Master Admin: 그룹 지정 조회 가능

### Response
```json
{
  "items": [
    {
      "nurse_id": "N001",
      "group_id": "GRP001",
      "year": 2026,
      "month": 5,
      "d_min": 3,
      "d_max": 3,
      "d_exact": 3,
      "e_min": null,
      "e_max": null,
      "e_exact": null,
      "n_min": 2,
      "n_max": 2,
      "n_exact": 2,
      "o_min": 8,
      "o_max": 8,
      "o_exact": 8
    }
  ],
  "meta": {
    "target_nurse_count": 6,
    "active_nurse_count": 14,
    "override_ratio": 0.4285714286,
    "recommended_ratio": 0.3
  },
  "warnings": [
    {
      "code": "OVERRIDE_RATIO_EXCEEDED",
      "message": "개별 OFF cap 설정 인원이 권장 비율(30%)을 초과했습니다. 현재 6/14명 (42.9%).",
      "severity": "warning"
    }
  ]
}
```

---

## 3.2 PUT `/nurses/monthly-limits`

### Request
```json
{
  "year": 2026,
  "month": 5,
  "limits": [
    {
      "nurse_id": "N001",
      "group_id": "GRP001",
      "year": 2026,
      "month": 5,
      "d_exact": 3,
      "n_exact": 2,
      "o_exact": 8
    }
  ]
}
```

### 필드 규칙
- `limits[].year/month`는 top-level `year/month`와 일치해야 함
- `exact/min/max`는 0 이상
- `exact`가 있으면 서버가 해당 shift의 `min/max`를 같은 값으로 정규화

### **중요: 동일 scope 중복 금지 (신규 정책)**

동일 요청(`limits` 배열) 안에서 아래 4개가 동일한 항목은 **중복으로 간주**되어 `400` 반환:

- `nurse_id`
- `group_id`
- `year`
- `month`

예시(잘못된 요청):
```json
{
  "year": 2026,
  "month": 5,
  "limits": [
    { "nurse_id": "N001", "group_id": "GRP001", "year": 2026, "month": 5, "d_exact": 10 },
    { "nurse_id": "N001", "group_id": "GRP001", "year": 2026, "month": 5, "e_exact": 10 }
  ]
}
```

프론트는 동일 scope를 **한 row로 병합**해서 보내야 합니다.

---

## 4. 저장 차단(400) 케이스

## 4.1 exact 합이 가용일 초과

예시:
```json
{
  "year": 2026,
  "month": 4,
  "limits": [
    {
      "nurse_id": "N001",
      "group_id": "GRP001",
      "year": 2026,
      "month": 4,
      "d_exact": 20,
      "e_exact": 20
    }
  ]
}
```

응답 예:
```json
{
  "detail": "N001 / group GRP001 설정 불가: exact 합(40) > 그룹 가용일(30)"
}
```

## 4.2 요청 연월 불일치
```json
{ "detail": "요청 year/month와 항목 year/month가 일치해야 합니다." }
```

## 4.3 타 그룹 수정 시도(HN/non-admin)
```json
{ "detail": "현재 그룹 외 limits는 수정할 수 없습니다." }
```

## 4.4 동일 scope 중복
```json
{ "detail": "동일한 (nurse_id, group_id, year, month) 항목이 요청에 중복되었습니다: ..." }
```

---

## 5. 해제(삭제) 동작

한 row의 모든 bound 값(`d/e/n/o min/max/exact`)이 `null`이면 서버가 해당 row를 삭제합니다.

즉, 프론트에서 "설정 해제"는 다음 중 하나로 처리 가능:

1. 해제 버튼에서 all-null payload 전송
2. (추후 API가 생기면) delete endpoint 사용

---

## 6. 프론트 구현 권장사항

### 1) 전송 전 로컬 검증
- `limits` 배열을 보내기 전에 `(nurse_id, group_id, year, month)` 기준으로 중복 검사
- 중복이면 API 호출 전에 병합 또는 사용자에게 에러 표시

### 2) exact-only 입력 UX
- min/max 입력 UI는 숨기고 exact 입력만 노출(현재 운영 정책 기준)
- exact 입력 시 화면 상태에서도 `min=max=exact`로 보이게 할지 여부는 선택

### 3) 경고 처리
- `warnings`가 오면 저장 성공이어도 배너/토스트 표시
- 특히 `OVERRIDE_RATIO_EXCEEDED`는 “권장 초과”임을 명확히 표기

### 4) 에러 메시지 처리
- `detail` 문자열 그대로 1차 표시
- 추후 백엔드에서 `error_code`를 주면 코드 기반 i18n으로 전환 권장

---

## 7. TypeScript 타입 예시

```ts
export interface NurseMonthlyLimitItem {
  nurse_id: string;
  group_id: string;
  year: number;
  month: number;

  d_min?: number | null;
  d_max?: number | null;
  d_exact?: number | null;

  e_min?: number | null;
  e_max?: number | null;
  e_exact?: number | null;

  n_min?: number | null;
  n_max?: number | null;
  n_exact?: number | null;

  o_min?: number | null;
  o_max?: number | null;
  o_exact?: number | null;
}

export interface NurseMonthlyLimitWarning {
  code: string; // OVERRIDE_RATIO_EXCEEDED
  message: string;
  severity: "warning";
}

export interface NurseMonthlyLimitMeta {
  target_nurse_count: number;
  active_nurse_count: number;
  override_ratio: number;
  recommended_ratio: number; // 0.3
}

export interface NurseMonthlyLimitListResponse {
  items: NurseMonthlyLimitItem[];
  meta?: NurseMonthlyLimitMeta | null;
  warnings?: NurseMonthlyLimitWarning[] | null;
}

export interface NurseMonthlyLimitBulkUpsertRequest {
  year: number;
  month: number;
  limits: NurseMonthlyLimitItem[];
}
```

---

## 8. fetch 예시

```ts
export async function putMonthlyLimits(body: NurseMonthlyLimitBulkUpsertRequest) {
  const res = await fetch("/nurses/monthly-limits", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  });

  const data = await res.json();
  if (!res.ok) {
    throw new Error(data?.detail ?? "월별 제한 저장에 실패했습니다.");
  }
  return data as NurseMonthlyLimitListResponse;
}

export async function getMonthlyLimits(year: number, month: number, groupId?: string) {
  const query = new URLSearchParams({ year: String(year), month: String(month) });
  if (groupId) query.set("group_id", groupId);

  const res = await fetch(`/nurses/monthly-limits?${query.toString()}`, {
    method: "GET",
    credentials: "include",
  });

  const data = await res.json();
  if (!res.ok) {
    throw new Error(data?.detail ?? "월별 제한 조회에 실패했습니다.");
  }
  return data as NurseMonthlyLimitListResponse;
}
```

---

## 9. QA 체크리스트

- [ ] 동일 scope 2건 전송 시 `400` 발생 확인
- [ ] exact 합이 월 가용일 초과 시 `400` 발생 확인
- [ ] 30% 초과 시 저장은 성공(`200`) + warnings 노출 확인
- [ ] all-null 전송 시 row 삭제 확인
- [ ] HN 계정으로 타 그룹 수정/조회 시 `403` 확인

---

## 10. 운영 메모

- 현재는 exact-only 중심 운영이므로 FE도 exact 중심 입력 UX를 유지 권장
- 이후 min/max UI를 열 때는 동일 scope 병합 규칙과 에러 표시 규칙을 그대로 유지하면 안전

---

## 11. 실서버 curl 테스트 (실행 로그)

아래는 **2026-05-06** 기준, 로컬 백엔드(`http://127.0.0.1:8000`)에 실제로 호출한 결과입니다.

> 인증: `Cookie: access_token=Bearer <JWT>`

### 11.1 인증 확인 (`/auth/me`)

```bash
curl -i -H "Cookie: access_token=Bearer <JWT>" \
  "http://127.0.0.1:8000/auth/me"
```

실제 응답:
```http
HTTP/1.1 200 OK
content-type: application/json

{"nurse_id":"445872","account_id":"ai_n0003","office_id":"101358","group_id":"101358f6de7b","is_head_nurse":true,"is_master_admin":false,"name":"전도연", ...}
```

---

### 11.2 저장 성공 + warning 포함 (`PUT /nurses/monthly-limits`)

```bash
curl -i -X PUT "http://127.0.0.1:8000/nurses/monthly-limits" \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=Bearer <JWT>" \
  --data '{
    "year": 2026,
    "month": 6,
    "limits": [
      {
        "nurse_id": "177741",
        "group_id": "101358f6de7b",
        "year": 2026,
        "month": 6,
        "o_exact": 8
      }
    ]
  }'
```

실제 응답(요약):
```http
HTTP/1.1 200 OK
content-type: application/json

{
  "items": [ ... ],
  "meta": {
    "target_nurse_count": 6,
    "active_nurse_count": 14,
    "override_ratio": 0.42857142857142855,
    "recommended_ratio": 0.3
  },
  "warnings": [
    {
      "code": "OVERRIDE_RATIO_EXCEEDED",
      "message": "개별 OFF cap 설정 인원이 권장 비율(30%)을 초과했습니다. 현재 6/14명 (42.9%).",
      "severity": "warning"
    }
  ]
}
```

---

### 11.3 exact 합 초과 저장 실패 (`400`)

```bash
curl -i -X PUT "http://127.0.0.1:8000/nurses/monthly-limits" \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=Bearer <JWT>" \
  --data '{
    "year": 2026,
    "month": 4,
    "limits": [
      {
        "nurse_id": "177741",
        "group_id": "101358f6de7b",
        "year": 2026,
        "month": 4,
        "d_exact": 20,
        "e_exact": 20
      }
    ]
  }'
```

실제 응답:
```http
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"177741 / group 101358f6de7b 설정 불가: exact 합(40) > 그룹 가용일(30)"}
```

---

### 11.4 동일 scope 중복 저장 실패 (`400`) — 신규 정책 검증

```bash
curl -i -X PUT "http://127.0.0.1:8000/nurses/monthly-limits" \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=Bearer <JWT>" \
  --data '{
    "year": 2026,
    "month": 5,
    "limits": [
      {
        "nurse_id": "177741",
        "group_id": "101358f6de7b",
        "year": 2026,
        "month": 5,
        "d_exact": 10
      },
      {
        "nurse_id": "177741",
        "group_id": "101358f6de7b",
        "year": 2026,
        "month": 5,
        "e_exact": 10
      }
    ]
  }'
```

실제 응답:
```http
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"동일한 (nurse_id, group_id, year, month) 항목이 요청에 중복되었습니다: 177741, 101358f6de7b, 2026-05"}
```

---

### 11.5 타 그룹 수정 시도 실패 (`403`)

```bash
curl -i -X PUT "http://127.0.0.1:8000/nurses/monthly-limits" \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=Bearer <JWT>" \
  --data '{
    "year": 2026,
    "month": 6,
    "limits": [
      {
        "nurse_id": "177741",
        "group_id": "OTHER_GROUP_X",
        "year": 2026,
        "month": 6,
        "o_exact": 8
      }
    ]
  }'
```

실제 응답:
```http
HTTP/1.1 403 Forbidden
content-type: application/json

{"detail":"현재 그룹 외 limits는 수정할 수 없습니다."}
```

---

### 11.6 조회 성공 + meta/warnings 확인 (`GET`)

```bash
curl -i -G "http://127.0.0.1:8000/nurses/monthly-limits" \
  -H "Cookie: access_token=Bearer <JWT>" \
  --data-urlencode "year=2026" \
  --data-urlencode "month=6"
```

실제 응답(요약):
```http
HTTP/1.1 200 OK
content-type: application/json

{
  "items": [ ... ],
  "meta": {
    "target_nurse_count": 6,
    "active_nurse_count": 14,
    "override_ratio": 0.42857142857142855,
    "recommended_ratio": 0.3
  },
  "warnings": [
    {
      "code": "OVERRIDE_RATIO_EXCEEDED",
      "message": "개별 OFF cap 설정 인원이 권장 비율(30%)을 초과했습니다. 현재 6/14명 (42.9%).",
      "severity": "warning"
    }
  ]
}
```
