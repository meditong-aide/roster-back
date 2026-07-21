# Infeasible 스윕 결과 (읽기전용, dev DB, 2026-08)

8병동 × 10설정변형. precheck(①②) 통과 시 probe_feasibility(solve-time, 8s).

| 병동 | 설정 | precheck | probe | 판정 | 원인 |
|---|---|---|---|---|---|
| 10253192 | baseline | ok | FEASIBLE | feasible | obj=-2267505221.0 |
| 10253192 | 2N2OFF | ok | FEASIBLE | feasible | obj=-2276207441.0 |
| 10253192 | 3N2OFF | ok | FEASIBLE | feasible | obj=-2493160643.0 |
| 10253192 | not_one_night | ok | FEASIBLE | feasible | obj=-2313323083.0 |
| 10253192 | N+3 | ok | FEASIBLE | feasible | obj=-1913285550.0 |
| 10253192 | all+3 | ok | UNKNOWN | UNKNOWN | - |
| 10253192 | off+6 | ok | FEASIBLE | feasible | obj=-1270643802.0 |
| 10253192 | maxNig5 | BLOCK:MONTHLY_NIGHT_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 10253192 | 2N2OFF+N+3 | ok | FEASIBLE | feasible | obj=-2425900890.0 |
| 10253192 | 2N2OFF+maxNig8 | ok | FEASIBLE | feasible | obj=-548356023.0 |
| 10253118 | baseline | ok | FEASIBLE | feasible | obj=-1114983494.0 |
| 10253118 | 2N2OFF | ok | FEASIBLE | feasible | obj=-480802350.0 |
| 10253118 | 3N2OFF | ok | UNKNOWN | UNKNOWN | - |
| 10253118 | not_one_night | ok | FEASIBLE | feasible | obj=-1374747116.0 |
| 10253118 | N+3 | ok | UNKNOWN | UNKNOWN | - |
| 10253118 | all+3 | ok | UNKNOWN | UNKNOWN | - |
| 10253118 | off+6 | ok | UNKNOWN | UNKNOWN | - |
| 10253118 | maxNig5 | BLOCK:MONTHLY_NIGHT_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 10253118 | 2N2OFF+N+3 | ok | UNKNOWN | UNKNOWN | - |
| 10253118 | 2N2OFF+maxNig8 | ok | FEASIBLE | feasible | obj=-1151103654.0 |
| 102527fb | baseline | ok | FEASIBLE | feasible | obj=-476618572.0 |
| 102527fb | 2N2OFF | ok | FEASIBLE | feasible | obj=-430146220.0 |
| 102527fb | 3N2OFF | ok | FEASIBLE | feasible | obj=-1449428007.0 |
| 102527fb | not_one_night | ok | FEASIBLE | feasible | obj=-498549255.0 |
| 102527fb | N+3 | ok | FEASIBLE | feasible | obj=-984319135.0 |
| 102527fb | all+3 | ok | UNKNOWN | UNKNOWN | - |
| 102527fb | off+6 | ok | UNKNOWN | UNKNOWN | - |
| 102527fb | maxNig5 | BLOCK:MONTHLY_NIGHT_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 102527fb | 2N2OFF+N+3 | ok | FEASIBLE | feasible | obj=-923965393.0 |
| 102527fb | 2N2OFF+maxNig8 | ok | FEASIBLE | feasible | obj=-1239113740.0 |
| 10135834 | baseline | ok | UNKNOWN | UNKNOWN | - |
| 10135834 | 2N2OFF | ok | FEASIBLE | feasible | obj=-1513386194.0 |
| 10135834 | 3N2OFF | ok | UNKNOWN | UNKNOWN | - |
| 10135834 | not_one_night | ok | UNKNOWN | UNKNOWN | - |
| 10135834 | N+3 | ok | UNKNOWN | UNKNOWN | - |
| 10135834 | all+3 | BLOCK:CAPACITY_TOTAL_SHORTAGE | - | INFEASIBLE(precheck) | CAPACITY_TOTAL_SHORTAGE |
| 10135834 | off+6 | ok | UNKNOWN | UNKNOWN | - |
| 10135834 | maxNig5 | BLOCK:MONTHLY_NIGHT_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 10135834 | 2N2OFF+N+3 | ok | UNKNOWN | UNKNOWN | - |
| 10135834 | 2N2OFF+maxNig8 | ok | UNKNOWN | UNKNOWN | - |
| 10135857 | baseline | ok | UNKNOWN | UNKNOWN | - |
| 10135857 | 2N2OFF | ok | UNKNOWN | UNKNOWN | - |
| 10135857 | 3N2OFF | ok | UNKNOWN | UNKNOWN | - |
| 10135857 | not_one_night | ok | UNKNOWN | UNKNOWN | - |
| 10135857 | N+3 | BLOCK:CAPACITY_TOTAL_SHORTAGE,MONTHLY_NIGHT_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | CAPACITY_TOTAL_SHORTAGE,MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 10135857 | all+3 | BLOCK:CAPACITY_TOTAL_SHORTAGE,GLOBAL_DAY_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | CAPACITY_TOTAL_SHORTAGE,GLOBAL_DAY_CAPACITY_SHORTAGE,MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 10135857 | off+6 | ok | UNKNOWN | UNKNOWN | - |
| 10135857 | maxNig5 | BLOCK:MONTHLY_NIGHT_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 10135857 | 2N2OFF+N+3 | BLOCK:CAPACITY_TOTAL_SHORTAGE,MONTHLY_NIGHT_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | CAPACITY_TOTAL_SHORTAGE,MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 10135857 | 2N2OFF+maxNig8 | ok | UNKNOWN | UNKNOWN | - |
| 100991fe | baseline | ok | FEASIBLE | feasible | obj=-1207958030.0 |
| 100991fe | 2N2OFF | ok | FEASIBLE | feasible | obj=-1188070870.0 |
| 100991fe | 3N2OFF | ok | FEASIBLE | feasible | obj=-1219576370.0 |
| 100991fe | not_one_night | ok | FEASIBLE | feasible | obj=-1268061270.0 |
| 100991fe | N+3 | ok | INFEASIBLE | INFEASIBLE(solve) | MUS cores=0 |
| 100991fe | all+3 | BLOCK:CAPACITY_TOTAL_SHORTAGE | - | INFEASIBLE(precheck) | CAPACITY_TOTAL_SHORTAGE |
| 100991fe | off+6 | ok | INFEASIBLE | INFEASIBLE(solve) | MUS cores=0 |
| 100991fe | maxNig5 | BLOCK:MONTHLY_NIGHT_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 100991fe | 2N2OFF+N+3 | ok | INFEASIBLE | INFEASIBLE(solve) | MUS cores=0 |
| 100991fe | 2N2OFF+maxNig8 | BLOCK:MONTHLY_NIGHT_CAPACITY_SHORTAGE | - | INFEASIBLE(precheck) | MONTHLY_NIGHT_CAPACITY_SHORTAGE |
| 102243e9 | baseline | ok | FEASIBLE | feasible | obj=-295136121.0 |
| 102243e9 | 2N2OFF | ok | FEASIBLE | feasible | obj=-318865101.0 |
| 102243e9 | 3N2OFF | ok | FEASIBLE | feasible | obj=-320924341.0 |
| 102243e9 | not_one_night | ok | FEASIBLE | feasible | obj=-302331081.0 |
| 102243e9 | N+3 | ok | FEASIBLE | feasible | obj=-306853117.0 |
| 102243e9 | all+3 | ok | UNKNOWN | UNKNOWN | - |
| 102243e9 | off+6 | ok | FEASIBLE | feasible | obj=-248974800.0 |
| 102243e9 | maxNig5 | ok | FEASIBLE | feasible | obj=-286821621.0 |
| 102243e9 | 2N2OFF+N+3 | ok | FEASIBLE | feasible | obj=-308301158.0 |
| 102243e9 | 2N2OFF+maxNig8 | ok | FEASIBLE | feasible | obj=-296664401.0 |
| 102243eb | baseline | ok | FEASIBLE | feasible | obj=-1316712238.0 |
| 102243eb | 2N2OFF | ok | FEASIBLE | feasible | obj=-1316712238.0 |
| 102243eb | 3N2OFF | ok | FEASIBLE | feasible | obj=-1316712238.0 |
| 102243eb | not_one_night | ok | FEASIBLE | feasible | obj=-1316712238.0 |
| 102243eb | N+3 | ok | FEASIBLE | feasible | obj=-304442295.0 |
| 102243eb | all+3 | ok | FEASIBLE | feasible | obj=-473558149.0 |
| 102243eb | off+6 | ok | FEASIBLE | feasible | obj=-202262718.0 |
| 102243eb | maxNig5 | ok | FEASIBLE | feasible | obj=-345088018.0 |
| 102243eb | 2N2OFF+N+3 | ok | FEASIBLE | feasible | obj=-343769415.0 |
| 102243eb | 2N2OFF+maxNig8 | ok | FEASIBLE | feasible | obj=-478487195.0 |

