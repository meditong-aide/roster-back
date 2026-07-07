# 퇴사자 대응 부분 재생성(Partial Re-solve) 설계

> 상태: 설계 확정 (구현 전)
> 관련 인프라: `fixed_cells` 하드핀, fallback_lex objective, replacement_recommend_service(참고), Infeasibility Resolver, Grade Soft Fallback

## 1. 문제 정의

특정 근무자가 갑자기 퇴사했을 때, 이미 확정·공지된 근무표를 **처음부터 다시 생성하지 않고** 필요한 부분만 다시 푼다.

요구사항:

- **cutoff 이전은 동결**한다. 퇴사일(cutoff) 이후만 변경한다.
- 원티드는 최대한 지킨다.
- 해당 근무표에 적용된 roster config 제약(연속근무, 개인별 나이트 개수 등)을 지킨다.
- **NOD/NOE(커버리지 부족) 구멍을 남기지 않는다** — 퇴사자 자리를 실제로 메운다.
- GRADE·Team은 최상위(하드)로 지킨다. 정말 안 되면 그때만 부족을 허용하고 원인을 진단한다.
- **현재까지의 통계(per-nurse 나이트/OFF 분포, 공정성)를 깨지 않는다.**
- 남은 인원의 근무 **변경을 최소화**한다.
- 결과는 **미리보기 diff 후 승인**으로 반영한다 (auto-apply 금지).

## 2. 핵심 통찰: "재생성"이 아니라 "prefix 동결 + 부분 재최적화"

날짜 범위를 잘라 별도로 푸는 방식이 아니라, **전체 월 모델을 그대로 유지**하면서 셀 단위 하드핀으로 자유도를 제어한다.

기존 솔버는 `fixed_cells` `(nurse_index, day_index) → shift` 를 하드 등식 제약으로 강제한다.

- 소비 지점: `app/services/cp_sat_basic.py:2591` (`for c in getattr(rs,'fixed_cells',[])`)
- 강제 예: `app/services/cp_sat_basic.py:3544` (`m.Add(X(n, d, off) == 1)`)

이를 이용해:

| 셀 그룹 | 처리 |
|---|---|
| cutoff **이전** 모든 셀 | 원래 값으로 `fixed_cells` 핀 (동결) |
| 퇴사자의 cutoff **이후** 셀 | OFF로 핀 (공급에서 제거) |
| 나머지 간호사의 cutoff **이후** 셀 | 자유 변수 (여기만 재최적화) |

이 구조의 이점:

1. config 제약(`max_conseq_work`, `max_nig_per_month`), coverage(NOD/NOE), GRADE hard, Team hard, 원티드 objective, **공정성 항이 전부 그대로 재사용**된다. 신규 solver 로직 최소.
2. cutoff 경계의 연속근무·나이트회복 히스토리가 **동결 prefix에서 자동 계산**된다 (별도 prev-tail 배선 불필요 — 같은 모델 안).
3. 자유 변수가 줄어 **full generate보다 빠르다** (Stage2 탐색공간 축소).

## 3. 우선순위 (Lexicographic) — 실제 엔진 기준

메인 solve 경로는 **fallback_lex의 3-stage 서열**이다. (`SKIP_PRIMARY` 기본값 `"1"` → primary 단발 solve를 건너뛰고 바로 fallback_lex 진입, `cp_sat_basic.py:1480`.) 따라서 우선순위는 이 엔진의 stage 구조를 그대로 따른다 (`fallback_lex.py:146-149`):

```
[전 stage 공통 하드]  GRADE + Team_min + fixed_cells 핀   ← 불가능 시 INFEASIBLE → 진단
        │
Stage 1 (최우선)   커버리지 부족(NOD/NOE) 최소화
Stage 2 (그 다음)  안전/법규 위반 최소화
                   (연속근무·월간나이트·N→D/E 전이·2N2O 회복·야간전담·주2OFF)
Stage 3 (최하위)   품질/선호 최대화 — 원티드 + 공정성(나이트/OFF 균등, KLD) + ★변경최소화 앵커★
```

각 stage는 이전 stage의 최적값을 **고정한 채** 다음을 푼다 (특히 Stage 2에서 위반=0인 셀은 Stage 3에서 0으로 잠금).

### 이 순서가 본 기능에 유리한 이유

