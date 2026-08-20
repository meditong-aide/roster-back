# 인피저블 진단·해결 — 온톨로지 관계분석 & probe 개선

> 근무표 생성이 막혔을 때 **왜** 막혔는지 데이터로 짚고, **한 번의 클릭**으로 풀 수 있는 해결책을 만드는 파이프라인.
> 실제 병동(시화 중환자실 · 2026-08 · 6명)으로 라이브 실행을 반복하며 진단 정확도·속도·문구를 끌어올림.

- 기간: 2026-07-22 ~ 2026-07-24
- 범위: backend 3 commits + frontend 2 commits
- 테스트: 1645 pass · 회귀 0

## TL;DR

| 지표 | 변화 |
|---|---|
| 해결책 탐색 재solve 수 | **105 → 3** |
| 실패 경로 소요(추정) | **94s → ~20s** |
| 온톨로지 공급 속성(단일소스) | **27** |
| 사용자 노출 "증상" 문구 | **0** |

핵심은 **온톨로지(presolve max-flow)가 제약을 구조적으로 파악**해서, probe가 시행착오로 헤매던 걸 **정확히 타깃팅**하도록 만든 것. 부수적으로 여러 배선 버그와 문구 문제를 잡았다.

---

## A. 배경 — 무엇을 고치려 했나

두 축:
1. 인피저블 원인을 정확히·빠르게 진단
2. 사용자가 클릭 한 번에 풀 수 있는 해결책 제시

**테스트 케이스**: 시화 중환자실 · 2026-08 · 간호사 6명. `177659(김수선)`이 **주말휴무 + 야간 고정(n_exact)=13**인데 config **max_nig=7** — 두 제약이 얽혀 **배정 0건(총붕괴)**으로 인피저블.

**기존 구조**: precheck(산술 사전차단) → solve(fallback_lex) → 실패 시 **probe**(완화안을 하나씩 재solve해 검증). probe는 확실하지만 브로드하게 훑어 느리고, 온톨로지 그래프는 **지어졌지만 진단에 미배선**이었다.

---

## B. 실험 여정 — 라이브를 돌리며 반복 개선

같은 케이스를 반복 실행 → 로그 분석 → 원인 발견 → 수정 → 재실행. 속도가 병목을 바꿔가며 드러났다.

