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
  - `graph_export.json` (ontology mapping seed)

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
- This is v1 runner; extend evaluators incrementally as new checklist rules are implemented.
