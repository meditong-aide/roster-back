# Roster Harness Runner

Minimal executable harness for roster generation QA gate.

## What it does
- Loads checklist rules from `tools/harness/rules/checklist_core.yaml`
- Calls live APIs with `access_token` cookie
- Repeats roster generation (`/roster_create/generate`)
- Evaluates core metrics (coverage under/over, infeasible runs, etc.)
- Emits:
  - `run_result.json`
  - `summary.json`
  - `triage.md`
  - `graph_export.json` (ontology/hypergraph exchange payload)

## Usage

```bash
python tools/harness/runner.py \
  --base-url http://127.0.0.1:8000 \
  --token "<JWT_OR_BEARER_JWT>" \
  --year 2026 --month 5 \
  --strategy COMBINED \
  --repeats 5 \
  --strict
```

Optional:

```bash
python tools/harness/runner.py \
  --rules tools/harness/rules/checklist_core.yaml \
  --out-dir tools/harness/reports
```

## Notes
- If token is raw JWT, runner automatically also supports `Bearer <JWT>` format in cookie.
- Unknown metrics in YAML are reported as `SKIPPED` (not evaluated yet).
- `--strict` is enabled, `SKIPPED` + `severity=blocking` becomes FAIL.
- Runner exits with non-zero code when summary status is `FAIL` (CI gate friendly).
- `graph_export.json` now includes typed `nodes[]` and `edges[]` plus:
  - `mapping_summary` (`rules_total`, `mapped_rules_count`, `unmapped_rules`)
  - `consistency` (summary fail rules vs exported violations cross-check)
- For currently unsupported metrics, rule status can still be `SKIPPED`; with `--strict`, blocking SKIPPED causes FAIL.
- This is v1 runner; extend evaluators incrementally as new checklist rules are implemented.

## Case-test guardrail (important)

- 탐색/실증 케이스 작성 시 `max_conseq_work` 값은 변경하지 않는다.
  - 이유: 운영 정책 축(연속근무 상한)을 건드리면 결과 해석이 과도하게 왜곡되어
    원인 분류(coverage/eligibility/fixed/carryover) 비교가 어려워진다.
  - 신규 케이스는 우선 아래 축으로 구성:
    1) 팀/등급 최소 요구의 소폭 조정
    2) allowed shift / fixed wanted 분포 조정
    3) day/eve/night 수요의 소폭 조정

- ontology 확인 전제:
  - `/ontology`는 API generate 결과를 직접 읽지 않고,
    `tools/harness/reports/run-*/graph_export.json` 산출물을 읽어 렌더링한다.
  - 따라서 `/ontology/runs`를 채우려면 harness runner를 최소 1회 실행해야 한다.
