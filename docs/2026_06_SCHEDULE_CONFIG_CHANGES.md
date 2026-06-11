# 2026-06 근무표 생성 설정 변경 내역

> 작성: 2026-05-23
> 대상: 9B(병동, group=10135890c287), ICU(중환자실, group=10135857f9f9)
> 목적: 6월 근무표 생성 시 발생한 INFEASIBLE / 품질 저하를 해결하기 위한 정책·파라미터 조정 1차

본 문서는 6월 근무표 생성에 한해 수동/일회성으로 적용한 변경을 기록한다. 후속 단계에서 일부는 **글로벌 디폴트로 승격**, 일부는 **UI 노출 옵션으로 외부화**할 예정이다(섹션 4 참조).

---

## 1. 코드 변경

| 파일·라인 | 항목 | 변경 전 → 후 | 적용 범위 |
|---|---|---|---|
| `app/db/roster_config.py:144` | `NurseRosterConfig.team_min_soft_fallback` (dataclass 기본값) | `False` → `True` | **전역**(모든 그룹의 team_min 디폴트가 soft로 동작) |

### 동작 영향
- `team_min_by_team`이 설정된 그룹(9B 등)에서 팀별 일일 최소(D≥1, E≥1 등)가 hard → soft 슬랙으로 전환됨.
- 슬랙 패널티: `team_min_penalty_weight = 500` (`roster_config.py:145`, 미변경)
- ICU는 `team_min_by_team`이 비어있으므로(`[TeamMin] skip: cfg.team_min_by_team is empty`) 본 변경의 직접 영향은 9B 계열 그룹에 한정.

---

## 2. DB 데이터 변경 (MSSQL prod 적용 완료)

### 2-1. Grade 최소 인원: hard 전환

| 그룹 | 테이블 | 컬럼 | 변경 |
|---|---|---|---|
| 9B (10135890c287) | `roster_grade_config` (config_id=16) | `allow_soft_fallback` | `1 → 0` |
| ICU (10135857f9f9) | `roster_grade_config` (config_id=10) | `allow_soft_fallback` | `1 → 0` |

- Hard 의도: Grade D/E/N 최소(예: `{D:{1:1,2:2}, E:{1:1,2:2}, N:{1:1,2:2}}`)를 엄격 충족.
- 단, 엔진에는 **`roster_create_service.py:5238`의 AUTO-SOFT 재시도 경로**가 살아있다. `NO_ASSIGNMENT` / `MAX_CAP_SHORTAGE` / `GRADE_MAX_SUM_BELOW_NEED` 검출 시 1회 자동 soft 재시도. ICU 6월은 이 경로로 풀렸음(grade_min 위반 50건, 달성 51.7%, HTTP 200 + `applied_relaxations=['grade_hard_to_soft']`).

### 2-2. 야간 월 상한: ICU 6·7월

| 그룹 | 테이블 | 변경 |
|---|---|---|
| ICU (10135857f9f9) | `nurse_monthly_limits` | 활성 27명(정아영 271772 제외) × 2026-06/07 → `n_max=7` 일괄 upsert (INSERT 53, UPDATE 1) |

- 운영팀 요청에 따른 야간 부담 상한.

---

## 3. 변경하지 않은 항목 (확인만 수행)

- `RosterConfig.team_min_by_team` (DB 컬럼이 아니라 `teams.min_shift` → 런타임 주입)
- `RosterGradeConfig.use_dynamic_scaling = 1` (Grade 목표 동적 상향 활성)
- OFF 균등화 가중치(`cp_sat_basic.py:2957~`의 `_off_eq_weight = -200`) — `off_first=False` 경로에서 약함. 후속 작업 대상(섹션 4-B).
- `extra_off_penalty_weight = 80`

---

## 4. 후속 작업 가이드 — 디폴트 승격 vs UI 옵션화

> 본 1차 변경은 임시조치 성격. 다음 항목을 단계적으로 정리한다.

### 4-A. 디폴트로 승격 검토

| 항목 | 현재 | 권장 방향 | 근거 |
|---|---|---|---|
| `team_min_soft_fallback` | 이번에 전역 디폴트 True | **유지(전역 True)** | 9B 표유진 케이스처럼 인력 결손 시 hard로는 풀리지 않음. soft + 패널티가 안전 |
| Grade `allow_soft_fallback` | 그룹별 DB 컬럼 (9B/ICU만 0) | **그룹별 유지**, 단 기본은 1(soft) | hard는 운영팀이 명시적으로 원할 때만. 자동 fallback 경로(`roster_create_service.py:5238`)와 정책 일관성 정리 필요 |
| `use_dynamic_scaling` | 1(활성) | 검토 후 결정 | hard 모드에서 동적 상향이 INFEASIBLE을 키우는지 확인 필요 |

### 4-B. UI 옵션화 (그룹/사용자 설정으로 외부화)

| 항목 | 현재 위치 | 외부화 방향 |
|---|---|---|
| `team_min_soft_fallback` | 코드 디폴트만 존재 (DB 컬럼 없음) | `RosterConfig`에 컬럼 추가 → UI에서 그룹별 토글 |
| `team_min_penalty_weight` (500) | 코드 상수 | 그룹별 가중치 슬라이더 |
| `extra_off_penalty_weight` (80) | `config_dict` (이미 주입 가능) | UI에서 게이지 노출 |
| OFF 균등 강도 (`_off_eq_weight`) | 코드 상수(`-200`/`-100000`) | "OFF 균등 강도" 게이지로 통합 (off_first 분기 제거) |
| `n_max` (월별 야간 상한) | `nurse_monthly_limits` 컬럼 | 이미 DB 컬럼 — **UI 일괄 설정 도구 필요** (이번에 일회성 스크립트로 처리) |
| Grade hard/soft AUTO 재시도 | `roster_create_service.py:5238` 하드코드 | 그룹별 옵션(`auto_grade_soft_retry_enabled`)으로 노출 |