- **커버리지가 Stage 1(최우선)** → "NOD/NOE 구멍 안 남긴다"가 엔진 기본 동작. 별도 조정 불필요.
- **연속근무·나이트는 Stage 2 소프트 슬랙**(하드 아님) — 커버리지 다음, 원티드 위. 물리적으로 불가능할 때만 완화.
- **변경최소화 앵커·공정성·원티드가 모두 Stage 3** → 앵커가 자연스럽게 최하위 tie-breaker가 된다.

### 원티드는 Stage 3인데 괜찮은가 (중요)

엔진 네이티브 순서는 **커버리지 > 안전 > 원티드**라, 원티드는 낮은 우선순위다. 그러나 본 기능에서는 문제되지 않는다:

- **원본 근무표가 이미 원티드를 지킨 상태**로 생성돼 있다.
- 우리는 prefix를 동결하고 suffix를 원본에 **앵커링**한다.
- 따라서 원티드는 Stage 3의 원티드 목적항이 아니라 **앵커(원본 근접)가 지켜준다.** 앵커와 원티드가 같은 Stage 3라 서로 충돌하지도 않는다.
- 원티드가 실제로 희생되는 경우는 **그것을 지키면 커버리지 구멍이 나는 상황뿐**이다 (Stage 1이 상위). 이는 "정 안 되면 어쩔 수 없이 조정"이라는 의도와 일치한다. 커버리지를 다른 사람으로 채울 수 있으면 Stage 1 최적해가 여러 개이고, Stage 3(원티드+앵커)가 원티드를 지키는 해를 고른다.

### Stage 3 내부 weight 서열

Stage 3 안에서는 **공정성(통계 보존) > 앵커(변경최소화)** 로 둔다. 원본이 이미 공정하므로 공정성 항은 "새로 생긴 퇴사자 물량만 고르게 분산"하는 역할이라 앵커와 크게 충돌하지 않는다. 앵커를 공정성보다 위에 두면 "통계를 깨면서까지 안 건드리기"가 생기므로 금지.

### 튜닝 노브

커버리지(Stage 1)와 앵커(Stage 3) 사이를 완전 서열 대신 **가중합**으로 완화하면 "싸게 메울 수 있으면 메우고, 비싸면 최소 변경" 같은 중간 정책이 가능하다. 기본은 엔진 네이티브 서열 유지, 필요 시 노브로 전환.

## 4. 컴포넌트

### 4.1 신규 (2곳)

**① 변경 최소화 앵커 목적항 (신규 solver term)**

변수 `X[n,d,s] ∈ {0,1}`, 셀마다 `Σ_s X[n,d,s] = 1`. 원본 시프트를 `s*(n,d)`라 하면 **`X[n,d,s*]`가 곧 "원본 유지" 지시자**다 (새 변수 불필요). 자유 셀 `F = {(n,d): n≠퇴사자, d≥cutoff}`.

*형태 A — 셀 단위 변경 수 (Hamming)*
```
ChangeCells = Σ_(n,d)∈F (1 − X[n,d,s*(n,d)])
목적항: + W_cell · Σ_(n,d)∈F X[n,d,s*(n,d)]   (원본 유지 보상)
```

*형태 B — 건드린 인원 수*
```
touched_n ≥ 1 − X[n,d,s*(n,d)]   ∀ d s.t. (n,d)∈F
NursesTouched = Σ_n touched_n
```

*권장: 하이브리드 (사람 우선, 칸 보조)*
```
목적항 = − W_nurse · NursesTouched − W_cell · ChangeCells
         (W_nurse ≫ W_cell, 둘 다 공정성 weight 아래)
```
A만: 변경이 여러 명에 흩어짐. B만: 한 명에 몰림. 하이브리드 = "되도록 적은 사람만, 그 안에서 총 변경 칸 최소" → "확정 근무표 최대한 안 건드림"에 대응.

*옵션: 변경 종류별 가중치 (여전히 선형)*
```
CellPenalty(n,d) = Σ_s c(s*(n,d), s) · X[n,d,s]
  c(a,a)=0, c(OFF,근무)=高, c(D,E)=低 …
```
형태 A는 모든 c=1인 특수형. 초기엔 A로 시작, 필요 시 c 테이블로 정교화.