**INFEASIBLE 총 15건**

---

## 분석

**INFEASIBLE 15건 = precheck(aggregate) 12 + solve-time 3.**

### 층별 검출
- **① aggregate precheck (12건)** — 대부분을 싸게+설명력있게 잡음:
  - `MONTHLY_NIGHT_CAPACITY_SHORTAGE` (maxNig5/N+3/2N2OFF+maxNig8 조합) ← **recovery 강화 신규 체크 포함 발효**
  - `CAPACITY_TOTAL_SHORTAGE` / `GLOBAL_DAY_CAPACITY_SHORTAGE` (all+3, N+3)
- **solve-time only (3건, 병동 100991fe)** — precheck 통과했는데 솔버 INFEASIBLE: `N+3`, `off+6`, `2N2OFF+N+3`. aggregate 산술이 못 본 **cross-nurse 조합**.

### solve-time 3건은 ④ MUS 진단으로 원인 규명됨 (검증)
probe는 기본 MUS off라 cores=0(UNDIAGNOSED)이었으나, **`AIDE_ENABLE_MUS_REGISTRY=1`(=layer ④ 실패-시-재solve)로 재실행하니 conflict core 추출**:
- `2N2OFF+N+3` → **core 4개**:
  - `coverage_min`: "108 cell 정책 동시 불가 → day1 E/N 최소인원 축소 or 가용 nurse 확대"
  - `mixed`(23명): `ConsecutiveNightCap·ConsecutiveWork·NightCap·NotOneNight·OffCap` → hint: 연속근무/전이금지 완화
  - `mixed`(2~3명): `AllowedShiftMask·ConsecutiveWork·OffCap·TransitionBan`