### 4-C. 구조적(데이터) 후속 과제

| 그룹 | 관찰 | 후속 |
|---|---|---|
| 9B | 표유진(390658) 5/26 영구 병동이동(target=101358f6de7b) → 9B에서 사용 불가, C팀 D/E 공급 부족 | `active=0` 처리 또는 인력 재배치 |
| ICU | g2 8명 vs 일일 D2+E2+N2=6명 요구 → 30일 수요 180명-day vs 최대 공급 168명-day(off=9 기준) → 산술 부족 | g2 일일 요구 하향(2→1) 또는 g3 일부 승급 검토 |

---

## 5. 검증 결과 요약 (2026-05-23)

| 그룹 | 월 | 1차(hard) 결과 | AUTO-SOFT 후 | 응답 |
|---|---|---|---|---|
| 9B | 6월 | INFEASIBLE (표유진 결손 + team_min 영향, 본 변경 직전 상태) | — | HTTP 500 `NO_ASSIGNMENT` |
| ICU | 6월 | 폴백 0~9 전 단계 INFEASIBLE | OPTIMAL (`relax_level=0`) | HTTP 200 + `applied_relaxations=['grade_hard_to_soft']`, grade_min 위반 50건 |

본 변경 적용 이후 9B 재실행은 아직 수행하지 않음. 다음 단계에서 표유진 처리와 함께 재검증 예정.

---

## 6. `max_same_shift` 소프트 항 추가 (2026-05-23, 후속)

### 6-1. 의도
같은 시프트(D/E/N)가 4일 이상 연속되는 패턴을 억제. 예: `D D D D` 발생 시 페널티.

### 6-2. 변경 파일

| 파일 | 변경 |
|---|---|
| `app/db/roster_config.py` | `NurseRosterConfig`에 `max_same_shift: bool = True`, `max_same_shift_penalty_weight: int = 10000` 추가 |
| `app/services/cp_sat_basic.py` | `config_data → cfg` 주입 추가 (소비 측 cfg 노출) |
| `app/services/cp_sat/objective_terms.py` | 메인 objective에 4일 윈도우 위반 soft 항 추가 (D/E/N 각각) |
| `app/services/cp_sat/fallback_objectives.py` | 폴백 objective에도 동일 항 추가 |

### 6-3. 수식
각 간호사 n, 시프트 s ∈ {D,E,N}, 시작일 d0에 대해:
```
sum_s(n, d0, s) = X(n, d0, s) + X(n, d0+1, s) + X(n, d0+2, s) + X(n, d0+3, s)
viol(n, d0, s) >= sum_s - 3        # 1 iff 4연속
objective += -w_ms × viol
```
- 4연속(`sum_s=4`) 발생 시 `viol=1`, 그 외 `viol=0`.
- 5연속(`DDDDD`)은 윈도우 2개에서 동시에 `viol=1` → 2×패널티(자연스러운 길이 비례 비용).

### 6-4. ICU 6월 가중치 튜닝 결과

| `max_same_shift_penalty_weight` | 4연속 발생 간호사 / 28 |
|---|---|
| 300 (초기) | 6 |
| 5000 | 5 |
| **10000 (채택)** | **2** |

- 채택값 10000은 KLD per-shift 가중치(90,000~1,200,000) 대비 약하지만, 대다수 케이스에서 4연속을 회피하기에 충분. 강제 hard로 만들 경우 grade/team_min과 충돌해 INFEASIBLE 위험.
- 남는 2케이스는 다른 hard 제약(grade 충족, KLD 균등 등)과의 트레이드오프에서 페널티를 감수한 결과로 해석.

### 6-5. 후속 옵션 (섹션 4-B 연장)
- UI 노출: 그룹별 토글(`max_same_shift`) + 가중치 슬라이더(`max_same_shift_penalty_weight`)로 외부화 후보.
- 가중치 30,000~90,000 모드(`B 옵션`)는 4연속 회피가 운영상 엄격히 요구되는 그룹에 한해 케이스별 적용.

---

## 7. AUTO-SOFT 재시도 정책 변경: grade hard 고정, team_min hard→soft 폴백 (2026-05-23)

### 7-1. 변경 전
- `roster_create_service.py:5238` 의 AUTO-SOFT 경로가 `NO_ASSIGNMENT`/`MAX_CAP_SHORTAGE`/`GRADE_MAX_SUM_BELOW_NEED` 검출 시 **grade hard → soft**로 강제 전환하고 1회 재시도.
- ICU 6월에서 이 경로로 자동 풀려 grade-1 51.7% 달성 + `applied_relaxations=['grade_hard_to_soft']` warning 반환.

### 7-2. 변경 후
- 같은 검출 신호에 대해 이제는 **team_min hard → soft** 1회 재시도로 전환. grade는 끝까지 hard 유지.
- `applied_relaxations=['team_min_hard_to_soft', 'treatment:soft:team_min']`, severity=warning, HTTP 200.
  - `team_min_hard_to_soft`: legacy 호환 라벨.
  - `treatment:soft:team_min`: ontology treatment 어휘 (agent-qa-harness 결합 호환).
- 재시도도 실패하면 HTTP 500 (`UNRECOVERABLE`).

### 7-3. 영향
- **운영 의미**: Grade(자격) 미달은 운영팀이 명시적으로 손볼 때까지 풀지 않고 INFEASIBLE로 노출. Team(D/E 분담)은 인력 결손 등 불가피한 케이스에서 자동 완화.
- **이전 grade AUTO-SOFT에 의존하던 ICU 케이스**: g2 demand가 supply보다 큰 경우 hard로 즉시 INFEASIBLE → 운영팀이 g2 일일요구를 낮추거나 인력 재배치해야 함. (현재 ICU는 6월 g2 demand를 `{D:1, E:1, N:1}`로 이미 낮춤 → 정상 풀림)