*구현*
- **Stage 3** 목적항으로 추가 (`build_fallback_stage3_objective_terms`, `app/services/cp_sat/fallback_objectives.py`). Stage 1(커버리지)·Stage 2(안전)는 손대지 않는다.
- Stage 3 내부 weight: 공정성 항보다 아래 (공정성 > 앵커).
- warm-start `AddHint(X[n,d,s*], 1)` 재사용 (`fallback_lex.py:2964`, `:3257`) → 원본 근처 출발 + 수렴 가속.

*검증*: solve 후 `NursesTouched`/`ChangeCells` 값을 리포트. 원본 entries vs draft entries Hamming 비교로 교차검증. 전체 재생성 대비 ChangeCells 감소 배수로 효과 정량화.

*합성 보장 (중요): 앵커는 독립 loss가 아니라 기존 목적함수에 얹는 "한 개 항"*

최소변경은 **Grade·Team·원티드·설정기준(연속·나이트)·개별속성을 모두 지킨 조건 하에서의 최소변경**이다. 기존 Stage 3 목적함수(원티드 + 공정성 + grade cascade + team_min)를 그대로 두고, 그 합에 앵커 한 항만 더한다:
```
maximize [ 기존 Stage3 항 전부 ] + ( − W_nurse·NursesTouched − W_cell·ChangeCells )
subject to  S1 잠금(커버리지=최적, 등식) · S2 잠금(ND/NE·연속·월나이트·회복=최적, 위반0셀 고정)
            · 항상 하드(fixed_cells, Σ_s X=1, join/leave, grade/team hard)
```
- **하드는 하드대로.** 앵커는 Stage 3에 살아 S1/S2를 못 건드린다. 특히 Stage 3는 "위반=0 셀 0 고정 + 새 위반 생성 금지"(`fallback_lex.py:149`)라 **앵커가 ND/NE를 새로 만들어 변경을 줄이는 것은 불가능**. ND/NE가 0인 해공간 안에서만 tie-break.
- **weight 조건 (tie-breaker 보장)**: `W_cell·(자유셀 최대수) < 기존 S3 최소 weight`. 미충족 시 앵커가 원티드/공정성을 이겨 "원본 유지하려 원티드 희생"이 생김.
  - (a) weight 바운딩 — 간단, 초기 채택.
  - (b) 별도 Stage 3.5 lex 패스(원티드·공정성 고정 후 앵커만 최소화) — tie-breaker를 수학적으로 100% 보장, 회귀 시 승격.

**② 오케스트레이션 서비스** `partial_resolve_on_resignation(schedule_id, resigned_nurse_id, cutoff_date)`

1. 기존 `ScheduleEntry` 로드 → cutoff 이전 셀을 `fixed_cells`로 변환 (`cp_sat_basic.py:2591` 소비 포맷 그대로).
2. 퇴사자를 cutoff 이후 공급에서 제외 → **엔진 기존 `resignation_date` 메커니즘 재사용** (`fallback_lex.py:216-232`이 `nu.resignation_date` 이후 변수 생성을 스킵). 별도 OFF 핀 불필요. 퇴사자의 cutoff 이후 fixed_wanted/특수근무·프리셉터 관계는 이 단계에서 정리(§6.3, §6.4).
3. 원본을 hint + anchor로 주입, 기존 생성 파이프라인 재호출.
4. 결과를 **정상 생성과 동일하게 새 draft Schedule로 저장** (별도 승인/발행 게이트 없음 — "추가 생성"과 같은 취급).
5. **diff 산출** (누가·언제·무엇→무엇)은 관리자가 변경을 **보는 정보용**. 발행은 기존 draft→issued 흐름 그대로.

### 4.2 재사용 (신규 코드 0)

- 셀 동결: `fixed_cells` 하드핀
- GRADE/Team 하드: `app/services/cp_sat/fallback_lex.py:2624-2658`
- 연속근무·나이트 상한: config `max_conseq_work` / `max_nig_per_month` (`cp_sat_basic.py:462-475`)
- coverage slack: `coverage_soft_penalty_weight` (`fallback_lex.py:537`)
- 원티드: `NurseShiftRequest` score → objective
- 공정성(나이트/OFF 균등, KLD grade balance): 기존 fallback objective 항

