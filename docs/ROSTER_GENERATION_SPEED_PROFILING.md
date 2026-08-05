# 근무표 생성 속도 프로파일링

- **작성일**: 2026-07-01
- **대상**: 시화병원(office 101358) 병동들, 2026년 6·7·8월
- **엔진**: `optimize_fallback_lex_hard_first` (폴백 서열 CP-SAT), `SKIP_PRIMARY=1` 기본이라 폴백이 실질 메인 경로
- **환경**: 로컬 uvicorn(:8000, reload=True) + MSSQL(원격), 10코어 머신
- **방법**: `generate_roster_service` 직접 호출 + `cp_model.CpSolver.Solve` 몽키패치로 per-solve 통계 캡처, 최종 근무표에서 품질지표 재계산. 저장소 코드는 실험용 env 훅을 임시 추가 후 **전부 원복**(git clean).

> ⚠️ 모든 수치는 CP-SAT 실행편차(run-to-run)가 있음. Stage2 wall 은 26~38s로 흔들림. 큰 효과(수십 %)는 신뢰, 작은 차이(±수 %)는 노이즈로 간주.

---

## 1. 실행 전제 (테스트 시 함정)

- `/roster_create/generate` 는 **cookie `access_token`** 로 JWT 인증. Bearer 토큰을 쿠키로 전달.
- **`req.group_id` 를 반드시 명시**해야 함. 미지정 시 `resolve_effective_group` 이 **DB home = `101358ddf07b`(9병동, 간호사 1명, grade 최소합 D=6>요구)** 로 해석 → precheck 즉시 차단(~1s). 토큰 병동(중환자실2)과 무관.
- **원티드 데이터가 있는 월만 생성 가능**([roster_create_service.py:4369](../app/services/roster_create_service.py#L4369)). 2026년 기준 대부분 병동이 **6·7·8월**만 보유. 9·12월 등은 "wanted 작성 먼저" 에러.

### 병동 목록 (office 101358)

| 그룹ID | 이름 | 인원 |
|---|---|---|
| 10135834e48b | 중환자실2 (ICU2) | 39 |
| 10135857f9f9 | 중환자실1 (ICU1) | 37 |
| 101358af4a2e | 9병동-NA/LP | 20 |
| 10135890c287 | 9병동-9B | 16 |
| 101358f6de7b | 9병동-9A | 16 |
| 101358f4ef48 | 9병동-CCR | 7 |
| 101358ddf07b | 9병동(home) | 1 (stale) |

---

## 2. 파이프라인 구조

```
POST /roster_create/generate
  → generate_roster_service()          roster_create_service.py:4349
     1) 데이터 로드(DB) + 그룹 해석
     2) Precheck(산술 infeasibility)    :5343   ← grade_min 합>요구 등 즉시 차단
     3) _run_cp_sat_basic(time_limit=60s, advanced면 180s)
        → SKIP_PRIMARY=1 → primary 스킵, 바로 폴백
        → optimize_fallback_lex_hard_first()   fallback_lex.py:130
             Stage1 커버리지 → Stage2 안전+lex5pass → Stage3 선호/공정성
     4) 후처리(off_swap, 프리셉티 sync) + DB 쓰기 + 응답 빌드
```

### 시간 예산 (fallback_lex.py:169)
- 총 `time_limit_seconds` = **60s 기본** / **180s (advanced_inference)**.
- 분할 tl1(45%) / tl2(35%) / tl3(20%). 단, **cap 은 상한이지 소비량이 아님** — OPTIMAL 증명 시 조기반환.

### Solve() 호출 (한 번 생성 = ~7회)
- Stage1: 최대 2회(hard, broad_soft)
- Stage2: base + lex 4pass(OFF range→N range→n2n deficit→D/E balance)
- Stage3: 1회 (`num_search_workers=8`)

---

## 3. CP-SAT 은 "계산"이 아니라 "탐색"

- 근무표 = NP-hard 조합최적화. 중환자실1 경우의수 ≈ 5^(37×31) ≈ 10^800.
- iteration 은 `Solve()` **내부**에 있음: 제약 전파 + 분기/역추적 + 충돌학습(CDCL).
- 상태 의미: **OPTIMAL** = 최선임을 증명 완료(조기반환) / **FEASIBLE** = 답은 찾았으나 최선 증명 전 시간초과(cap 소진).

### 실측 (중환자실1 8월, 60s): 탐색은 가볍다
| Solve | stage | status | branches | wall |
|---|---|---|---|---|
| #1 | 커버리지 | OPTIMAL | 7,716 | 0.48s |
| #2 | 안전 | OPTIMAL | 10,578 | 2.21s |
| #3~6 | lex passes | OPTIMAL | ~10~20K | 1~3s |
| #7 | **선호(Stage3)** | **FEASIBLE** | 36,601 | **12.08s (cap)** |

- 총 분기 **10.6만, 충돌 0**. → "수백만 탐색"이 아님. 제약 전파가 강해 대부분 즉시 최적증명.
- **시간의 정체 = 개방형 Stage3 가 cap 을 꽉 씀 + 비-솔버 오버헤드.** 트리탐색보다 LP완화/증명에 시간이 감.

### 시간 분해 (중환자실2 8월, 총 ~41~67s)
| 구간 | 비중 | 정체 |
|---|---|---|
| 비-솔버 | **~19s (절반 가까이)** | DB 로딩·precheck·후처리·DB 쓰기·응답빌드 |
| Stage3 | ~12s | FEASIBLE = cap 소진 (선호 최적화) |
| Stage2 | ~10s | 안전+lex, 대개 OPTIMAL |
| Stage1 | ~0.8s | 즉시 |

### "3분" 케이스 = `advanced_inference`
- advanced 켜면 예산 60→180s, **모든 cap 3배**. 중환자실1 8월: Stage3 12s→**37s**로 팽창(FEASIBLE). 빡센 달이면 Stage2 pass 들도 cap 소진하며 쌓여 ~180s.
- 중환자실1 **7월은 INFEASIBLE** → infeasibility 진단(0초 solve 22회 + MUS) 경로. 3분 케이스와 무관.

---

## 4. "선호 최적화"(Stage3)가 최대화하는 것

[fallback_objectives.py](../app/services/cp_sat/fallback_objectives.py) — 하드(커버리지·안전) 동결 후 아래를 가중합 Maximize:

1. **개인 희망**: 원티드/근무선호/휴무요청 반영(`preference_matrix`), 월단위 선호(강도 0~10), 야간전담 보너스.
2. **공정성**: KLD 분포 균등(D/E/N/총근무를 간호사에 고르게, baseline_target=21), 과잉인원 균등화.
3. **패턴 품질**: 고립근무(O-근무-O) 벌점, 외톨이 E, 연속OFF 선호, n2n 야간간격, 연속근무 soft 상한.
4. **조직**: 프리셉터 페어링, 팀 밸런스/team_min, 등급 분배.

→ 서로 상충하는 수십~수백 항이라 **최적 증명이 극난 → 항상 FEASIBLE 로 cap 소진.**

---

## 5. A/B 실험 — 무해한 속도 knob 은 없다

기준(중환자실2 8월): 솔버 ~40~52s, 안전위반합 114, 커버리지 0.

| 시도 | env | 속도 | 품질 영향 | 판정 |
|---|---|---|---|---|
| n2n pass 제거 | `FB_SKIP_N2N=1` | Stage2 26→12s (-14s) | **야간간격 deficit 66→139 (2.1배 악화)**, 하드는 동일 | ❌ 품질↓ |
| worker 8→16 | `FB_WORKERS=16` | 이득 0 (오히려 +1.6s) | 야간 deficit med 16→38 악화 | ❌ 10코어 오버구독 |
| 예산 재배분 | `tl1=0.15 tl2=0.65` | 이득 0 | 동일 | ❌ Stage1은 cap 아닌 early-return |
| lex 시간 축소 | `N2N_LEX_TIME_FRAC=0.2` | 이득 0 | deficit 16→50 악화, 한 런 안전 2308 폭주 | ❌ 품질↓·불안정 |

**결론: knob 튜닝으로 무해한 속도개선은 없음.** 각각이 어떤 병동/조건에서 품질을 무너뜨림.

---

## 6. Stage3 cap 민감도 — 병동×월 매트릭스 (핵심 교훈)

cap 12s→6s, 4병동×6·7·8월. obj 는 Maximize라 Δ%<0 = 나빠짐.

| 병동(인원) | 월 | S3 cap 포화 | **선호 obj Δ%** | 공정성 D | 공정성 N | 고립 | 야간 n2n |
|---|---|---|---|---|---|---|---|
| ICU2(39) | 6 | YES | +1.3% | 2→2 | 0→0 | 6→9 | 5→0 |
| ICU2(39) | 7 | YES | +1.2% | 4→4 | 3→2 | 3→4 | 49→21 |
| ICU2(39) | 8 | YES | −1.2% | 3→3 | 1→1 | 7→2 | 16→8 |
| ICU1(37) | 6 | YES | **−54.5%** | 6→8 | 1→5 | 15→12 | **1→133** |
| ICU1(37) | 8 | YES | −0.4% | 6→6 | 1→1 | 10→12 | 4→0 |
| 9B(16) | 7 | YES | **−10.1%** | 20→20 | 15→15 | 0→2 | 25→41 |
| 9B(16) | 8 | YES | **−13.5%** | 19→19 | 15→15 | 0→0 | 79→54 |
| CCR(7) | 6 | no(9.8s) | −29.2% | 10→11 | 15→15 | 1→1 | 21→35 |
| CCR(7) | 7 | no(0.4s) | −6.0%* | 10→11 | 15→15 | 0→0 | 29→29 |
| CCR(7) | 8 | no(3.5s) | −0.0% | 16→16 | 15→15 | 0→2 | 37→28 |

*(CCR 7월은 S3가 0.4초에 끝나 cap과 무관 → 실행편차. ICU1 7월·9B 6월은 INFEASIBLE)*

### 교훈: 단일 병동 일반화 금지
- 처음 중환자실2(39, 과잉인원)만으로 "cap 줄여도 선호 안 떨어짐"이라 결론 → **틀림.** ICU2가 하필 가장 둔감한 케이스였음.
- 실제 영향은 **0% ~ −54%**, 병동·월마다 완전히 다름:
  - **인원 여유가 좌우**: 과잉(ICU2) → cap 무관 / 빡셈(ICU1 6월, 9B) → 급락.
  - **같은 병동도 월마다 딴판**: ICU1 8월 −0.4% vs 6월 −54.5%.
  - **공정성도 cap 민감할 수 있음**(ICU1 6월 N범위 1→5) — "공정성은 Stage2 소유라 무관"도 틀림.
  - 야간 n2n 방향은 인스턴스마다 뒤집힘(신뢰 불가 신호).
- **판단축 = 인원여유 × 월별난이도** (Stage3가 cap 을 실제로 포화시키는지). 고정 낮은 cap 일괄적용은 위험.

---

## 7. 결론 및 개선 방향

### 지금 당장 토글할 안전한 속도 스위치: **없음**
- 모든 knob 이 어떤 병동/월에서 품질을 무너뜨림. `advanced_inference`는 기본 off(켜면 3배 느림).

### 유망하나 구현+검증 필요 (품질 리스크 순)
1. **비-솔버 오버헤드(~19s) 절감** — DB 로딩·후처리·쓰기·응답빌드. 전체의 절반 가까이이며 **CP-SAT 무관·품질 100% 중립**. 프로파일 후 쿼리 배칭/캐싱. *리스크 최저, 1순위 추천.*
2. **Stage3 `relative_gap_limit` 조기종료** — 고정 cap 대신 "충분히 수렴하면 멈춤". 여유 병동은 알아서 빨리 끝나고(CCR처럼) 빡센 병동만 cap 소진. 병동별 품질손실 자동조절. 단 게이트 튜닝 + 매트릭스 재검증 필요.

### 검증 원칙 (이번에 지불한 교훈)
- 헤드라인 지표(안전위반합·OPTIMAL·집계 obj)만 보면 함정. **실제 근무표 패턴(고립근무·야간간격)과 공정성까지** 봐야 함.
- 속도 튜닝 안전성은 **병동 하나가 아니라 인원여유×월난이도 분포**로 판단.
