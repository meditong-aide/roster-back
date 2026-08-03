# Infeasible 케이스 코퍼스 — 오프라인 진단·연구

라이브 생성이 **진단에 넘기는 입력 전부**(config 관련키 + per-nurse 속성 +
`initial_constraints`)를 JSON 스냅샷으로 두고, **DB·솔버 없이** 진단 스택을 재현·실험한다.

## 구성

| 파일 | 역할 |
|---|---|
| `cases/*.json` | 케이스 코퍼스 (라이브 캡처 + 합성) |
| `make_cases.py` | 다양한 계열의 **합성** 케이스 생성 |
| `replay.py` | 케이스 → 진단 스택 재현(classification·cert·카드·per-nurse 판정) |
| `../../app/services/ontology_graph/case_export.py` | 캡처/직렬화(라이브 훅 + `build_case`) |

## 케이스 JSON 스키마

```jsonc
{
  "meta":   { "group_id": "...", "source": "live-capture|synthetic", "name": "..." },
  "year": 2026, "month": 8, "num_days": 31,
  "config": {
    "daily_shift_requirements": {"D":2,"E":1,"N":1},
    "off_days": 9, "max_nig_per_month": 15,
    "two_offs_after_three_nig": true, "not_one_night": true, ...,
    "initial_constraints": {
      "forbidden":  { "<nurse_id>": { "<day_idx>": ["O","D",...] } },  // O=강제근무
      "forced_off": { "<nurse_id>": [ <day_idx>, ... ] }               // 강제 OFF
    }
  },
  "nurses": [ { "nurse_id","name","allowed_shifts","is_night_only",
                "n_exact","n_min","is_weekend_off", ... } ],
  "expected": { "classification": "...", "top_family": "...", "certificate": "..." }
}
```

## 재현

```bash
# 전체 케이스 진단 (요약)
.venv/bin/python tools/infeasible_cases/replay.py

# 한 케이스 상세(카드·per-nurse 판정)
.venv/bin/python tools/infeasible_cases/replay.py cases/synth-banned_recovery_blocked.json
```

`≠` 표시는 진단이 `expected` 와 불일치함을 뜻한다(회귀 감지).

## 합성 케이스 생성/변형

```bash
.venv/bin/python tools/infeasible_cases/make_cases.py
```
`make_cases.py` 의 `add(...)` 파라미터(금지 날짜·규칙·인원)를 바꿔 변형 실험. 현재 계열:

| 케이스 | 계열 | 원인 |
|---|---|---|
| `banned_4consecutive` | banned_wanted | N전담 4연속 O금지 > 최대3연속(3N2OFF) |
| `banned_recovery_blocked` | banned_wanted | NNN 후 회복 2OFF 자리 금지 |
| `banned_isolated_single_night` | banned_wanted | 고립 1N (not_one_night) |
| `personal_night_over_cap` | monthly_limit | n_min 13 > 월상한 7 |
| `weekend_off_bottleneck` | weekend_off | 주말휴무 과다 |
| `coverage_shortage` | coverage | 하루 수요 > 인원 |
| `feasible_control` | — | 대조군(진단 없음) |

## 라이브 실제 입력 캡처

생성 파이프에 게이팅 훅이 있다. `AIDE_DUMP_CASE=<dir>` 를 설정하고 (유효 HN 토큰으로)
infeasible 생성을 돌리면, 진단이 본 **효과적 입력 그대로** 그 폴더에 저장된다:

```bash
AIDE_DUMP_CASE=tools/infeasible_cases/cases \
  <유효 토큰으로 /roster_create/generate 호출 또는 in-process 실행>
# → cases/case-<YYYYMM>-<group>-<classification>.json
```

> 실제 캡처본(`case-*.json`)은 실 간호사 id/이름을 담는다. 외부 공유 시 익명화 권장.

## 왜 오프라인에서 되나

진단 함수(`explain_infeasibility_from_config`, `detect_banned_off_conflict`,
`per_nurse_night_feasible`, `cause_to_resolution_options`)는 **순수**(DB·솔버 불필요)라
케이스 dict 만으로 완결적으로 돈다. JSON 을 고치면 진단이 그대로 반응한다.