### 4.3 신규 스킬 `resolve-resignation`

CLAUDE.md agentic 원칙 준수 — self-describing description, 내부 grounding(퇴사자 이름→id, "3월 15일"→date를 스킬 내부에서 해석). auto-apply 안 하고 diff 제안 반환 (현행 `repair-schedule` 철학과 일치, `app/agents_v2/skills/repair_schedule.py:15-19`).

## 5. 백필 정책

**내부 재분배만** (확정). 공급 집합 = 해당 병동 기존 nurse. 타병동/파견 인력 투입 없음 → 구현 최단.

## 6. 리스크

### 6.1 퇴사로 GRADE/Team 바닥이 부족 — 엔진은 터지지 않고 "조용히 degrade"

코드 확인 결과 grade/team 둘 다 인원 부족으로 **INFEASIBLE 나지 않는다**. 대신 부드럽게 무너지며, 문제는 그 degrade가 **조용하다(silent)**는 것.

**Grade — 이중 cascade (`grade_constraints.py`)**
- ① 인원 0 등급 이양(`_cascade_constraints_to_existing_grades`, :296): grade-1 인원이 없으면 "시니어 ≥N" 요구가 grade-2 → 3으로 이양. "설정 등급 부재"로 헛 INFEASIBLE 방지.
- ② 누적 등급 cascade(`_GRADE_CASCADE_ENABLED=True` 기본, `_add_minimum_constraints`, :543-596): 요구 등급을 못 채우면 하위 등급이 대체하되 페널티 급증(off0=160K → off1=2M → off2+=6M, near-hard). **하드가 아니라 slack 페널티** → 해는 나오고 등급 부족은 큰 페널티로 표시.
- `allow_soft_fallback` 플래그는 이 cascade가 꺼진 **레거시 경로**(`_GRADE_CASCADE_ENABLED=False`)에서만 유효. 현행 기본에선 무관.

**Team_min — 용량 부족 시 skip (`team_constraints.py:116-128`)**
- 기본 하드(`team_min_soft_fallback` default False)지만, **하드 모드라도 팀 활성 인원 `< min_t`이면 그 (팀,일,시프트) 제약을 skip**(`skipped_capacity`)해 INFEASIBLE을 방지한다. 퇴사로 팀이 인원 부족해지는 케이스가 정확히 skip 대상.
- 결과: **INFEASIBLE 안 남. 대신 팀 최소가 조용히 미달된 채 넘어감.**
- 진짜 INFEASIBLE은 "인원은 충분한데 다른 제약과 충돌"하는 드문 경우뿐.

**따라서 precheck의 역할 = 막기가 아니라 "가시화"**
- solve 후, grade near-hard 페널티가 걸린 셀(6M short)과 team `skip-capacity` 로그를 수집해 **diff에 명시적 경고**로 노출: "3/20 팀2 나이트 최소 미달", "grade-1 1명 부족".
- 관리자가 팀 재배정/외부 인력 여부를 판단. 조용히 넘어가지 않게 하는 것이 핵심.
- 드문 진짜 INFEASIBLE만 max-flow 원인 진단(Infeasibility Resolver)으로 설명.

### 6.2 draft 저장 — 기존 메커니즘으로 해결 (Phase B 완료, 신규 테이블 0)

조사 결과 미리보기·승인에 필요한 구조가 **이미 전부 있다**:

- **`schedules`** (`db/models.py:376-394`): `version`(BIGINT) + `status`(`'draft'`|`'issued'`) + `dropped`(bool). 같은 (group, year, month)에 여러 버전 공존 가능.
- **`ScheduleEntry`**는 `schedule_id`로 키됨 → **새 schedule_id = 완전히 독립된 셀 집합**. 원본 덮어쓰기 불필요.
- **생성 파이프라인이 이미 새 draft를 만든다**: `Schedule(..., version=latest_version+1, status='draft')` (`roster_create_service.py:5961-5970`). 즉 정상 생성도 원본을 안 덮고 새 draft를 쌓는 구조.
- **조회 우선순위**: `status='issued' > 최신 version(draft)` (`roster_create_service.py:1664-1703`).
- **발행**: `IssuedRoster`(office,group,version 복합PK, is_active) + `IssuedRosterSnapshot`(roster_json 등 전체 스냅샷) (`db/models.py:639-688`).

