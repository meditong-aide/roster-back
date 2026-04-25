# [기획안] 팀별 설정: `team.min_shift` + `team.handoff_policy`

| 항목 | 내용 |
| --- | --- |
| 문서 버전 | v2.0 |
| 작성일 | 2026-04-24 |
| 작성자 | David |
| 대상 시스템 | 간호사 근무표 백엔드 (FastAPI + CP-SAT + MSSQL) |
| 브랜치 | `feat/tg-integrate3` |
| 영향 범위 | DB 2컬럼 / API 1엔드포인트 확장 / CP-SAT 제약 2모듈 (재작성 1 + 신규 1) |

---

## 0. 문서 범위

본 문서는 팀(Team) 도메인에 도입되는 **2가지 팀별 설정 기능**을 통합 기술한다.

- **Part A** — `team.min_shift`: 팀별 일일 최소 시프트 커버리지 (v1.0, 2026-04-17)
- **Part B** — `team.handoff_policy`: 팀 내 인계(handoff) 제한 정책 (v2.0 신규, 2026-04-24)

두 기능은 독립적으로 활성/비활성 가능하며, `grade_strategy` 값에 따라 CP-SAT에 선택적으로 반영된다.

| 기능 | 활성 조건 (grade_strategy) |
| --- | --- |
| team.min_shift | `TEAM` 또는 `COMBINED` |
| team.handoff_policy | `COMBINED` |

---

# Part A — 팀별 일일 최소 시프트 커버리지 (`team.min_shift`)

## A.1 배경

기존에는 "팀 단위 일일 최소 시프트 커버리지"가 `roster_config.team_min_shift` 에 **단일 dict**로 저장되어, **모든 팀에 동일 기준**이 적용되었다.

```python
cfg.team_min_shift = {"D": 1, "E": 1, "N": 0}   # ← 전 팀 공통
```

그러나 현장 병동에서는 팀마다 **역할·규모·야간 가용 인력이 상이**하기 때문에 획일 기준이 실제 요구와 맞지 않는다.

> 예: 외과팀(야간 커버 필요) = `{D:2, E:1, N:1}`, 내과팀(외래 집중) = `{D:1, E:1, N:0}`

## A.2 목적

팀별로 **개별 min 시프트 커버리지**를 설정·저장·솔버 반영할 수 있도록 구조 전환한다.

### ✅ 성공 기준 (Definition of Done)

- 팀마다 서로 다른 `{D,E,N,M}` 최소값을 저장할 수 있다.
- CP-SAT 솔버가 팀별 최소값을 반영하여 스케줄을 생성한다.
- 기존 팀 레코드 (min_shift 미설정) 는 "제약 없음"으로 해석되어 회귀 없이 동작한다.

## A.3 범위

### IN-SCOPE

- `teams.min_shift` (JSON) 컬럼 추가
- `PUT /teams` API 에서 `min_shift` upsert 지원
- `GET /teams` 응답에 `min_shift` 포함
- `NurseRosterConfig` 필드 전환: `team_min_shift` → `team_min_by_team`
- CP-SAT 팀 제약 모듈을 **팀별 lookup 방식으로 전면 재작성**
- MSSQL 마이그레이션 스크립트 제공

### OUT-OF-SCOPE

- 팀별 **최대 시프트 (`team.max_shift`)**
- 프론트엔드 입력 UI
- precheck 자동 조립

## A.4 요구사항

### A.4.1 기능 요구사항

| ID | 요구사항 | 우선순위 |
| --- | --- | --- |
| FR-1 | `{D,E,N,M}` 최소값 저장 가능 | Must |
| FR-2 | 팀별 독립 적용 | Must |
| FR-3 | NULL → 제약 없음 | Must |
| FR-4 | use_mid=False 시 M 무시 | Must |
| FR-5 | soft/hard 선택 가능 | Must |
| FR-6 | 팀 삭제 시 같이 삭제 | Must |
| FR-7 | 필드 생략 시 기존값 유지 | Must |

### A.4.2 비기능 요구사항

| ID | 요구사항 |
| --- | --- |
| NFR-1 | API 역호환 유지 |
| NFR-2 | 무손실 마이그레이션 |
| NFR-3 | 무중단 반영 |

## A.5 설계

