# [Frontend] 퇴사자 부분 재생성 (Partial Re-solve) 연동 문서

> 대상: 프론트엔드 팀
> 기능: UI에서 "특정 간호사가 N일부터 퇴사" 를 실행하면, **N일 이전 근무는 그대로 두고 N일부터만** 남은 인원으로 최소 변경 재조정한 **새 draft 근무표**와 **변경 내역(diff)** 을 돌려준다.

---

## 1. 한눈에

- 관리자가 근무표 화면에서 간호사 선택 → "퇴사 처리 후 재조정" → **이 엔드포인트 1회 호출**.
- 백엔드가 **cutoff 이전을 동결**하고, 퇴사자를 빼고, **cutoff 이후만 다시 최적화**해서 **새 draft schedule** 을 만든다.
- 응답으로 **새 schedule_id + 누가 무엇→무엇으로 바뀌었는지(diff) + 경고** 를 준다.
- 프론트는 새 근무표를 그리고 **바뀐 셀을 하이라이트**, 경고를 배너로 표시. 확정은 **기존 draft 발행 UI 그대로**.

> ⚠️ **동기(synchronous) 호출이고 솔버가 실제로 돌아 ~10~30초 걸린다.** 로딩 스피너 + **타임아웃 최소 60초** 필수. (비동기 잡 아님 — 응답이 올 때까지 기다린다.)

---

## 2. 엔드포인트

```
POST /roster_create/partial-resolve/resignation
```
- **인증**: 로그인 쿠키. **수간호사 권한** + 대상 병동 접근 권한 필요.
- **Content-Type**: `application/json`

### 요청 body

| 필드 | 타입 | 필수 | 설명 |
|---|---|:--:|---|
| `schedule_id` | string | ✅ | 재조정 대상(현재 보고 있는) 근무표 id. 보통 현재 화면의 issued 근무표. |
| `resigned_nurse_id` | string | ✅ | 퇴사자 nurse_id. |
| `cutoff_date` | string (`YYYY-MM-DD`) | ✅ | **이 날부터 변경**, 전날까지 동결. "21일 퇴사" → 해당 월 21일. |
| `replacement_preceptor_id` | string \| null | ❌ | 퇴사자가 **프리셉터**였을 때 남은 기간을 인계할 새 프리셉터 nurse_id. 없으면 `null`. |

> 프론트는 이미 `schedule_id`(현재 화면)와 `resigned_nurse_id`(선택한 간호사)를 갖고 있으므로 **id를 그대로 전달**한다. 이름→id 변환 불필요.

**예시**
```json
{
  "schedule_id": "1305d59288dc",
  "resigned_nurse_id": "N01",
  "cutoff_date": "2026-03-21",
  "replacement_preceptor_id": null
}
```

---

## 3. 응답 (200)

```jsonc
{
  "schedule_id": "14d96932f1d8",     // ★ 새 draft 근무표 id (이걸 화면에 렌더)
  "base_schedule_id": "1305d59288dc",// 원본(비교 기준)
  "cutoff_date": "2026-03-21",
  "resigned_nurse": { "nurse_id": "N01", "name": "간호1" },

  "summary": {
    "nurses_touched": 4,     // 근무가 바뀐 '비퇴사자' 수
    "cells_changed": 12,     // 바뀐 (간호사×일) 칸 수 (퇴사자 vacated 제외)
    "warnings": [            // 커버리지 미달 등 '조용한 문제'를 노출. 보통 빈 배열.
      "2026-03-24 나이트 커버리지 미달 (0/1)"
    ]
  },

  "changed_nurses": [        // 변경된 간호사 목록 (num_changes 내림차순). 퇴사자도 포함(kind=resigned).
    {
      "nurse_id": "N01", "name": "간호1", "grade": 4, "team_id": 1,
      "num_changes": 6,
      "changes": [
        { "day": 21, "date": "2026-03-21",
          "from": "D_A", "from_name": "데이",
          "to": "-",    "to_name": "-",
          "kind": "resigned" }        // 퇴사자 → 셀 비움
      ]
    },
    {
      "nurse_id": "N07", "name": "간호7", "grade": 1, "team_id": 1,
      "num_changes": 2,
      "changes": [
        { "day": 21, "date": "2026-03-21",
          "from": "OFF_A", "from_name": "오프",
          "to": "D_A",     "to_name": "데이",
          "kind": "off_to_work" }     // 빈자리 흡수
      ]
    }
  ],

  "roster": {                         // 새 draft 전체 근무표 (기존 생성 응답과 동일 구조). §5 참고
    "year": 2026, "month": 3, "schedule_id": "14d96932f1d8",
    "days_in_month": 31,
    "shift_colors": { "D_A": "#...", "E_A": "#...", "N_A": "#..." },
    "nurses": [
      { "id": "N07", "name": "간호7", "experience": 3,
        "schedule": ["D_A","OFF_A","E_A", /* ...일자별 shift_id, 없으면 "-" */],
        "counts": { "D_A": 8, "E_A": 6, "N_A": 4, "OFF_A": 9 } }
    ],
    "violations": []
  }
}
```

### 필드 상세

