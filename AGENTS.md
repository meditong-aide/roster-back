# AGENTS Governance for Nurse Rostering Backend

## Project Context & Operations

### Business Context
- This repository provides nurse rostering APIs and optimization engines for hospital scheduling.
- The core objective is to produce feasible and fair rosters while preserving strict staffing and safety constraints.

### Tech Stack
- Python 3.13+ (`pyproject.toml`)
- FastAPI + Uvicorn API server (`app/main.py`)
- SQLAlchemy with MSSQL/Maria connectors (`app/db`)
- OR-Tools CP-SAT and optimization helpers (`app/services`)
- LangGraph/LangChain based analyzers (`app/agents`)

### Operational Commands
- Install (uv): `uv sync`
- Install (pip): `pip install -r requirements.txt`
- Run API (preferred): `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
- Run API (python): `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
- Run tests (pytest): `pytest -q`
- Run solver smoke test: `python app/services/cp_sat_simple_test.py`
- Sanity compile check: `python -m compileall app`

## Golden Rules

### Immutable
- Never hardcode credentials, DSN, tokens, or secrets.
- Never bypass role checks (`is_head_nurse`, `is_master_admin`) in routers or services.
- Never weaken hard scheduling constraints for convenience; use explicit config/versioned behavior instead.
- Never change DB schema assumptions in code without matching migration or backward-compatible handling.

### Do's
- Keep routers thin and move business logic into `app/services`.
- Reuse existing query and response patterns before introducing new abstractions.
- Validate target office/group boundaries for admin override flows.
- Preserve schedule/issue snapshot consistency when touching publish flows.

### Don'ts
- Do not mix persistence, permission, and optimization logic in a single new function.
- Do not add new global state for request-scoped behavior.
- Do not introduce alternate command paths when an existing service function already owns the flow.

## Standards & References

### Coding Standards
- Follow existing Python style in the touched module and keep changes minimal.
- Prefer explicit typing where patterns already use it.
- Keep imports and file-level side effects aligned with existing project conventions.

### Git Strategy
- Branch naming: `feature/*`, `fix/*`, `chore/*`.
- Commit style: conventional and intent-first.
- Message format: `<type>(<scope>): <why-focused summary>`.

### Maintenance Policy
- If implementation behavior diverges from this AGENTS system, update the nearest `AGENTS.md` in the same change.
- If a lower-level AGENTS rule conflicts with root policy, follow the lower-level file only for its scoped directory.

### Documentation Policy (Mandatory)
- Any feature/fix/refactor change MUST include or update a development note in `docs/`.
- If DB behavior or schema is touched, the note MUST include a `DB Changes` section.
- `DB Changes` section MUST contain SQL fenced code blocks (`sql`):
  1) Apply SQL (when schema/data migration exists)
  2) Verification SQL
- If no DB change exists, the note MUST still include a SQL fenced block for "no schema change" verification checks.
- Missing `DB Changes` section for DB-related changes is treated as incomplete work.

## Context Map (Action-Based Routing)

- **[FastAPI entrypoint and app wiring](./app/AGENTS.md)** — API lifecycle, router registration, middleware, and app-level conventions.
- **[HTTP endpoint behavior and permissions](./app/routers/AGENTS.md)** — Route contracts, auth/authorization checks, and request/response semantics.
- **[Roster engines and business services](./app/services/AGENTS.md)** — Optimization logic, service orchestration, and domain rules.
- **[Database sessions and models](./app/db/AGENTS.md)** — DB connections, SQLAlchemy models, and data boundary rules.
- **[LLM/graph analyzers](./app/agents/AGENTS.md)** — LangGraph state flow and analyzer-specific constraints.
- **[Operational and evaluation scripts](./scripts/AGENTS.md)** — One-off analysis scripts and execution patterns.
- **[RL mini project sandbox](./rl_mini_project/AGENTS.md)** — Experimental RL scheduling code isolated from production API paths.
- **[Documentation standards and templates](./docs/AGENTS.md)** — Dev-note format, DB SQL block requirements, and naming conventions.