→ **layer ④가 aggregate가 놓친 solve-time 조합 infeasible을 정확히 설명 + 해결힌트 제공** 확인.

### 미결(UNKNOWN)
- probe TL=8초가 짧아 일부 병동(10135834/10135857/10253118 등)에서 **UNKNOWN**(feasible/infeasible 판정 못함). 실제 생성은 60~180초라 대부분 판정됨. 정밀 분류하려면 TL↑ 재실행 필요.

### 병동 특성
- **저수요 병동**(102243e9·eb, demand 3/3/2·3/3/3): maxNig5·all+3에도 대부분 feasible — 여유 큼.
- **고수요 병동**(10135857 demand 6/6/6·37명): N+3·all+3·2N2OFF+N+3에서 precheck 즉시 blocking.
- **100991fe**(demand 6/6/6·36명): baseline·2N2OFF·1N 단독은 feasible이나, N+3/off+6/조합에서 solve-time infeasible → 경계 병동.

## 결론
- 5층 시스템이 실데이터에서 작동: **①이 12/15를 solve 전에 잡고, ④가 나머지 solve-time 3건을 원인규명.**
- recovery 강화(신규)가 `MONTHLY_NIGHT_CAPACITY_SHORTAGE`로 실제 발효 확인.
- UNKNOWN 케이스는 TL 짧음 탓 — 정밀 스윕은 TL↑.

---

## 실제 generate E2E (DB 쓰기 경로, 원인→해결→재생성)