- **`schedule_id`** — 새로 만들어진 **draft** 근무표. 화면에 이걸 그린다. (원본 `base_schedule_id` 는 안 건드림.)
- **`summary.warnings`** — 퇴사로 커버리지/팀/등급이 **조용히 미달**되면 사람이 읽을 문장으로 노출. **비어있지 않으면 배너로 강조**(빨강/주황). 정상이면 `[]`.
- **`changed_nurses[].changes[].kind`** — 셀 하이라이트 색 구분용:

  | kind | 의미 | 권장 표시 |
  |---|---|---|
  | `resigned` | 퇴사자 셀이 비워짐(`to="-"`) | 회색/취소선 (퇴사자 행) |
  | `off_to_work` | 쉬던 사람이 빈자리 투입 | 강조(예: 파랑 채움) |
  | `work_to_off` | 근무→오프로 조정 | 연한 강조 |
  | `shift_change` | 근무 종류 변경(D↔E↔N) | 연한 강조 + from→to 툴팁 |

- **`from`/`to`** = shift_id(코드), **`from_name`/`to_name`** = 한글명(툴팁용). `to="-"` 는 근무 없음(퇴사자).

---

## 4. 프론트 흐름

```
[간호사 선택 → "퇴사(N일)부터 재조정"]
   │  (확인 모달: "N일부터 재조정합니다" )
   ▼
POST /roster_create/partial-resolve/resignation   ← 로딩 스피너 (~10~30s)
   │
   ▼ 200
1. 응답 schedule_id 로 새 draft 근무표 렌더
   (기존 '월 조회' 컴포넌트로 schedule_id 로드, 또는 응답 roster 사용 — §5)
2. changed_nurses[].changes[].day 셀에 kind별 색/뱃지, from→to 툴팁
3. summary: "N명·M칸 변경" 배지 + warnings 있으면 경고 배너
4. 퇴사자 행: cutoff 이후 회색/취소선
   │
   ▼
[관리자 확인 → 기존 'draft 발행' UI 로 확정]  ← 별도 승인 API 없음. 기존 발행 흐름 그대로.
```

- **승인 게이트 없음**: 이 응답은 "추가로 생성된 draft" 취급. 관리자가 기존 draft 목록/발행 UI에서 그대로 발행한다.
- **버림**: 관리자가 이 재조정을 원치 않으면 기존 draft 삭제(dropped) 흐름 사용.

---

## 5. 새 근무표 그리드 얻는 법 (2가지 중 택1)

1. **(권장) 기존 월 조회 재사용** — 응답의 `schedule_id` 로 기존 근무표 조회 API를 그대로 호출해 그리드를 받는다. `changed_nurses` diff 만 위에 오버레이.
2. **응답 내장 사용** — 응답 `roster` 필드에 새 draft 전체가 들어있다(기존 생성 응답과 동일 구조). 그리드 = `roster.nurses = [{ id, name, schedule:[일자별 shift_id, 없으면 "-"], counts }, ...]`, 색상 = `roster.shift_colors`, 일수 = `roster.days_in_month`. 추가 호출 없이 바로 렌더 가능. (주의: 간호사 키는 `id` 이고, diff 의 `changed_nurses[].nurse_id` 와 매칭.)

둘 다 결과는 동일. 기존 렌더 파이프라인 재사용이 편하면 1번.

---

## 6. 동작 계약 (프론트가 사용자에게 안내할 기대치)

- **cutoff 이전은 100% 그대로** — 이미 지나간/확정된 근무는 한 칸도 안 바뀐다.
- **빈자리는 실제로 메운다** — 커버리지(일별 최소 인원)를 지키며 남은 인원이 흡수. 물리적으로 불가능한 날만 `warnings` 로 노출(조용히 구멍 안 남김).
- **변경 최소화** — 위(커버리지·근무규칙·공정성)를 지킨 하에서 **원본과 가장 가까운** 해를 고른다. 단, 소인원 병동에서 1명 퇴사분을 흡수하면 여러 명이 조금씩 바뀔 수 있다(정상).
- **근무 규칙 유지** — 병동 config에 **켜져 있는** 규칙(연속근무 상한, 개인 나이트 한도, 나이트 후 회복 OFF, 싱글 나이트 억제 등)은 재조정 후에도 **깨지지 않는다**. (config에서 끈 규칙은 원래 그 병동 정책상 허용이므로 강제하지 않음.)
- **GRADE·Team** 최우선 유지. 퇴사로 특정 등급/팀이 부족해지면 `warnings` 로 알림.

---

## 7. 에러

| status | 의미 | 프론트 처리 |
|---|---|---|
| `403` | 권한 없음(수간호사 아님/병동 접근 불가) | 권한 안내 |
| `404` | `schedule_id` 없음 / 퇴사자 없음 | "대상 근무표/간호사를 찾을 수 없음" |
| `400` | `cutoff_date` 가 해당 월 범위 밖 / `replacement_preceptor_id` 무효 | 입력 재확인 |
| `500` | 재생성 실패 | "재조정 실패, 재시도" |

에러 body: `{ "detail": "<사유>" }`

---

## 8. 주의사항 요약

- ⏱ **동기 호출 ~10~30초** → 로딩 UI + 타임아웃 ≥60s.
- 🔁 결과의 `cells_changed`/`nurses_touched` 수치는 실행마다 다를 수 있음(원본·솔버 특성). **보장(동결·빈자리 메움·규칙 유지)** 은 항상 성립.
- 🆕 응답은 **새 draft** 다. 원본은 그대로 남아있으니, 사용자가 취소해도 원본 손상 없음.