### 실행 ① — 94.0s · 105 probe · INFEASIBLE
- **발견**: 단일 완화 14개 전부 실패 → 콤보 탐색이 `C(14,2)=91`쌍 **전수**. 정답(#24)을 찾고도 91까지 다 돌았음(least-invasive 고르려고).
- **조치**: `_find_combo`에 **first_hit**(첫 해결 조합서 조기종료) + 우선순위 정렬.

### 실행 ② — 51.0s · 25 probe
- **발견**: 콤보 91→11로 줄었지만 **온톨로지가 관여 안 함** — 로그의 완화 순서가 원래 카탈로그 순서 그대로(presolve가 이 "제약모순형"을 못 봄, shortages 비어있음).
- **조치**: presolve에 **개인 제약 압박 감지**(pressure_families) 추가 — `night_floor_over_cap`(n_exact>max_nig)·`weekend_off_load`. 정찰 dict가 `n_exact`를 안 담던 것도 발견(→ 27필드 단일소스화).

### 실행 ③ — 47.4s · 15 probe
- **확인**: 로그에 `[UndiagProbe] 온톨로지 우선 완화군 ['disable_weekend_off_only','raise_max_night_cap'] 먼저 검증` — **온톨로지가 실제로 활용됨**. 정답 콤보를 첫 시도에 확보.
- **남은 관찰**: 시간이 51→47로 거의 안 줆 → probe가 병목이 아니었음.

### 실행 ④ — 42.9s · 3 probe (hard-filter)
- **조치**: **hard-filter** — 압박 감지 시 압박군 단일만 확인→그 콤보→성공하면 종료(나머지 생략), 실패해야 전수 폴백. 105→**3**.
- **발견**: 여전히 42s → **진짜 병목은 per-nurse 재solve가 verify-mode 미사용으로 stage3 최적화를 time_limit(20s)까지 소비**하던 것. → `_quiet_verify_solve` 적용.

### 클릭 실험 — 주말휴무 해제 눌렀는데 적용 안 됨
- **버그**: config 옵션의 `max_nig`(DB컬럼)는 반영됐는데 `weekend_off_only_enable`(비-DB 솔버키)이 누락. 추적 결과 **sync `/generate` 핸들러가 `config_override`를 서비스로 안 넘김**.
- **추가 발견**: per-nurse 주말 probe가 "feasible" 거짓양성(검증이 n_exact를 안 봄) → 데이터(presolve flag)로 **주말+야간 번들**하도록 교정. "주말휴무 전용 해제"(config 플래그)는 애초에 **주말을 끄는 게 아니라** 정책 토글이라 **해결책에서 제거**.

### 속도 진행 (실패 경로)

| 단계 | probe 수 | 시간 | 레버 |
|---|---|---|---|
| 최초 | 105 | 94.0s | 콤보 전수 |
| 콤보 first_hit | 25 | 51.0s | 첫 해결 조합서 종료 |
| + 온톨로지 우선정렬 | 15 | 47.4s | 압박군 먼저 |
| + hard-filter | 3 | 42.9s | 압박군만→콤보→폴백 |
| + per-nurse verify-mode | 3 | ~20s* | stage3 스킵(feasibility만) |

\* verify-mode는 코드 반영 후 미실측 — 주말 per-nurse feasible 재solve의 ~20s stage3 제거로 추정. 남은 시간은 base 생성 2회 + 재solve별 **모델 빌드** 반복(cross-month·제약 재조립 ~1.5s/회)이며, 근본 개선은 **빌드 캐싱**(별도 과제).

---

## C. 개발 내용 — 영역별

### C-1. 온톨로지 관계분석 (핵심)

- **제약모순 감지** [신규]: presolve가 개인 하드모순을 산술로 감지 → `constraint_flags`/`pressure_families`. `night_floor_over_cap`(n_exact>max_nig) · `shift_floor_over_workdays`(하한합>근무가능일) · `weekend_off_load`. probe 우선순위로 먹임.
- **관계 → 인원 산출** [신규]: **주말휴무자를 주말 요일 공급 0으로 요일별 모델링** → max-flow `day_shortage`로 **weekend_release_needed**(풀어야 할 최소 인원)를 **조합탐색 없이** 직접 계산. 예: 6명·수요4/일 → 주말휴무 1명=0, 3명=1, 4명=2. per-nurse가 그 N명을 한 번에 해제.
- **공급 정확도 확장** [신규]: **입·퇴사(활성일 범위)** — 월중 입퇴사자를 범위 밖 공급0 + 월 capacity 상한(엔진 join/leave 미반영으로 과대계상하던 것 교정). **fixed_shift** — 그 간호사는 그 시프트만 공급(다른 시프트 0). **allowed_shifts** — 이미 eligibility 반영.

### C-2. probe 타깃팅 & 속도

- **소프트정렬**: 압박군을 앞으로 정렬(우선 검증), 못 풀면 나머지 폴백(완결성 유지).
- **hard-filter**: 압박 신뢰 시 압박군 단일+콤보만 → 성공하면 나머지 생략, 실패해야 전수.
- **콤보 first_hit**: 첫 해결 조합서 종료(전수 91→소수). **stop_after**: 검증 2건이면 종료.
- **verify-mode**: per-nurse 재solve는 stage3(최적화) 스킵 — feasibility만 필요.
- **time_limit** 60→20s, 진행 `\r` 제자리 갱신 + 내부 stage 로그 억제(`AIDE_PROBE_*`).

### C-3. per-nurse 해결책

| 상황 | 해결 옵션(문구) |
|---|---|
| 주말 단독 | `{이름} 간호사 주말 휴무 해제` |
| 주말+야간 [번들] | `{이름} 주말 휴무 해제 + 야간 고정 {n_floor}→{max_nig}회로 낮추기` |
| 주말 다인 [신규] | `주말 휴무 {N}명 해제 ({이름들})` |
| 야간 하향 | `{이름} 야간 고정 {현재}→{상한}회로 낮추기` |

값(13→7 등)은 **전부 데이터 주입**, 하드코딩 아님. 데이터가 "두 모순"을 알면 처음부터 **묶어서** 제시(클릭→실패→또 뜸 방지).

### C-4. 데이터 단일소스 & 문구

- **27필드 단일소스**(`_ONTOLOGY_NURSE_FIELDS`): 정찰 dict를 손으로 고르지 않고 포괄(속성 누락 사각지대 제거).
- **NO_ASSIGNMENT 문구 원천 차단**: 트리거(reason_code)는 유지하되, "실근무 배정 0건" 증상 문구는 사용자에게 절대 노출 안 됨. 요약을 전체 옵션(config+per-nurse)에서 생성 + 3중 안전망.
- 모달: 제목/안내 문구, 옵션 번호배지, 큰 글씨, 닫기 확인.

---

## D. 잡은 버그

- **allowed_shifts as-of stale 컬럼** — N전담 해제([])가 미래월 생성에 안 먹음(컬럼이 as-of-today 캐시라 stale). 엔진 투입 전 **대상월 as-of 오버레이**로 교정.
- **저장↔config 결합** — 간호사 월한도 저장이 최신 roster config(max_nig·nig_req)를 읽어 **차단**. 개인 속성 저장은 config 독립이어야 — 결합 제거, 충돌은 생성 시점에서 판정.
- **sync `/generate` config_override 미전달** — 해결책 클릭 시 비-DB 솔버키(`weekend_off_only_enable` 등)가 누락돼 적용 안 됨. 핸들러가 파라미터를 서비스로 전달하도록 수정.
- **per-nurse 필터가 config 콤보 오삭제** — 주말 옵션 추가 시 `weekend_off_only_enable=False`인 옵션을 전부 지워 **콤보까지 삭제** → 옵션 1개만 뜸. 단독 옵션만 지우게 수정.
- **grade는 건드리지 않음(확인 후 보류)** — "grade 조절" 문구 제거 검토 → **확인 결과 grade는 기본 hard**(`allow_soft_fallback=False`). 제거하면 grade 모순이 원인불명 infeasible이 됨 → **보류**. 필요 시 `allow_soft_fallback` 조건화가 올바른 방향.

---

## E. 평가방법 — 어떻게 검증했나

두 층: 결정적 회귀 테스트(DB 불필요) + 실제 병동 라이브 실측(3지표). 판정은 "생성 성공"에서 멈추지 않고 결과 품질까지.

### E-1. 단위·회귀 테스트 (결정적)

presolve의 관계분석은 **산술**이라 DB·솔버 없이 결정적으로 검증된다. `test_presolve_pressure.py`가 진단의 각 판정을 못박는다:

| 테스트 | 검증하는 판정 | 기대 |
|---|---|---|
| `night_floor_over_cap` | n_exact=13 > max_nig=7 → 야간 압박 감지 | `night_cap ∈ families` |
| `weekend_off_load` | 주말휴무자 → 주말 압박 family | `weekend_off ∈ families` |
| `coupled_prioritizes_both` | 주말+야간 동시 → 우선순위 앞 2칸에 **둘 다** | `prio[:2]={주말,야간}` |
| `weekend_release_needed` | 요일별 max-flow로 풀 인원 산출 | `1명→0·3명→1·4명→2` |
| `shift_floor_over_workdays` | d_exact=25 > 근무가능일=21 → off 압박 | `off_budget ∈ families` |
| `fixed_shift_reduces_elig` | 전원 D고정 → E·N 공급 0 | `E/N=eligibility_shortage` |
| `joining_midmonth` | 월중 입사 → 월초 공급 급감 | `monthly_shortage>0` |
| `no_pressure_within_bounds` | 한도 내 + 주말휴무 없음 → 무압박(오탐 방지) | `families=[]` |

probe 측은 `test_probe_soft_ordering.py`(9) — 우선군 재정렬·early-stop·**hard-filter가 압박군만 3콜 후 종료**·**실패 시 전수 폴백(완결성)**. 저장 독립·as-of는 `test_monthly_limit_save_asof`·`test_allowed_shift_asof_generation`. **전체 1645 pass · 회귀 0.**

### E-2. 라이브 E2E 실측 (실제 병동)

결정적 테스트가 "논리"를 보증하지만, **실제 데이터에서 온톨로지가 정말 관여했는지·얼마나 빠른지**는 라이브로만 확인된다. 시화 중환자실·2026-08을 반복 트리거하며 **3지표**를 측정:

| 지표 | 무엇을 보나 |
|---|---|
| probe 수 | 재solve 횟수 — 타깃팅 효율 (105→3) |
| wall-clock | 체감 소요 — 병목 위치 노출 (94→42.9s) |
| 로그 신호 | 온톨로지 실제 관여 여부 |

- **온톨로지 관여 확인**: 로그에 `[UndiagProbe] 온톨로지 우선 완화군 ['disable_weekend_off_only','raise_max_night_cap'] 먼저 검증` — 우선군이 **실제 병목과 일치**하는지 눈으로 대조.
- **데이터 정합 확인**: as-of 오버레이 `[AllowedShiftAsof] 2026-8: 177659 ['N']→[]` — 대상월 값이 엔진에 들어갔는지.
- **해결 유효성**: 제시된 옵션을 **실제 클릭**→재생성이 feasible(OPTIMAL)로 떨어지는지. 여기서 "버튼 눌렀는데 또 2개 뜸" 같은 **false-positive를 잡음**(테스트로는 안 잡히는 배선/검증 갭).

> 라이브는 Claude 샌드박스에서 DB 접근 불가(`root@localhost` 인증 실패) → David가 브라우저+JWT로 트리거하고 로그를 회신, Claude가 분석하는 **2인 루프**로 진행.

### E-3. 판정 원칙 — "생성됨"에서 멈추지 않는다

**3단계 회귀 검증**: ① **파라미터**가 실제로 반영됐나(config delta·period write) → ② **조건**이 솔버에 적용됐나(로그·제약) → ③ **결과 품질**이 괜찮나(severity·N/OFF range·야간간격·고립근무). `HTTP 200`·`OPTIMAL`만 보고 끝내면 "적용된 척"을 놓침 — 그래서 클릭 실험에서 배선 버그 4건이 드러났다.

| 판정 신호 | 무엇을 뜻하나 |
|---|---|
| **진단 정확** | 로그 우선군 == 실제 병목(주말+야간). 엉뚱한 family 앞세우면 실패. |
| **해결 유효** | 옵션 적용 후 재생성 feasible. 값(13→7)이 데이터에서 나온 실제 수치. |
| **완결성** | 우선군으로 못 풀면 전수 폴백 — hard-filter가 정답을 놓치지 않음. |
| **무회귀** | 속도·타깃팅 개선이 기존 1645 테스트를 안 깬다. |

---

## F. 커밋

| 레포/브랜치 | 해시 | 내용 |
|---|---|---|
| backend / dev | `5511604` | per-nurse 야간한도 옵션 + allowed_shift as-of |
| backend / dev | `2aa7e79` | 월한도 저장 config 독립 + as-of |
| backend / dev | `5c3746e` | 온톨로지 관계분석 probe(소프트정렬·하드필터·번들/다인·속도·문구·버그픽스) |
| frontend / feat/infeasibility-resolution-ui | `bf810a4a` | 해결책 원클릭 재생성 배선(타입·mutation·onSelectResolution) |
| frontend / feat/infeasibility-resolution-ui | `8a37f25d` | 모달 UI 개편(번호·큰글씨·닫기확인) |

**남은 것**: push(인증) · 라이브 실행 ⑤ 실측 · grade `allow_soft_fallback` 조건화(선택) · cross-month 꼬리 presolve 반영(정확도) · 모델빌드 캐싱(근본 속도).

---

### 관련 파일

- backend: `roster_create_service` · `cp_sat/undiagnosed_probe` · `ontology_graph/presolve_diagnosis` · `nurse_monthly_limit_service`
- frontend: `RosterCreateBlockingInfeasibilityAlert` · `Roster_create` · `useGenerateRosterMutation` · `types/roster-infeasibility`
- tests: `test_presolve_pressure`(8) · `test_probe_soft_ordering`(9) · `test_monthly_limit_save_asof` · `test_allowed_shift_asof_generation`