### A.5.1 DB 스키마

| 컬럼 | 타입 | Null | 설명 |
| --- | --- | --- | --- |
| office_id | VARCHAR(50) | NO | PK |
| group_id | VARCHAR(50) | NO | PK |
| team_id | INT | NO | PK |
| team_name | VARCHAR(100) | NO |  |
| active | TINYINT | NO |  |
| min_shift | NVARCHAR(MAX) | YES | JSON |
| created_at | DATETIME | NO |  |
| updated_at | DATETIME | NO |  |

```sql
ALTER TABLE teams ADD min_shift NVARCHAR(MAX) NULL;
```

### A.5.2 시맨틱 규약

| 값 | 의미 |
| --- | --- |
| NULL | 제약 없음 |
| {} | 제약 없음 |
| {"D":1} | D만 적용 |
| {"D":2,"E":1,"N":1,"M":0} | 복합 |

- 허용 키: D, E, N, M
- 허용 값: 0 이상의 정수

### A.5.3 Pydantic

```python
class TeamOps(BaseModel):
    team_id: Optional[int]
    team_name: Optional[str]
    add: List[str]
    remove: List[str]
    min_shift: Optional[Dict[str, int]]

class TeamWithMembers(BaseModel):
    team_id: int
    team_name: str
    team_members: List[str]
    min_shift: Optional[Dict[str, int]]
```

### A.5.4 Config 변경

```python
# BEFORE
team_min_shift: Dict[str, int]

# AFTER
team_min_by_team: Dict[str, Dict[str, int]]
team_min_soft_fallback: bool = True
team_min_penalty_weight: int = 500
```

### A.5.5 CP-SAT 로직

```
for tid in team_min_by_team:
    team_min = team_min_by_team[tid]
    for d in days:
        for code, min_t in team_min.items():
            ΣX >= min_t
```

### A.5.6 데이터 흐름

```
프론트 → PUT /teams
 → DB 저장
 → 스케줄 생성 요청
 → team_min_by_team 주입
 → CP-SAT 적용
 → 결과 생성
```

## A.6 API

### PUT /teams

```json
{
  "teams": [
    {
      "team_id": 1,
      "team_name": "외과팀",
      "min_shift": {"D": 2, "E": 1, "N": 1}
    }
  ]
}
```

### GET /teams

```json
[
  {
    "team_id": 1,
    "min_shift": {"D": 2, "E": 1, "N": 1}
  }
]
```

## A.7 Curl API Test

### 1. Team 확인

```bash
curl -s "http://localhost:8000/teams?group_id=101358f6de7b" \
  -H "Cookie: access_token=Bearer <JWT>" | jq .
```

응답:

```json
[
  {
    "team_id": 2,
    "team_name": "2",
    "team_members": ["177904", "262335", "322731", "418638"],
    "min_shift": null
  },
  {
    "team_id": 1,
    "team_name": "1",
    "team_members": ["177741", "260120", "429090", "439514"],
    "min_shift": null
  },
  {
    "team_id": 3,
    "team_name": "3",
    "team_members": ["262339", "338304", "373019", "428798"],
    "min_shift": null
  }
]
```

### 2. Team 설정 (신규 min_shift)

```bash
curl -s -X PUT "http://localhost:8000/teams?group_id=101358f6de7b" \
  -H "Cookie: access_token=Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "teams": [
      {"team_id": 1, "team_name": "1팀", "add": [], "remove": [], "min_shift": {"D": 1, "E": 1, "N": 0}},
      {"team_id": 2, "team_name": "2",   "add": [], "remove": [], "min_shift": {"D": 1, "E": 1, "N": 0}},
      {"team_id": 3, "team_name": "3",   "add": [], "remove": [], "min_shift": {"D": 1, "E": 1, "N": 0}}
    ],
    "delete_team_ids": []
  }' | jq .
```

### 3. Grade 설정 조회

```bash
curl -s "http://localhost:8000/grade/config?group_id=101358f6de7b" \
  -H "Cookie: access_token=Bearer <JWT>" | jq .
```

### 4. Grade 설정 저장 (EXCLUDE + constraints_max)