### 7-4. 코드 변경 파일

| 파일 | 변경 |
|---|---|
| `app/services/roster_create_service.py` (≈5237) | AUTO-SOFT 블록을 grade flip → team_min flip으로 교체. 가드 플래그를 `_team_min_soft_retry_attempted`로 변경 |
| `app/services/roster_create_service.py` (≈2877) | snapshot `attempt_meta.label`에 `team_min_retry` 분기 추가 |
| `app/services/constraint_impact/types.py` | `SolveAttemptLabel`에 `"team_min_retry"` 리터럴 추가 |

### 7-5. 검증 (회귀)
- 9B 2026-06: 1차 hard에서 OPTIMAL `relax_level=0`, retry 미작동, HTTP 200 (정상)
- ICU 2026-06: 1차 hard에서 OPTIMAL `relax_level=0`, retry 미작동, HTTP 200 (정상)

### 7-6. 남은 dead code (후속 청소 대상)
`_force_grade_max_soft_fallback` 플래그 consumer 4곳(라인 2818-2821, 2877-2882 일부)은 그대로 두었음. 외부에서 이 플래그를 주입하는 경로가 없으므로 사실상 dead. 후속 cleanup에서 제거 가능.

---

## 8. 4O hard 제약 해제 (디폴트 False) (2026-05-23)

### 8-1. 의도
"순수 OFF 4연속 금지" hard 제약(당월 내 + 월경계)을 디폴트로 해제. 운영 측에서 4O 자체를 막을 필요가 약하다고 판단.

### 8-2. 변경 파일

| 파일 | 변경 |
|---|---|
| `app/db/roster_config.py` | `enforce_4o_hard: bool = False` 추가 (디폴트 해제) |
| `app/services/cp_sat_basic.py` | config 주입 + 4O hard 블록 2곳(`:2557~`, `:2604~`)에 게이트. 환경변수 `ROSTER_DISABLE_4O_HARD` escape hatch도 함께 |
| `app/services/cp_sat/fallback_lex.py` | 폴백 경로의 4O hard 블록 2곳(`:761~`, `:798~`)에도 동일 게이트 |

### 8-3. 검증 결과 (`ROSTER_DISABLE_4O_HARD=1`로 강제 해제, 2026-06)

| 그룹 | 활성 인원 | 4O 발생 | 비율 | 최대 OFF run 길이 |
|---|---|---|---|---|
| 9B | 15 | 2명 | 13% | 4일 (5+ 없음) |
| ICU | 28 | 3명 | 11% | 4일 (5+ 없음) |

→ 솔버가 자발적으로 4O를 만들지는 않고, 다른 제약(OFF cap, 월 OFF 총량, KLD 분배 등)이 ~10-13% 수준으로 자연 억제.

### 8-4. 재활성화 방법
운영 요구로 다시 hard로 돌리려면:
- 그룹별: 후속 작업에서 RosterConfig DB에 `enforce_4o_hard` 컬럼 추가 후 그룹별 True로
- 일회성 테스트: 환경변수 `ROSTER_DISABLE_4O_HARD=0` 또는 미설정 + cfg default True로 변경
- 코드 디폴트: `roster_config.py:148`을 다시 `True`로

---

## 9. N 블록 종료 → 다음 N 블록 시작 간격 soft (2026-05-23)

### 9-1. 의도
간호사별 "야간 블록 종료 후 다음 야간 블록 시작까지의 간격"을 target=10일로 유도. 너무 짧으면(연속 야간 부담) 페널티, 너무 길면(야간 분포 편향) 페널티. **대칭 페널티**.

### 9-2. 새 cfg 필드 (`roster_config.py`)

| 필드 | 디폴트 | 설명 |
|---|---|---|
| `n_to_n_interval_target` | 10 | 목표 gap(일) |
| `n_to_n_interval_penalty_weight` | 50 | 거리당 페널티 가중치 (낮게) |
| `n_to_n_interval_max_window` | 15 | pair 모델링에 고려할 최대 gap. 이보다 멀면 무시 |

### 9-3. "N 블록"의 정의

**N 블록** = 한 간호사의 스케줄에서 `X(n,d,N)=1`이 **연속**된 day들의 **최대** 묶음. 앞·뒤로 non-N day가 있거나(또는 활동 범위 경계) 끊긴다.

예시 (한 명의 30일 스케줄):
```
day:  1 2 3 4 5 6 7 8 9 10 11 12 13 14 ... 22 23 24
shift:N N N O O O E O O O  O  N  N  O  ... O  N  O
                                                    
       └─block1─┘                  └─b2─┘    └─b3─┘
```
- 블록1: days 1-3 (size 3) — block_end = day 3
- 블록2: days 12-13 (size 2) — block_start = day 12, block_end = day 13
- 블록3: day 23 (size 1, 단일 N도 블록) — block_start = block_end = day 23

### 9-4. "블록 종료 → 다음 블록 시작 간격"의 수식 모델링

각 간호사 n, day 쌍 (d1, d2)에 대해 **d1+2 ≤ d2 ≤ d1+max_window** 범위에서:
```
pair(n,d1,d2) = X(n,d1,N) ∧ X(n,d2,N) ∧ Π_{k ∈ (d1+1..d2-1)} ¬X(n,k,N)
gap            = d2 - d1
```
즉 `d1=N`, `d2=N`, 그 사이 모든 날이 non-N. 이 세 조건이 동시에 만족될 때만 `pair=1`.

