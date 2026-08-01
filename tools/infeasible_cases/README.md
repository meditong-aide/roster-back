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