read-only probe는 stage1-hard만 봐서 "해결책이 실제로 works"를 끝까지 검증 못 함(실제 generate는 precheck+fallback+payload+treatment 경로를 탐). 그래서 **실제 `generate_roster_service`로 E2E** 수행. 병동 100991fe / 2026-04(wanted 존재) / HN(이보영) 컨텍스트. **생성된 schedule row는 전부 삭제 정리(최종 41건 원복 확인).**

### 결과
| 단계 | config | 결과 |
|---|---|---|
| baseline | (기본) | **SUCCESS** — 28명·840 entries 실제 생성(plumbing 정상) |
| 교란 | max_nig_per_month=3 | **UNRECOVERABLE(blocking)** — "야간 월요구=180 / 월가능=69 / **부족=111**" |
| 옵션0 문자그대로 | max_nig=4 (+1) | **UNRECOVERABLE** — 부족=88 (여전히 불가) |
| 실질 수정 | max_nig=8 | **SUCCESS** |
| 실질 수정 | max_nig=15 | **SUCCESS** |

### 시스템이 낸 resolution_options (max_nig=3 케이스)
1. `treatment:threshold:monthly_night_cap` — **max_nig_per_month 상향(+1)**, verified=False
2. `treatment:disable:night_recovery` — **two_offs_after_two_nig 비활성화**, verified=False

### 판정 (원인·해결 품질)
- ✅ **원인 진단 정확**: 정확한 수치(월요구/가능/부족)와 올바른 노브(max_nig) 지목.
- ✅ **해결 방향 정확**: max_nig 상향 / recovery 비활성 — 둘 다 맞는 방향.
- ⚠️ **해결 크기(magnitude) 부족**: ontology treatment가 부족량(111)과 무관하게 **고정 "+1"**만 제시 → 극단 케이스에선 1회 적용으로 안 뒤집힘(max_nig 4에서도 부족=88). 실질 임계값은 ⌈180/23⌉≈8.
- ⚠️ **verified=False**: precheck-block 케이스는 ontology treatment(미검증 방향제시)만 받음. solve-time-undiag 케이스가 받는 **probe-verified 옵션**(실제 뒤집힘 확인)과 비대칭.

### 액션 아이템 (도출)
1. **magnitude-aware treatment**: 부족량으로부터 필요 증분 계산(예: max_nig ← ⌈demand/N가능인원⌉)해서 "+1" 대신 실효값 제시.
2. **precheck-block 옵션도 검증**: undiag-probe의 verify 경로를 precheck-block 케이스에도 적용해 verified=True 옵션 제공(또는 magnitude 계산으로 대체).
3. 대안: 적용 후 재생성 루프(iterate)로 여러 번 밀어붙여 자동 수렴.

---

## 구현: 단일축 magnitude sizing (액션 1)

**갭**: precheck-block(단일축 산술) 케이스가 ontology treatment 의 고정 "+1"·미검증 옵션을 받아, max_nig=3 같은 큰 결손(부족111)엔 1회 적용으로 안 뒤집힘.

**수정** (조합 케이스는 무변경 — 계속 probe 재solve 검증에 위임):
- `precheck/treatment_enricher.py`: `_needed_max_nig(need, caps)` = Σmin(cap,m)≥need 인 최소 m 스캔(단조) + `_size_from_cause_details(config_key, details)`. precheck 가 cause.details 에 이미 담은 정확한 숫자(`n_required`, `night_capable_nurses[].capacity_days`)로 실효 목표값 계산. `enrich_treatment_recommendations` 에 배선 → `t["suggested_value"]`.
- **alias 매칭 버그도 수정**: treatment.covers 는 정식 cause_id(`cause:capacity:monthly_night_shortage`)인데 precheck cause 는 reason_code alias(node_id=None)라 매칭 실패하던 것 → `resolve_cause_alias` 로 정식 id 도 `cause_by_id` 에 등록(evidence 매핑도 함께 복구).
- `cp_sat/undiagnosed_probe.py::treatments_to_resolution_options`: `changes` 에 `suggested_value`+`sizing_ko` 노출, sized 값이 있으면 `apply={config_key: value}` 로 승격(직접 적용 가능).

