# TEAM / GRADE API 연동 가이드 (월별 개인제한 연계 포함)

> 대상: Frontend / API 연동 개발자
> 범위: Team 설정, Grade 설정, 월별 개인 제한(Team 저장 검증에 영향)

---

## 1) API 맵 (핵심 엔드포인트)

## Team
- `GET /teams`
  - 팀 목록 + 멤버 + `min_shift` + `handoff_policy` 조회
- `PUT /teams`
  - 팀/멤버/`min_shift`/`handoff_policy` 동기화 저장

## Grade
- `GET /grade/config`
  - 그룹 grade 제약 설정 조회
- `POST /grade/config`
  - 그룹 grade 제약 설정 저장

## Monthly limits (Team min_shift 검증에 영향)
- `GET /nurses/monthly-limits`
- `PUT /nurses/monthly-limits`

---

## 2) Team API 상세

## 2.1 GET /teams

### Query
- `group_id` (optional)

### Response shape
```json
[
  {
    "team_id": 1,
    "team_name": "A팀",
    "team_members": ["N001", "N002"],
    "min_shift": {"D": 1, "E": 1},
    "handoff_policy": {"restrictions": []}
  }
]
```

---

## 2.2 PUT /teams

### Request shape
```json
{
  "teams": [
    {
      "team_id": 1,
      "team_name": "A팀",
      "add": ["N010"],
      "remove": ["N002"],
      "min_shift": {"D": 1, "E": 1, "N": 0},
      "handoff_policy": {
        "restrictions": [
          {"grades": [1, 2], "block_same_shift": true, "block_adjacent": true}
        ]
      }
    }
  ],
  "delete_team_ids": []
}
```

### `min_shift` 업데이트 규칙
- `null` → 변경 없음
- `{}` → 클리어(제약 없음)
- `{...}` → 유효 키(`D/E/N/M`)만 저장, 음수/비정상값은 정제

### 저장 전 검증 (중요)
`min_shift`를 변경할 때 백엔드는 저장 전에 hard 검증을 수행합니다.

#### A. 기존 검증
- `TEAM_EMPTY_BUT_MIN_SET`
  - 팀 인원이 0인데 일일 최소합 > 0
- `TEAM_ACTIVE_LT_DAY_MIN_SUM`
  - 팀 인원 < 일일 최소합

#### B. 신규 검증 (이번 반영)
- `TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL`
  - 월별 개인 제한(`nurse_monthly_limits`의 `o_exact`/`o_min`)을 고려했을 때,
  - 팀 월 총 필요량(`day_min_sum × days_in_month`) > 팀 월 최대 근무 가능량 합

즉, Team min_shift 저장이 Monthly limits와 모순되면 **저장 실패(400)** 합니다.

### 실패 응답
```json
{
  "detail": "[TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL] A팀 2026-04 기준 월 총 필요량(30)이 팀 월 최대 근무 가능량(0)을 초과해요."
}
```

---

## 3) Grade API 상세

## 3.1 GET /grade/config

### Query
- `group_id` (optional, 권한에 따라 필요)

### Response
`GradeConfigResponse` (서비스에서 반환하는 grade 제약 설정 구조)

---

## 3.2 POST /grade/config

### Request
`GradeConfigUpsert` 스키마 기반 JSON

### Response
`GradeConfigResponse`

### 실패
- `400`: 입력 값/비즈니스 검증 실패
- `403`: 다른 오피스 그룹 접근
- `404`: 그룹 없음

---

## 4) Team ↔ Grade ↔ Monthly limits 연계 관점

## 저장 시점
- Team `min_shift` 저장: 팀 인원 + 월별 개인 제한까지 반영된 hard 검증 수행
- Grade 저장: grade 제약 자체 검증 수행

## 생성/사전점검 시점
- `POST /groups/{group_id}/roster/precheck`에서 Team/Grade 수요-공급 모순 사전 탐지
- Monthly limits는 실제 생성 시 solver 제약으로 반영됨

권장 UX:
1. Team/Grade 설정 저장
2. Precheck 실행
3. 이슈 없을 때 생성 요청

---

## 5) 프론트 구현 포인트

1. **Team 저장 실패 detail 노출**
   - `400`일 때 `detail`을 사용자에게 그대로 보여주기
2. **Monthly limits와 Team min_shift 순서 안내**
   - 개인 OFF exact/min을 크게 올리면 Team min_shift 저장이 막힐 수 있음을 UI에 안내
3. **저장 전 로컬 계산(선택)**
   - `day_min_sum × monthDays` 대략치와 팀 인원/개인 OFF 설정으로 사전 경고 가능

---

## 6) 테스트로 확인된 동작

- `tests/test_team_min_shift_monthly_limits_validation.py`
  - 케이스 1: 팀원 전원 `o_exact=30`, `min_shift={D:1}` → `400` (`TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL`)
  - 케이스 2: 팀원 전원 `o_exact=20`, `min_shift={D:1}` → `200` 저장 성공

---

## 7) 참고 문서
- `docs/MONTHLY_LIMITS_EXACT_ONLY_FRONTEND_GUIDE.md`
- `docs/FRONTEND_PRECHECK_INTEGRATION.md`