**왜 자동으로 "블록 종료 → 다음 블록 시작"이 잡히는가**
- 사이가 non-N이려면 `X(d1+1, N) = 0` → 자동으로 d1은 자기 블록의 **마지막 N day** (블록 종료)
- 사이가 non-N이려면 `X(d2-1, N) = 0` → 자동으로 d2는 다음 블록의 **첫 N day** (블록 시작)
- 따라서 별도의 block_end/block_start 인디케이터 변수 없이 단일 boolean pair로 표현

**왜 `d2 ≥ d1+2`인가** (`range(d1+2, …)`로 강제)
- `d2 = d1+1`이면 두 N day가 연속(같은 블록 내부) → pair는 의미 없음. 이 경우 제외.
- `d2 = d1+2`이면 두 N day 사이에 1일짜리 non-N(=gap 2) → 별개 블록으로 인정.

**Edge cases**
- **단일 N day 블록** (예: ...O N O...): 그 N day가 block_end이자 block_start. 전 블록 → 이 N(d2)으로의 pair 1개 + 이 N(d1) → 다음 블록으로의 pair 1개, 두 개 모두 잡힘.
- **N으로 끝나는 마지막 블록** (이후 다음 블록 없음): 해당 블록 다음 pair는 없음(d2가 없으니 자연 제외).
- **월경계**: 미반영(전월 마지막 N → 당월 첫 N 간격은 별도 작업 후보 — section 9-7 참조).

**gap 해석 예시**
| 패턴 (d1..d2) | d2-d1 = gap | target=10 페널티 |
|---|---|---|
| `N N` (인접) | 1 | (제외, 같은 블록) |
| `N O N` | 2 | \|2-10\|=8 → heavy |
| `N O O O O O O O O O N` (9일 휴식 후) | 10 | 0 (목표 적중) |
| `N O O ... O N` (12일 휴식 후) | 13 | \|13-10\|=3 |

### 9-5. 페널티

```
obj += -weight × |gap - target| × pair
```
- `gap = target`일 때 `pair=1`이어도 dist=0이므로 항 생략 (코드에서 `if dist == 0: continue`).
- `pair=0`이면 항 전체 0.
- 양쪽 방향 모두 선형 페널티 → **대칭**.

### 9-6. 집계 스크립트 동등성

검증 스크립트(`/tmp/...` 인라인)는 결과 배열에 state machine을 돌려 block_start/block_end를 명시적으로 잡지만, 도출되는 gap 정의(`block_start[i+1] - block_end[i]`)는 위 수식의 `d2 - d1`과 **수치적으로 동일**.

### 9-7. 모델링 비용
약 N × D × win = 28 × 30 × 15 = 12,600 pair 변수, 각 ~10개 제약 → 약 130K 추가 제약. 실측에서 solve 시간 영향 없음(OPTIMAL relax_level=0 유지).

### 9-8. 검증 결과 (2026-06)

| 그룹 | pair 수 | 평균 gap | target±1 (9~11일) 비율 |
|---|---|---|---|
| 9B | 13 | 9.31일 | 31% (엄애란 N-only가 평균 끌어내림) |
| ICU | 44 | 9.14일 | **39% (gap=10에 8개 peak)** |

낮은 weight(50)로도 ICU 분포에서 target=10 주변 명확한 모달리티 형성. 짧은 gap(4-5일)은 월 N 상한(`n_max=7`) 제약 + 야간 몰아넣기로 구조적으로 발생.

### 9-9. 후속 옵션
- weight를 100~200 수준으로 올리면 더 강하게 target에 수렴(다른 soft와 트레이드오프 발생 가능).
- 월경계 N 간격(전월 마지막 N 블록 → 당월 첫 N 블록) 미반영 — 후속 작업에서 cross-month 항 추가 검토. → **§13-4/5에서 양 엔진 시제품+실측: 소규모·빠듯한 N 그룹(`101358f4ef48`)은 N 커버리지 제약으로 경계 간격이 안 밀려 구조적 무효 → 코드 미적용(폐기). weight는 fallback(lex minimize)엔 무관. 대규모/N여유 그룹 재검토는 향후 과제.**

---

## 10. 팀 자동 분배 알고리즘 (`team_auto_assign`) (2026-05-24)

### 10-1. 의도
원티드(OFF/연차) 데이터와 grade 정보로 nurses pool을 팀으로 자동 분배. 운영팀이 매월 손으로 짜던 작업을 자동화 — 기본 룰만 만족(겹침 없음/grade 균등/G1 ≥1/쏠림 X), 비공식 페어링은 운영팀이 결과에 미세 조정.

### 10-2. 신규 모듈

| 파일 | 역할 |
|---|---|
| `app/services/team_auto_assign.py` | 핵심 알고리즘 (외부 의존성 0, 순수 Python) |

핵심 자료구조:
```python
@dataclass
class NurseInput:
    nurse_id: str
    grade: int | None             # 1=preceptor 시드, 2=일반, 3=preceptee 등
    preceptor_id: str | None      # preceptee면 preceptor nurse_id
    off_days: frozenset[int]      # wanted OFF 일자
    fb_days: frozenset[int]       # wanted FB(연차) 일자

@dataclass
class TeamAssignResult:
    teams: dict[int, list[str]]   # team_idx → [nurse_id]
    objective: float
    overlap_total: int
    grade_dev_total: float
    team_size, team_grade_breakdown
```

진입점:
```python
auto_assign_teams(nurses, num_teams=3, seed_ids=None,
                  w_overlap=100, w_grade=200.0,
                  min_size=4, max_size=6, swap_iterations=100)
```