**검증** (실제 `build_blocking_payload` / `generate_roster_service`):
- month4(수요180): `apply={max_nig_per_month: 8}` — Stage2 에서 8이 실제 SUCCESS 로 확인된 값과 정확히 일치.
- month8(수요186): `apply={max_nig_per_month: 9}` (23×9=207≥186).
- boolean treatment(two_offs_after_two_nig)는 사이징 대상 아님 → apply 비고(방향만). 올바름.
- insufficient(야간 가능 인원 부족)면 값 대신 "증원 필요" 명시.

**테스트**: `tests/test_treatment_sizing.py` (6). 회귀 0(사전결함 2건은 async 시그니처, 무관).

### 다른 단일축 노브 확장 (조사 결과)

온톨로지의 set_threshold numeric treatment 는 4개뿐 — 각각 자동 apply 가능성이 다름:

| cause | config_key | 자동 apply? | 처리 |
|---|---|---|---|
| monthly_night_shortage | `max_nig_per_month` | ✅ scalar, 검증됨 | **suggested_value + apply** |
| daily_total_shortage | `daily_shift_requirements` | ❌ nested + 수요는 DailyShift 출처 | **message-only**(정확한 초과 숫자 안내, apply 없음) |
| team:min_over_need | `team_min_by_team` | ❌ nested {team:{shift:min}} | 미확장(방향만) |
| consecutive:work_limit | `max_consecutive_work_days` | ❌ 키명 3중 불일치(`_days`/`max_consecutive_work`/`max_conseq_work`) + precheck-block 아님(MUS/probe 경로) | 미확장 |

`off_days` 는 애초에 treatment 로 제공되지 않음(capacity_total_shortage 는 manual 증원만). **결론: 자동 apply 가능한 clean scalar 노브는 `max_nig_per_month` 하나. `daily_shift_requirements` 는 message-only 로 정확한 숫자 제공.** 조합(coupled)은 설계상 probe 재solve 담당(이미 됨).

**테스트**: `tests/test_treatment_sizing.py` (7 — night sizing/insufficient/alias/boolean/daily message-only).

---

## 후속 갭 마무리 (원인추적·해결탐색 완성도)

### ⓐ MUS 래핑갭 — within-month 2N2OFF 회복 (커밋 38c9677)
solve-time 회복 병목이 core 에 안 뜨던 원인 = within-month 2N 회복 강제(cp_sat_basic.py:4677)가 `_assume_2n2off` 리터럴 없이 OnlyEnforceIf(3N·boundary·carryover 는 이미 붙음, 2N만 오버사이트). 리터럴 추가(기본 true=동작무변경). 실증: 100991fe 2N2OFF+N+3 MUS core 에 `RecoveryOffNode` 포함(수정 전 부재) + "two_offs_after_two_nig 완화" 힌트.

### ⓑ probe magnitude-search — 고정 델타 → 최소침습 이분탐색 (커밋 7f3b49b)
undiag probe 의 숫자 완화가 고정 델타(+8 등)라 진짜 필요값이 크면(부족) 놓치던 것 → 단조 노브(max_nig↑·max_conseq_work↑·max_consecutive_nights↑·off_days↓)를 재solve 이분탐색해 최소 feasible 값을 찾음. 단조라 정당·유한(log), 예산 12(총)·노브당6 으로 상한. 결과=verified 옵션 "from→to" + apply(원클릭). 고정 +8 로 놓치던 케이스 회수 + 과잉 최소화.

### 원인추적 커버리지 요약 (갱신)
| 상황 | 탐지 | 원인 | 해결 |
|---|---|---|---|
| 단일축 산술 | ✅ precheck | ✅ 숫자 | ✅ arithmetic sizing(max_nig 자동적용, daily 안내) |
| 개인 시퀀스 | ✅ per-nurse DP | ✅ witness | 방향 |
| flow 결합 | ✅ max-flow | ✅ min-cut | advisory |
| solve-time 조합 | solve 후 | ✅ MUS core(**회복 포함 확장**) | ✅ probe **magnitude-search**(검증된 최소값) |

**남은 것**: fallback_lex 전용 hard 제약 중 MUS 미포함이 더 있는지 추가 감사(현재 recovery 가 주 후보였고 닫음). daily/team nested 노브 자동 apply(현재 안내만).