```bash
curl -s -X POST "http://localhost:8000/grade/config?group_id=101358f6de7b" \
  -H "Cookie: access_token=Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "null_grade_policy": "EXCLUDE",
    "use_dynamic_scaling": true,
    "constraints": {
      "D": {"1": 1},
      "E": {"1": 1}
    },
    "constraints_max": {
      "D": {"3": 1},
      "E": {"3": 1},
      "N": {"3": 1}
    },
    "grade_names": {"1": "시니어", "2": "중견", "3": "주니어"},
    "use_mid": false
  }' | jq .
```

### 5. 근무표 생성 (COMBINED)

```bash
curl -s -X POST "http://localhost:8000/roster_create/generate" \
  -H "Cookie: access_token=Bearer <JWT>" \
  -H "Content-Type: application/json" \
  --max-time 600 \
  -d '{
    "year": 2026,
    "month": 5,
    "grade_strategy": "COMBINED",
    "distribution_mode": "hybrid"
  }' | jq .
```

## A.8 변경 파일

| 파일 | 내용 |
| --- | --- |
| `app/db/models.py` | 컬럼 추가 |
| `app/schemas/team_schema.py` | 필드 추가 |
| `app/services/team_service.py` | 로직 추가 |
| `app/db/roster_config.py` | config 필드 전환 |
| `app/services/cp_sat_basic.py` | config 반영 |
| `app/services/roster_create_service.py` | 로더 연결 |
| `app/services/constraints/team_constraints.py` | 재작성 |

## A.9 페널티 가중치

| 값 | 의미 |
| --- | --- |
| 0 | 무효 |
| 100 | 약함 |
| 500 | 기본 |
| 1000+ | 강함 |

---

# Part B — 팀 내 인계 제한 정책 (`team.handoff_policy`)

## B.1 배경

같은 팀(=같은 병실 관리 단위) 안에서 **막내 라인 간호사들끼리 연속 교대 인계**(D→E, E→N, N→D익일)가 발생하면, **인계점에 감독자가 없어 안전 위험**이 발생한다. 또한 같은 팀·같은 시프트에 막내가 2명 이상 겹치는 것도 업무 분산상 부담이 된다.

이 제약은 **팀 축(team) × grade 축(grade) × 시간 축(temporal)** 이 동시에 걸리는 cross-제약이라, 기존 `team_constraints` / `grade_constraints` 어느 쪽에도 귀속될 수 없다 → **제3의 모듈로 분리**.

## B.2 목적

- 팀별로 **"인계 제한 대상 grade 집합"** 을 정의하고,
- 해당 집합에 속한 간호사들끼리 **인접 시프트 핸드오프**(당일 D→E, E→N / 익일 N→D)를 **금지**(soft),
- 선택적으로 **같은 시프트 동시 배치**도 **금지**(soft).

### ✅ 성공 기준 (DoD)

- 팀마다 서로 다른 `restrictions` 리스트를 저장할 수 있다.
- `grade_strategy == "COMBINED"` 일 때만 활성, 외 전략에서는 영향 없음.
- 규칙 없는 팀에는 제약이 걸리지 않는다.
- 미래 확장(`from`/`to` 방향성 규칙)을 **스키마 변경 없이** 흡수한다.

## B.3 범위

### IN-SCOPE

- `teams.handoff_policy` (JSON) 컬럼 추가
- `PUT /teams` 에서 `handoff_policy` upsert 지원 (3-상태: `None`/`{}`/`{...}`)
- `GET /teams` 응답에 `handoff_policy` 포함
- `NurseRosterConfig` 에 `team_handoff_policy_by_team`, `team_handoff_soft_fallback`, `team_handoff_penalty_weight` 추가
- 신규 제약 모듈 `app/services/constraints/team_grade_handoff_constraints.py`
- v1 대칭 규칙(`grades`) 완전 구현, v2 방향성 규칙(`from`/`to`, `bidirectional`) 수식/파서 구현
- MSSQL 마이그레이션 제공

### OUT-OF-SCOPE

- 프론트엔드 입력 UI
- "시간대 외 인계"(같은 팀 D-D 중복 등) — `block_same_shift` 로 해결 가능
- 전역(global) 기본 `junior_grades` (팀별 per-team 정책만 지원)

## B.4 요구사항

### B.4.1 기능 요구사항