### 10-3. Hard 제약 (자체 검증 통과 필수)
| ID | 내용 |
|---|---|
| C1 | 각 팀에 grade-1 ≥ 1 (시드 자동 선정 또는 외부 입력) |
| C2 | 팀 크기 ∈ [min_size, max_size] (디폴트 [4, 6]) |
| C3 | preceptee → preceptor 같은 팀 (preceptor 시드 여부 무관) — `_enforce_preceptee_follow`로 강제 |
| C4 | N전담 등 사전 격리 인원은 입력 풀에서 제외 (호출자 책임) |

### 10-4. Soft Objective (minimize, 가중치 튜닝 가능)
| 항 | 식 | 디폴트 weight |
|---|---|---|
| OFF/FB pairwise overlap | `Σ_{a<b ∈ team_t} |OFF(a)∩OFF(b)| + |FB(a)∩FB(b)|` | 100 |
| Grade L1 deviation | `Σ_{t,g} \|count_g[t] - total_g/num_teams\|` | 200 |

### 10-5. 알고리즘 (2단계)

**Stage 1 — Greedy seed-and-grow**
- 시드(G1 K명) → 고정 팀
- preceptee → preceptor와 같은 팀 자동 배정
- 잔여 인원을 `grade desc → OFF 일수 desc` 순으로 정렬, 각 후보를 모든 팀에 시뮬레이션해 objective 가장 낮은 팀으로 추가

**Stage 2 — Local 2-opt swap**
- movable 정의: 시드/preceptee/preceptor-of-anyone 모두 제외 (페어 보존 위해)
- 각 (팀a, 팀b) 페어, 각 movable nurse 페어를 swap해보고 objective 감소 시 반영
- max_iterations 또는 no improvement 시 종료

**복잡도**: 15명/3팀 기준 < 100ms. CP-SAT 같은 무거운 솔버 불필요.

### 10-6. 검증 (회귀 분석 3단계 적용)

대상: 9A/9B × 2026-04/05 = 4 케이스. `fixed_wanted_entries` 132건 사용 (`/tmp/fixed_wanted_register.py` 적용 후).

**1. 파라미터 확인**: DB nurses + fixed_wanted 정상 로드, grade fallback (None→G2), N전담 외부 제외 (4월 9A: 이윤지, 9B: 엄애란 / 5월 9A: 박춘일, 9B: 박지연)

**2. 조건 적용**: 모든 4 케이스에서 hard 제약 100% 충족 (G1 ≥1/팀, 크기 ∈ [4,6], preceptee follow)

**3. 결과 품질**:

| 케이스 | 팀 크기 | OFF/FB overlap | Grade dev | 팀별 grade |
|---|---|---|---|---|
| 9A 4월 | 5/4/4 | **0** | 1.33 | G1=[1,1,1] G2=[4,3,3] |
| 9A 5월 | 5/4/4 | **0** | 1.33 | G1=[1,1,1] G2=[4,3,3] |
| 9B 4월 | 6/4/4 | **1** | 3.33 | G1=[2,1,1] G2=[3,2,2] G3=[1,1,1] |
| 9B 5월 | 6/4/4 | **1** | 3.33 | G1=[2,1,2] G2=[3,2,1] G3=[1,1,1] |

(9B 6명 팀은 preceptee follow hard로 인한 자연 확장)

### 10-7. 정답지 매칭률 한계 (데이터 sparsity)

운영팀 4월/5월 9A/9B 정답지와 비교 (Hungarian best matching):

| 케이스 | 매칭률 |
|---|---|
| 9A 4월 | 53.8% (7/13) |
| 9A 5월 | 57.1% (8/14) |
| 9B 4월 | 50.0% (7/14) |
| 9B 5월 | 42.9% (6/14) |
| **평균** | **~51%** |

**원인은 알고리즘이 아니라 OFF 데이터 sparsity**:

| 케이스 | OFF 신청 인원 | 겹치는 pair 비율 |
|---|---|---|
| 9A 4월 | 54% (7/13) | 5% (4/78) |
| 9A 5월 | 46% (6/13) | 4% (3/78) |
| 9B 4월 | 64% (9/14) | 13% (12/91) |
| 9B 5월 | 71% (10/14) | 10% (9/91) |

- 30~54% 인원이 OFF 0건 → 알고리즘이 그들 분배는 임의 결정 (변별 신호 없음)
- 겹치는 pair가 전체의 4~13% 뿐 → "안 겹치게" 제약 만족 자유도 너무 큼
- 운영팀의 비공식 페어링 (성격/조합/연차) — 데이터 외 신호, 알고리즘 불가

### 10-8. 운영 모드 제안
1. **자동 추천 + 운영팀 미세 조정** (현실 권장) — 알고리즘이 기본 룰 만족 분배 제안, 운영팀이 검토·페어 조정
2. **시드 사용자 지정** — 추가 G1 페어를 명시해서 매칭률 ↑ (예: "김한별과 장세현 같은 팀")
3. **OFF 데이터 강화** — 모두 2-3건+ 신청 받으면 자연스럽게 정답지 수렴

### 10-9. 후속 작업
- API: `POST /teams/auto-assign` 엔드포인트 (단일/다중 group_id 지원)
- DB UPDATE wrapper: 결과를 `nurses.team_id`에 반영
- 다중 그룹 통합 모드: 여러 group_id를 한 풀로 묶어 분배
- 시드 자동 선정 개선: 현재는 G1 풀의 첫 K명. 외부 입력 외에도 wanted 분포 기반 자동 선정 시도 가능
- UI: "팀 자동 분배" 버튼 + dry-run 미리보기

---

## 11. 원티드 팀 분류 wire-in + 속성 이벤트 모델 (옵션1) (2026-06-04)