## Baseline 비교 실험

```bash
.venv/bin/python tools/infeasible_cases/baseline.py
```
같은 N축 부분문제에서 세 방법 정량 비교:

| 방법 | N축 원인 격리 | 비용 | 산출 |
|---|---|---|---|
| Tier1 (max-flow only) | 20% | 1 검사 | 시퀀스·결합 놓침 |
| QuickXplain (IIS) | 100% | ~9.6 oracle 호출 | 제약집합(비행동) |
| **branch-infer (우리)** | 100% | **1 진단** | 행동가능 typed certificate + proof + 검증복구 |

→ IIS 최소화의 반복 oracle 없이 sound·행동가능 원인. (실서비스 일반축은 oracle=솔버라
  branch-and-check로 확장; N축은 자기완결.)

> ⚠️ 한계(정직): 지금 QuickXplain 의 oracle 이 **우리 진단기 자신**이라 순환이다. "IIS 대비
> 우수"가 아니라 **내부 PoC 검증**일 뿐. 정식 비교엔 아래 독립 oracle + 실행시간 + 실코퍼스가
> 필요(`baseline_v2` TODO).

## 독립 exact oracle + hard-residual (VE 엔진의 존재 이유)

```bash
.venv/bin/python tools/infeasible_cases/exact_oracle.py    # oracle vs 우리 스택
.venv/bin/python tools/infeasible_cases/make_hard_residual.py  # 반례 생성(자기검증)
```

`exact_oracle.py` 는 우리 진단기와 **무관한** ground-truth 판정기(소형 인스턴스 backtracking).
우리 스택과의 결정적 차이 = **회복(recovery)은 실제 OFF** 여야 한다(우리 N/notN 오토마톤은
notN=D/E 도 회복으로 관대 인정). 그래서 다음을 **모두 통과**(우리 스택 전층 침묵)하는데도
정수 근무표가 infeasible 인 **hard-residual** 이 존재한다:

| 케이스 | per-nurse | max-flow | aggregate | joint-N DP | oracle | 우리 |
|---|---|---|---|---|---|---|
| `hard_residual_recovery_off_starvation` | ✓ | ✓ | ✓ | ✓feasible | **INFEAS** | 침묵 |
| `hard_residual_night_to_day_ban` | ✓ | ✓ | ✓ | ✓feasible | **INFEAS** | 침묵 |

이 gap(정수-결합 잔여)이 **variable-elimination / frontier DP 엔진**(미니 솔버 대체)의 대상이다.
oracle 은 소형(≤12일·≤8명) 전용 — 대형은 SKIP.

### frontier DP 엔진이 gap 을 닫음 (app/services/ontology_graph/frontier_dp.py)

`diagnose_frontier` 는 {D,E,N,O} 를 **exact** 로 판정하는 frontier DP(=variable elimination/
bucket DP)다. nurse×day 격자에서 일별 joint 상태 frontier 를 전개, 붕괴하면 **backpointer 로
원인 certificate** 추출:
- `recovery_off_starvation` — 야간 회복(실제 OFF)이 인원을 잠식해 그날 슬롯 미달.
- `joint_sequencing_collapse` — 인원은 되나 시퀀스·전이로 D/E/N 동시배정 불가.

검증: 두 hard-residual 모두 **INFEASIBLE_CERTIFIED 로 포획**, 독립 oracle(DFS)과 **불일치 0**.
multi_axis_diagnose 에 exact tier 로 배선(완화 층이 침묵할 때만 호출). 이제
`test_hard_residual.py` 는 "gap 닫힘"을 단언한다(relaxed joint-N DP 는 여전히 blind =
frontier DP 가 필요했던 이유를 함께 문서화).

경계: |frontier| 이 cap·전개예산 초과하는 **대형(넓은 separator)** 은 UNKNOWN 반환(무한루프
아님). 이 경우 **병목 window/간호사 component 로 분해**해 폭을 낮추면 exact 유지 — 다음 단계.