| ID | 요구사항 | 우선순위 |
| --- | --- | --- |
| FR-1 | `restrictions[].grades` 리스트 저장 가능 | Must |
| FR-2 | 팀별 독립 적용 | Must |
| FR-3 | NULL/`{}` → 제약 없음 | Must |
| FR-4 | `block_adjacent` 옵션 지원 (D→E, E→N, N→D익일) | Must |
| FR-5 | `block_same_shift` 옵션 지원 | Must |
| FR-6 | soft fallback + penalty_weight | Must |
| FR-7 | `grade_strategy=COMBINED` 이외 전략에서는 무시 | Must |
| FR-8 | v2 확장(`from`/`to`, `bidirectional`) 파서/수식 동시 지원 | Should |
| FR-9 | 팀 삭제 시 정책도 같이 삭제 | Must |

### B.4.2 비기능 요구사항

| ID | 요구사항 |
| --- | --- |
| NFR-1 | API 역호환 유지 |
| NFR-2 | 무손실 마이그레이션 |
| NFR-3 | min_shift 모듈과 **sibling coupling 0** 유지 |
| NFR-4 | v1 → v2 확장 시 **스키마 변경 없음** |

## B.5 설계

### B.5.1 DB 스키마 (teams 테이블)

| 컬럼 | 타입 | Null | 설명 |
| --- | --- | --- | --- |
| handoff_policy | NVARCHAR(MAX) | YES | JSON. v1: `{"restrictions":[{"grades":[...], "block_adjacent":bool, "block_same_shift":bool}]}` |

```sql
ALTER TABLE teams ADD handoff_policy NVARCHAR(MAX) NULL;
```

### B.5.2 시맨틱 규약

| 값 | 의미 |
| --- | --- |
| NULL | 정책 없음 |
| `{}` | 정책 없음 (클리어) |
| `{"restrictions": []}` | 정책 없음 (클리어) |
| `{"restrictions": [{...}]}` | 규칙 1개 이상 |

**3-상태 입력 시그널** (PUT /teams):

| payload 값 | 해석 |
| --- | --- |
| `null` / 필드 생략 | 변경 없음 |
| `{}` | 클리어 (DB NULL 저장) |
| `{"restrictions":[...]}` | 정제 후 저장 (빈 규칙 배제) |

### B.5.3 규칙 스키마

#### v1 — 대칭(symmetric) 규칙 (현 구현)

```jsonc
{
  "grades": [6, 7, 8],        // 이 집합 안에서 상호 인계 금지
  "block_same_shift": true,   // 선택: 같은 시프트 동시 ≤ 1
  "block_adjacent": true      // D→E, E→N, N→D(익일) 금지 (기본 true)
}
```

#### v2 — 방향성(directional) 규칙 (파서/수식 구현 완료)

```jsonc
{
  "from": [7, 8],             // 출발 인계자 grade 집합
  "to": [5],                  // 도착 인계받는자 grade 집합
  "bidirectional": false,     // true 시 역방향도 자동 추가
  "block_adjacent": true
}
```

**파싱 규칙** (코드에서 구분):

```python
if "grades" in rule:
    lhs = rhs = rule["grades"]           # 대칭
elif "from" in rule and "to" in rule:
    lhs, rhs = rule["from"], rule["to"]  # 방향성
    if rule.get("bidirectional"):
        # (rhs, lhs) 페어도 추가
```

### B.5.4 Pydantic

```python
class TeamOps(BaseModel):
    team_id: Optional[int]
    team_name: Optional[str]
    add: List[str]
    remove: List[str]
    min_shift: Optional[Dict[str, int]]
    handoff_policy: Optional[Dict[str, Any]]  # 신규

class TeamWithMembers(BaseModel):
    team_id: int
    team_name: str
    team_members: List[str]
    min_shift: Optional[Dict[str, int]]
    handoff_policy: Optional[Dict[str, Any]]  # 신규
```

### B.5.5 Config 필드

```python
# NurseRosterConfig
team_handoff_policy_by_team: Dict[str, Dict] = field(default_factory=dict)
team_handoff_soft_fallback: bool = True
team_handoff_penalty_weight: int = 80000  # grade(160000)와 team_min(500) 사이
```

### B.5.6 CP-SAT 수식

각 팀 `t`, 각 일 `d`, 각 규칙 `r`, 각 (lhs, rhs) 페어에 대해:

```
L_t = { n : n.team_id == t AND n.grade ∈ lhs_grades }
R_t = { n : n.team_id == t AND n.grade ∈ rhs_grades }

# aux 바이너리 (OR 링크)
b_L[t,d,s] >= X[n,d,s]   for all n in L_t
b_R[t,d,s] >= X[n,d,s]   for all n in R_t

# block_adjacent
b_L[t,d,D] + b_R[t,d,E]   <= 1     # 당일 D→E
b_L[t,d,E] + b_R[t,d,N]   <= 1     # 당일 E→N
b_L[t,d,N] + b_R[t,d+1,D] <= 1     # 익일 N→D

# block_same_shift (L == R 인 경우 의미)
sum(X[n,d,s] for n in L_t) <= 1

# soft fallback: 각 제약에 slack, penalty_weight * slack 감산
```

### B.5.7 데이터 흐름

```
프론트 → PUT /teams (handoff_policy 포함)
 → team_service._coerce_handoff_policy 로 정제
 → DB 저장
 → 스케줄 생성 요청 (grade_strategy="COMBINED")
 → roster_create_service 가 teams.handoff_policy 를 config_dict["team_handoff_policy_by_team"] 로 주입
 → cp_sat_basic 이 NurseRosterConfig 에 세팅
 → objective_terms.py / fallback_objectives.py 에서 add_team_grade_handoff_constraints 호출
 → CP-SAT 모델에 soft slack + penalty 추가
 → 결과 생성
```

### B.5.8 모듈 구조 (결합도 분석)

- `team_constraints.py` / `grade_constraints.py` / `team_grade_handoff_constraints.py` — 세 파일이 **서로 import 없음**
- 공유는 도메인 모델(`rs.nurses[i].team_id`, `rs.nurses[i].grade`, `cfg.*`)뿐
- **sibling coupling = 0**
- 활성 조건·페널티 가중치·입력 스키마가 모두 달라 합치면 분기만 증가

## B.6 API

### PUT /teams

```jsonc
{
  "teams": [
    {
      "team_id": 2,
      "handoff_policy": {
        "restrictions": [
          {
            "grades": [6, 7, 8],
            "block_same_shift": false,
            "block_adjacent": true
          }
        ]
      }
    }
  ]
}
```

### GET /teams

```jsonc
[
  {
    "team_id": 2,
    "handoff_policy": {
      "restrictions": [
        {"grades": [6, 7, 8], "block_same_shift": false, "block_adjacent": true}
      ]
    }
  }
]
```

### 정책 클리어

```json
{"teams": [{"team_id": 2, "handoff_policy": {}}]}
```

## B.7 Curl API Test — 팀 내 handoff 조정

### 1. handoff_policy 설정 (팀2: Grade 2 상호 인계 금지)

```bash
curl -X PUT "http://localhost:8000/teams" \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=Bearer <JWT>" \
  -d '{
    "teams": [
      {
        "team_id": 2,
        "handoff_policy": {
          "restrictions": [
            {
              "grades": [2],
              "block_same_shift": false,
              "block_adjacent": true
            }
          ]
        }
      }
    ]
  }'
```

### 2. GET /teams 로 반영 확인

```bash
curl -s "http://localhost:8000/teams?group_id=101358f6de7b" \
  -H "Cookie: access_token=Bearer <JWT>" | jq .
```

→ `team_id=2` 에 `handoff_policy.restrictions` 배열이 나오면 DB 왕복 OK.

### 3. 근무표 생성 (COMBINED)

```bash
curl -s -X POST "http://localhost:8000/roster_create/generate" \
  -H "Cookie: access_token=Bearer <JWT>" \
  -H "Content-Type: application/json" \
  --max-time 600 \
  -d '{
    "year": 2026,
    "month": 5,
    "grade_strategy": "COMBINED",
    "distribution_mode": "hybrid"
  }' | jq .
```

### 4. 변형 예시

**같은 시프트 동시 금지까지 적용**:

```jsonc
"handoff_policy": {
  "restrictions": [
    {"grades": [2, 3], "block_same_shift": true, "block_adjacent": true}
  ]
}
```

**v2 방향성 규칙 (미래 UI 확장용 샘플)**:

```jsonc
"handoff_policy": {
  "restrictions": [
    {"grades": [6, 7, 8]},
    {"from": [7, 8], "to": [5], "block_adjacent": true}
  ]
}
```

## B.8 변경 파일

| 파일 | 내용 |
| --- | --- |
| `app/db/models.py` | `Team.handoff_policy` JSON 컬럼 |
| `app/db/roster_config.py` | `team_handoff_policy_by_team`, `team_handoff_soft_fallback`, `team_handoff_penalty_weight` |
| `app/schemas/team_schema.py` | `TeamOps.handoff_policy`, `TeamWithMembers.handoff_policy` |
| `app/services/team_service.py` | `_coerce_handoff_policy` + upsert 3-상태 처리 |
| `app/services/roster_create_service.py` | `teams.handoff_policy` → `config_dict["team_handoff_policy_by_team"]` |
| `app/services/cp_sat_basic.py` | `NurseRosterConfig` 로 전달 |
| `app/services/constraints/team_grade_handoff_constraints.py` | **신규 모듈** (대칭 + 방향성 통합 lhs/rhs 추상화) |
| `app/services/cp_sat/objective_terms.py` | `COMBINED` 에서 호출 연결 |
| `app/services/cp_sat/fallback_objectives.py` | `COMBINED` 에서 호출 연결 |

## B.9 페널티 가중치 계층

```
grade_penalty_weight      (160000)  ← grade 분배
team_handoff_penalty      ( 80000)  ← 신규
team_min_penalty_weight   (   500)  ← 팀 커버리지
```

중간 가중치 — grade 분배가 더 중요하지만, handoff 위반은 팀 커버리지보다 훨씬 무겁게 억제.

## B.10 테스트

### 단위 테스트

- `_coerce_handoff_policy`: `None`/`{}`/빈 규칙/유효 규칙/유효하지 않은 규칙 케이스 ✓
- `_extract_lhs_rhs`: 대칭/방향성/양방향 ✓
- `NurseRosterConfig` 신규 필드 수용 ✓
- `TeamOps` 스키마 검증 ✓

### 통합 테스트

- PUT → DB → GET round-trip (`handoff_policy` 보존)
- grade_strategy=BASE/TEAM/GRADE 에서는 제약 비활성 (회귀 없음)
- grade_strategy=COMBINED 에서 제약 활성

### 솔버 테스트

- 지정된 grade 집합 간호사들이 당일 D→E, E→N, N→D(익일) 인접 인계에 함께 배치되지 않음
- `block_same_shift=true` 시 같은 팀·같은 시프트에 해당 grade 2명 이상 겹치지 않음
- 규칙 비대칭(`from`≠`to`) 시 방향성대로만 제약 걸림

## B.11 영향도

| 항목 | 영향 |
| --- | --- |
| DB | 컬럼 1개 추가 (teams.handoff_policy) |
| 성능 | 팀당 일수 × 규칙 수 만큼 aux 바이너리 추가 (소폭 증가) |
| 안정성 | soft fallback으로 infeasible 방지, 기본 비활성 |

## B.12 롤아웃

1. PR merge
2. DB migration (`ALTER TABLE teams ADD handoff_policy NVARCHAR(MAX) NULL;`)
3. 배포
4. 검증 — grade_strategy=BASE 에서 회귀 없음 확인
5. stage → prod

## B.13 Open Questions

- 프론트엔드 UI 디자인 (팀별 handoff 정책 입력 폼)
- 전역 fallback `default_junior_grades` 필요성 (현재는 per-team 강제)
- "시간 외 인계"(예: OFF→D 전일 OFF 후 D 인수) 요구가 생기면 별도 규칙 키 필요

---

# 15. 참고 문서

- `docs/TEAM_GRADE_INFEASIBILITY_PRECHECK.md`
- `docs/FRONTEND_PRECHECK_INTEGRATION.md`

---

# 16. 변경 이력

| 버전 | 날짜 | 작성자 | 변경 |
| --- | --- | --- | --- |
| v1.0 | 2026-04-17 | David | 초안 — `team.min_shift` 도입 |
| v2.0 | 2026-04-24 | David | `team.handoff_policy` 신규 추가 (Part B). 단일 팀별 설정 통합 문서로 확장 |