**전략 (신규 저장소 없음)**:
1. 부분 재생성 결과를 **기존 생성과 동일하게 새 draft Schedule**로 저장 (freeze+anchor만 다름). 원본 issued 무손상.
2. **diff** = 원본 issued의 `schedule_entries` vs draft의 `schedule_entries` (둘 다 schedule_id·nurse_id·work_date 키).
3. **승인** = draft→issued 승격(기존 발행 흐름 + IssuedRoster/snapshot). **거부** = draft `dropped=True`.

**preview 계약 선례**: `ward_redistribute_service.preview_ward_redistribution`(`:323`)이 이미 "read-only 변경셋 반환, DB 무변경" 패턴 + 프리셉티↔프리셉터 짝 처리(`_detect_ward_pairs`)를 갖고 있다. diff/preview API 형태는 이를 따른다.

**권장**: 생성이 비동기(SQS)라 워커가 draft를 영속화하는 흐름이 자연스러움 → *persist-as-draft* 방식. **별도 승인/발행 게이트는 만들지 않는다** — 재생성은 "추가 생성"과 동일하게 새 draft를 쌓고, 관리자는 기존 draft 조회/발행 UI로 확인·발행한다. diff는 정보 표시용이지 게이트가 아니다. (거부는 기존 `dropped=True` 재사용.)

### 6.3 퇴사자가 프리셉터인 경우 — period 테이블로 재지정

관계가 SSOT period 테이블 `nurse_preceptee_period`로 모델링돼 있고(`db/models.py:296-313`), `end_reason`에 이미 `preceptor_transfer`가 정의돼 있다. 따라서 "다른 프리셉터로 남은 기간 진행"이 **기존 메커니즘으로 지원된다** (`app/services/preceptee_period.py` 패턴 재사용).

분기:
- preceptee의 현재 period `valid_to`가 **cutoff 이후까지 유효** → 기존 row를 `valid_to = cutoff`로 끊고(`end_reason=preceptor_transfer`), 새 preceptor_id로 `[cutoff, 원래 valid_to)` 새 row 작성 → 남은 기간 새 프리셉터로 진행.
- preceptee period가 **cutoff 전에 이미 종료**(`valid_to <= cutoff`) → **아무것도 안 함** (관계 무관).

새 프리셉터 선정은 1차로 **관리자 선택**(같은 팀·시니어 등급 후보 제시). 자동 선정은 후속. 미처리 시 preceptee의 cutoff 이후 프리셉터 follow 고정(`cp_sat_basic.py:1632`)이 부재 프리셉터를 가리켜 INFEASIBLE/오고정 발생하므로, 재지정 또는 follow 해제는 필수.

### 6.4 퇴사자의 cutoff 이후 승인된 fixed_wanted / 특수근무

퇴사로 무효가 되어야 한다. `FixedWantedEntry`·특수요청을 cutoff 이후 구간에서 drop. 미처리 시 "퇴사자가 근무하는" 고정 셀이 남아 모순.

### 6.5 post-solve 후처리 — 현재 대부분 비활성 (리스크 하향)

확인 결과 OFF 재배치는 **현재 돌지 않는다**: `postprocess_rebalance_off_fn`이 no-op 람다로 주입되고(`cp_sat_basic.py:2182`), 호출부도 주석 처리(`fallback_lex.py:3409`), `trim_extra_offs`도 주석(`cp_sat_basic.py:1503`). → 동결 prefix를 건드릴 위험 대부분 소멸.

남는 것은 `fixed_wanted` M-overlay(근무형 고정 셀이 커버리지 솔버 우회 후 post-solve overlay되는 기존 경로, 메모리 "Fixed-Pin Coverage Bypass"). 본 기능에선 **diff를 후처리까지 끝난 최종 materialized 표 기준으로 산출**하면 overlay 결과가 그대로 반영되어 문제없다. 향후 post-solve가 다시 켜지면 suffix·비동결 셀 제한을 재검토.

### 6.6 OFF quota 미달 / 연속근무·나이트 소폭 초과

퇴사자 물량 흡수로 남은 인원의 OFF가 월 기준 미만이 되거나 나이트가 +1 될 수 있다. 커버리지(Stage 1)가 상위라 Stage 2가 최소화하되 물리적으로 불가피하면 일부 희생된다. **정상적 degradation** — diff·경고에 노출해 관리자가 판단.