> §10의 `team_auto_assign` 알고리즘을 실제 운영 흐름에 연결. 핵심 원칙: **팀 분류는 병동 내(team_id만) 변경, group_id는 절대 불변**. 병동 간 이동(옵션2)은 별개 흐름.

### 11-1. NurseAssignment kind/payload (DDL Phase 1.4)

| 항목 | 내용 |
|---|---|
| `nurse_assignment.kind` | `VARCHAR(30) NOT NULL DEFAULT 'transfer'` — reason(한글) 기반 명시적 분류 |
| `nurse_assignment.payload` | `NVARCHAR(MAX) NULL` — 속성변경 직전값 등 JSON |
| kind enum | transfer/dispatch/preceptee/leave/return/resign/**permanent_change** (`assignment_service.REASON_TO_KIND`) |

- 프로덕션 DDL 적용 후 백필 실행: 실측 분포 `preceptee 11 / dispatch 10 / transfer 3` (이전엔 전부 transfer로 오염돼 있었음 → §2.3 경고 케이스).
- 인덱스는 24행 규모라 생략(수백 행 이상 커지면 `idx_na_nurse_kind_date` 추가).

### 11-2. 속성 이벤트 모델 (permanent_change) — 존재 이벤트와 분리

핵심 통찰: assignment에 **두 종류**가 섞인다.

| 부류 | 예 | 기간 겹침 |
|---|---|---|
| **존재 이벤트**(어디 있나) | 파견·병동이동 | 동시 active 1개 (한 몸이 두 병동 불가) |
| **속성 이벤트**(무엇인가) | team/grade 변경 | **겹쳐도 됨** (팀 바뀐 채로도 파견 가능) |

| 함수(`assignment_service.py`) | 동작 |
|---|---|
| `create_permanent_change(...)` | 병동 내 team/grade 변경 이벤트. `source==target==group_id`, `payload={prev_team_id, prev_grade}`(되돌리기) |
| `flush_pending_permanent_changes(as_of)` | 발효일(`start_date<=as_of`)에 `Nurse.team_id/grade` 갱신 → **엔진은 현재값만 읽어 헬퍼 wire-in 위험 회피** |
| `_raise_if_overlap` | `ATTRIBUTE_CHANGE_KINDS` 제외 → 팀변경이 파견 생성 막지 않음 |

- 일일 스케줄러(`main.py`)에 발효 flush 연결.

### 11-3. 팀 분류 wire-in (`team_classify_service.py`)

| 함수 | 동작 |
|---|---|
| `preview_team_classification(group_id, year, month)` | **read-only**. 확정 원티드(`FixedWantedEntry`, OFF=shift∈{O,OFF,주}, 연차=Shift.type='휴가')로 `auto_assign_teams` 실행 → 제안 팀 + 현재팀 대비 diff + 통계 |
| `apply_team_classification(...)` | 변경분만 `permanent_change` 발행(대상월 1일 발효). 무변경 skip |

- num_teams = 현재 병동 distinct team_id 수. N전담(`is_night_nurse==['N']`) 풀 제외.
- **churn 최소화**: 제안 클러스터 → 현재 소속 중복 최대로 실제 team_id 매핑 (불필요한 팀 이동 억제).

### 11-4. 엔드포인트 + 권한 (`routers/teams.py`)

| 엔드포인트 | 권한 |
|---|---|
| `POST /teams/classify/preview` | 관리 그룹 한정 (read-only) |
| `POST /teams/classify/apply` | 관리자(ADM/수간호사/hn_auth) + 관리 그룹 한정 |

- **그룹관리자 개념 반영**: `group_access.resolve_managed_group_ids` 재사용 — HN은 home 그룹 + `Group.hn_id`에 본인이 등록된 그룹 전부 관리. 관리 목록 밖 그룹 지정 시 403, 다중 관리 그룹 미지정 시 400.
- **단, 분류는 group_id 불변** — 관리 그룹이 여럿이어도 각 그룹의 team_id만 재배치. 그룹 간 인원 이동 불가.

### 11-5. 전출자 과거병동 read-only 가시성 (`nurse_service.py`)

전출(병동이동) 발효 시 `nurses.group_id`가 target으로 바뀌어 과거 병동(source) 명단에서 사라지는 문제. 기존 inbound 메커니즘을 **역방향 재사용**:
- 리스트 쿼리에 `source==나 AND target≠나 AND reason='병동이동'` 갈래 추가 → 전출자 명단 노출
- `_build_inbound_blocks(caller_group_id=...)` — source==caller인 completed 병동이동 포함(전입처 B는 비오염)
- 프론트는 inbound 항목의 source/target 방향으로 '전출' 판단, 상세 차단(B-local 속성 leak 방지)

### 11-6. 실DB 통합 테스트 (2026-06-04, 그룹 `1019076bd1f7` 전도연 수간호사)

**팀 분류 preview/apply (확정원티드 2026-05 기준)**: 3팀, 풀 17명(N전담 2 제외), 변경 11명, overlap=1, 팀 5/6/6 균형. apply 11건 생성/skip 6 정상.

**team·grade 라이프사이클 (생성→발효→해제, 끝나고 원복)**:

| # | 케이스 | 결과 |
|---|---|---|
| 1-3 | team/grade/동시 기간설정 생성 | ✅ |
| 4 | payload 직전값 저장 | ✅ |
| 5 | 발효 前 flush = no-op (현재값 유지) | ✅ |
| 6 | 발효일 flush → team/grade 적용 | ✅ |
| 7 | 발효 행 status=completed | ✅ |
| 8 | 해제-1: pending 취소 → 미발효 | ✅ |
| 9 | 해제-2: 발효분 payload로 원복 | ✅ |
| 10 | 정리: 원상복구 + 행삭제 | ✅ |

→ **10/10 성공**. 테스트 데이터·간호사 속성 전부 원복(프로덕션 무영향).

### 11-7. 실DB로 잡은 버그 (SQLite 더블은 놓침)

| 버그 | 원인 | 수정 |
|---|---|---|
| `FixedWantedEntry.is_applied.is_(True)` | MSSQL이 BIT를 `IS 1`로 렌더 → 구문오류 | `== True` |
| apply가 무변경(4→4)도 이벤트 생성 | `nurses.team_id`=varchar vs 제안 team_id=int 비교 불일치 | `str()` 정규화 |

> 메모리 원칙("OPTIMAL/HTTP 200만 보고 끝내지 말 것") 그대로, 실DB 연동에서만 드러난 케이스.

### 11-8. 후속

- 옵션2(특정 N 병동 간 재분배 = 대량 transfer): kind=transfer 모델 위에 권한·정원·풀 정의 추가하여 별도 구현. → §12에서 구현.
- 발효된 permanent_change 되돌리기 전용 함수(payload prev 복원)를 cancel과 별도로 정식화 검토.
- master_admin 경로(`get_nurses_filtered_service`)에도 전출자 가시성 적용 여부.

---

## 12. 원티드 기반 병동 간 재분배 (옵션2) (2026-06-04)

> 수간호사가 화면에서 **여러 그룹을 선택**하면 그 풀 안에서 클러스터링해 각 그룹(=버킷)에 배정.
> 옵션1(team_id만, 병동 내)과 달리 **group_id 변경(병동이동)** 을 동반. preview→확인→apply 구조.

### 12-1. 신규 모듈 (`ward_redistribute_service.py`)

| 함수 | 동작 |
|---|---|
| `preview_ward_redistribution` | **read-only**. 선택 그룹 풀+확정원티드로 클러스터링 → 각 그룹 버킷 + **그룹→팀→간호사 중첩** + 이동 diff + 통계 |
| `apply_ward_redistribution` | 이동 간호사 → 병동이동(transfer, target_team_id 동반), 잔류+팀변경 → permanent_change, 동일 → skip |

### 12-2. 정원(capacity) 모드
- `even`: 균등분할(총원/N) ± tolerance
- `explicit`: 그룹별 목표 인원 `{group_id: 인원}` ± tolerance. cluster i ↔ ward i 고정(시드=그룹 G1), 풀이 [Σmin, Σmax] 안에 들어야 함.

### 12-3. 핵심 안전장치
| 항목 | 내용 |
|---|---|
| **churn 페널티** | 현재 병동 유지 보상(`home_cluster`/`w_churn`, 기본 500). 같은 정원에서 실DB 이동 **15→1** |
| **G1 사전검증** | 시니어 없는 병동 있으면 `WardSetupError` → **422 + `needs_g1_setup`**(병동목록), 프론트가 시니어 지정 유도. 차출로 얼버무리지 않음 |
| **role 혼합 경고** | AN/RN 등 직역 섞이면 경고 |
| **권한** | `_assert_groups_managed`: 관리자 + **선택 그룹 전부 ⊆ 관리 그룹**(아니면 403) |

### 12-4. 엔드포인트 (`routers/teams.py`)
- `POST /teams/redistribute/preview` (read-only, G1 미설정 시 422)
- `POST /teams/redistribute/apply`

### 12-5. team_auto_assign 확장 (옵션1·2 공통)
- 클러스터별 정원(`max_sizes`/`min_sizes`) + min-fill
- churn 페널티(`home_cluster`/`w_churn`)
- **스왑 mutate-while-iterate 버그픽스** (실DB가 잡은 크래시)

### 12-6. 검증
- 단위/통합: ward_redistribute 14 + API 5, 전체 스위트 **1251/1251**
- **실DB**: preview 양 모드(even/explicit) read-only 확인. explicit churn 500 → 이동 1.
  apply는 미래월(2026-08) 1명 이동 이벤트 생성→검증→**flush 없이 삭제로 완전 원복**(group_id 불변).

### 12-7. 후속
- explicit 모드 within-ward 팀(team_id)까지 apply에 반영(현재 transfer의 target_team_id로만 동반).
- 발효 후 대량 transfer 운영 가이드(롤백·알림 묶음).

---

## 13. N tail(전월 경계) 회복 정합(NOD 해결, 적용) + N블록 간격 cross-month(검토·미적용) (2026-06-11)

### 13-1. 배경 (버그)
전월 N tail 회복(2N→2OFF=하드락 ⑤, 3N→2OFF=하드락 ④)의 **partial 분기**(`_rem==1`, 전월에 회복 OFF를 이미 1개 소비한 경우 = `offs_after==1`)가 남은 1개 OFF를
`countable_off(T0) + countable_off(T0+1) >= 1`("월초 2일 중 아무 1일 OFF")로만 요구하고 `OnlyEnforceIf([end_prev_block])`로 게이트했다.
→ 솔버가 OFF를 **T0+1**에 주고 **T0(월초 첫날)에 근무(D)를 자유 배정** → 전월 마지막 OFF와 **연속이 깨져** 회복 위반 + N tail **NOD**(`N N O | D`) 발생. `end_prev_block`(=T0≠N) 게이트는 T0=N 탈출까지 허용.

실증: group `101358f4ef48` 2026-07, 박지은(383440) 6월 tail `… N N O`(cons_n=2, offs_after=1) → 7월 `D …` = 경계 `N N O | D D`.

### 13-2. 변경 — 회복 partial을 "경계 직후일 OFF 강제"로 정정

| 파일 | 변경 |
|---|---|
| `app/services/cp_sat_basic.py` (`:4276` 3N partial, `:4428` 2N partial) | expr `off(T0)+off(T0+1) >= 1` → **`off(T0) >= 1`**(경계 직후일 강제). `OnlyEnforceIf`에서 **`end_prev_block` 제거**(MUS용 `_co_lit`만 유지). |
| `app/services/cp_sat/fallback_lex.py` (3N·2N partial) | 동일 수정 — **기본 엔진(`SKIP_PRIMARY=1`)이 fallback_lex이므로 이 쪽이 실효 경로.** primary/fallback parity 유지. |

- 원리: `_rem==1` ⟺ `offs_after==1` ⟺ 전월 마지막날이 이미 OFF(=T0 직전 인접). 남은 회복 OFF는 **반드시 T0**여야 `전월OFF + T0OFF` = 연속 2OFF가 성립.
- **`==2`(rem≥2, offs_after==0) 분기는 무변경** — 양일 OFF(`== 2`) 강제가 이미 정확하고, `end_prev_block` 게이트도 타당(T0=N이면 3N 블록으로 넘어감).
- 하드락 정책 준수: 소프트화·플래그 종속 아님, **하드 제약을 정확히 강화**.

### 13-3. 검증
- group `101358f4ef48` 2026-07 재생성(v12·v13 동일): 박지은 경계 **`N N O O`**(연속 2OFF) → 하드락 ⑤ 충족, NOD 해소. `cp_sat_simple_test` 위반 0.
- ★부수효과(불가피): 박지은이 7/1 OFF로 빠지며 **7/1 D 커버리지 1 감소**. 실측상 7/1 D 가능자 = 유은혜 1명뿐(박지은=회복OFF, 한수아=N→D금지, 김원아·표유진·김민진=E→D금지[6/30=E, **issued 기준**], 장세현=N전담) → **D2 물리적 불가 = 하드락 준수의 불가피한 비용**(v6의 D2는 박지은 7/1=D=회복위반으로 메웠던 것). 솔버 동작 정상, 추가 코드수정 불필요. 운영 선택지: 7/1 D요구 2→1 하향 / 인력 재배치 / 수용.
- 함정 메모: inbound 간호사의 전월 tail은 source 병동의 **마감(issued, `status='issued'`) 근무표** 기준(`_query_prev_month_schedule_id`, `roster_create_service.py:1655`·inbound `:1969`). 최신 draft가 아님.

### 13-4. N블록 간격 cross-month 항 (§9-4/§9-9 후속) — 검토·실측 후 **미적용(폐기)**
§9의 "월경계 미반영 — 후속 작업 후보"를 시제품으로 구현(primary `objective_terms.py` + fallback `fallback_lex.py` n2n lex pass: 전월 N tail seed로 가상 block_end `pe=T0-1-offs_after` 산출 → 당월 첫 N까지 gap<target이면 soft 벌점)해 §13-5에서 실측 → **이 그룹엔 구조적 무효** + 타 그룹 실효 미검증 → **코드 폐기**(미커밋 변경 `git restore`). 회복(§13-2, 커밋 a60addd)은 무관하게 유지.

설계·실측 지식(향후 재검토용으로 보존):
- ★**weight 적용 범위**: `n_to_n_interval_penalty_weight`(기본 **300**, cp_sat_basic.py:614 — docs §9-2 "50"은 드리프트)는 **primary 목적함수에만** 곱해짐. **fallback n2n은 weightless lex minimize**(`m2.Minimize(sum((target-gap)*pair))`)라 weight 무관 → weight 튜닝은 기본 엔진(fallback) 출력에 영향 0. 게다가 **기본 엔진=fallback(`SKIP_PRIMARY=1`)** 이라 primary n2n 항 자체가 dormant.
- prepend 미구현 → 전월 day는 X변수 없음(seed 상수로만 표현 가능).

### 13-5. 실측 평가 (group `101358f4ef48` 2026-07) — 이 그룹은 구조적으로 효과 無
cross-month 항을 양 엔진에 넣고 fallback 재생성해 경계 N 간격(전월 마지막 N→당월 첫 N) 이동 여부를 실측. **세 가지 시도 모두 경계 placement 불변:**

| 시도 | 결과 |
|---|---|
| weight 50→300 | fallback weightless라 무효(primary는 dormant) |
| cross-month 항 추가 | 박지은 첫N=7/5(gap6)·표유진 첫N=7/3(gap6) **불변**, n2n deficit 29→35(항만 추가) |
| n2n lex N-range freeze +2 | N range 5 유지(여유 미사용), 경계 **불변** |

- binding 제약은 N-range freeze가 아니라 **N 커버리지**(1 N/day, N가능 5~6명, N전담1) — 월초 N 슬롯을 메우려면 일부 간호사가 N을 일찍 할 수밖에 없음. docs §9-8("짧은 gap은 N상한+야간몰아넣기로 구조적")과 일치.
- 결론: **소규모·빠듯한 N 그룹에선 경계 간격을 못 늘림(구조적).** → cross-month 항 **미적용(폐기)** — 효과 미검증이라 코드 미보존, §13-4/5의 설계·실측만 지식으로 남김. 박지은 NN OO 회복(§13-2, 커밋)은 정상 유지. (대규모/N여유 그룹 재검토는 향후 과제.)

### 13-6. day0 경계 커버리지 미달(불가피)의 운영 처리
- 7/1 D 미달(필요2·확보1)은 §13-3대로 하드락(회복+ED/ND+N전담) 준수의 불가피한 산술 결과(D 가능자=유은혜 1명).
- **처리: 7/1 일별 D 요구를 2→1 하향** = `daily_shift` 일별 정원 설정(`daily_shift_requirements_by_day`, roster_create_service.py:742) 조정 = **운영 데이터**(솔버 코드 아님). 코드 자동완화는 실제 부족까지 가릴 위험으로 비채택.