## 7. 시나리오 스트레스 테스트

| # | 상황 | 예상 동작 | 판정 |
|---|---|---|---|
| A1 | 퇴사자 D/E 위주, 병동 여유 有 | OFF/여유 인원이 빈칸 흡수, Stage3 앵커로 나머지 최소변경. diff 소규모 | ✅ 설계대로 |
| A2 | 퇴사자 나이트 담당 | 나이트 가능·여유자 채움. Stage2 전이/상한 최소화 + Stage3 공정성으로 몰빵 방지 | ✅ (나이트 인력 빠듯하면 누군가 +1, diff 표시) |
| B1 | cutoff 경계에서 누군가 4연속 근무 중 | prefix가 fixed_cells로 같은 모델에 있어 연속근무 제약이 경계 정확 인식 → 자동 방지 | ✅ 동결 방식의 강점 |
| B2 | cutoff이 월초(1~2일) | 사실상 전체 재생성에 근접, 느리고 변경량↑ (원본이 퇴사자 포함) | ⚠️ 성능·변경량 주의, 버그 아님 |
| B3 | cutoff이 월말(28~31일) | 자유일 거의 없어 재배치 여력 부족 → 커버리지 홀 잔존 가능 | ✅ 정상 degradation, diff+진단 노출 |
| C1 | 퇴사자가 특정 등급 유일 인력 | INFEASIBLE 아님. grade cascade(인원0 이양 + 누적 near-hard 6M 페널티)로 degrade → **조용한 등급 미달** | ⚠️ 미달 가시화(§6.1) |
| C2 | 퇴사자가 팀 유일 인력 | INFEASIBLE 아님. team_min이 용량 부족 시 skip → **조용한 팀 미달** | ⚠️ skip 로그 가시화(§6.1) |
| C4 | 인원 충분한데 다른 제약과 충돌 | 드물게 진짜 INFEASIBLE → max-flow 원인 진단 | ⚠️ 진단 경로 |
| C3 | 소인원 병동, 여러 날 커버리지 need 미달 | Stage1 소프트라 INFEASIBLE 아님. 최대 충족 후 홀 남김 → 외부 인력은 관리자 판단 | ✅ 내부재분배만 결정과 일치 |
| D1 | 남은 간호사 원티드-OFF 날에 커버리지가 그를 필요로 함 | 다른 사람으로 커버 가능 → 원티드 지킴(Stage3). 불가능 → 원티드 깨고 투입(Stage1) | ✅ 의도와 일치 |
| D2 | 원본이 약간 불균형(누군가 나이트 과다) | 공정성 항이 리밸런싱 시도 → 변경↑. 앵커가 억제. Stage3 weight로 조절 | ⚠️ 노브(§3 Stage3 서열) |
| E1 | 퇴사자가 프리셉터 (period 유효) | `nurse_preceptee_period` 재지정(preceptor_transfer)으로 남은 기간 새 프리셉터(§6.3) | ✅ 기존 period 메커니즘 |
| E2 | 퇴사자가 프리셉터 (period 이미 종료) | 관계 무관 → 아무 처리 안 함 | ✅ no-op |

## 8. 구현 순서

- **A. 설계 문서화** (본 문서) — 완료
- **B. draft/버전 메커니즘 조사** → 저장 전략 확정 (§6.2) — 완료(기존 draft/version/issued 재사용, 신규 테이블 0)
- **C. 앵커 목적항 PoC** — fallback objective에 변경 페널티 term 실제 삽입, 소규모 재현
- **D. 오케스트레이션 서비스 + diff 산출**
- **E. `resolve-resignation` 스킬**
- **F. 회귀 검증** — 파라미터→조건 적용→결과 품질 3단계, 통계 보존 확인

## 9. 미해결/추후 결정

- 커버리지 vs 변경최소화: 완전 서열 유지 vs 가중합 노브 (§3 튜닝 노브)
- cutoff 경계 semantics: cutoff = 퇴사일 (퇴사일 포함하여 그날부터 변경, 전날까지 동결) — 구현 시 최종 확정
- ~~draft 저장 전략~~ → §6.2에서 확정 (기존 draft/version/issued 재사용)
