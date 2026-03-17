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

### Code Modification Rules (STRICT)

- Only modify the minimum necessary lines to achieve the goal
- Do NOT reformat, reorder, or clean up unrelated code
- Do NOT change indentation, spacing, or line breaks unless required
- Do NOT rename variables or refactor unless explicitly asked
- Preserve existing coding style exactly as-is

### Editing Discipline

- Before editing, identify the exact lines that must change
- Limit changes strictly to those lines
- Avoid touching surrounding code
- Prefer surgical edits over broad rewrites

### Forbidden Behavior

- No formatting-only changes
- No "cleanup" changes
- No style normalization
- No large refactors unless explicitly requested

### Patch Strategy

- Prefer minimal, diff-style edits
- Avoid rewriting entire files or functions unless strictly necessary
- Never replace entire files unless absolutely necessary
- Prefer line-level edits over block-level rewrites
- Think in terms of diffs, not rewritten code

### Work Mode Separation (STRICT)

- Treat bug fixes and refactors as different kinds of work.
- Default to Bugfix Mode unless the user explicitly asks for refactoring.
- Never introduce refactoring during bug fixing unless explicitly requested.
- Never expand the scope of a bug fix into cleanup, renaming, or structural improvement.
- If a cleaner structural fix seems preferable, first explain it as a separate refactor option instead of applying it by default.

### Bugfix Mode

Use Bugfix Mode when the request is about:
- fixing an error
- resolving incorrect behavior
- patching a failing test
- addressing a regression
- making a minimal functional correction

In Bugfix Mode:
- change only what is required to fix the issue
- preserve existing structure unless the current structure directly prevents the fix
- avoid renaming, extraction, file moves, or abstraction changes
- avoid formatting-only edits
- prefer the smallest safe patch
- validate with the smallest relevant test scope first

### Refactor Mode

Use Refactor Mode only when the user explicitly asks for:
- refactoring
- cleanup
- restructuring
- simplification
- modularization
- naming improvements
- abstraction changes

In Refactor Mode:
- explain the intended scope before making broad changes
- keep behavior unchanged unless explicitly requested otherwise
- separate pure refactor changes from behavior changes when possible
- identify risk areas and affected modules before editing
- validate behavior with relevant tests after changes

### Decision Rule

- If the request is ambiguous, choose Bugfix Mode.
- If both bug fixing and refactoring are needed, do the bug fix first and present refactoring as a separate follow-up step.
- Do not silently combine Bugfix Mode and Refactor Mode in a single edit pass unless explicitly requested.

### Scope Control

- Solve only the explicitly requested problem, not adjacent issues
- Do not fix unrelated code smells during bug fixing
- Do not expand a local fix into a broader redesign
- If additional issues are discovered, report them separately instead of fixing them
- Avoid modifying files or functions not directly related to the request

### Git Strategy
- Branch naming: `feature/*`, `fix/*`, `chore/*`.
- Commit style: conventional and intent-first.
- Message format: `<type>(<scope>): <why-focused summary>`.

### Maintenance Policy
- If implementation behavior diverges from this AGENTS system, update the nearest `AGENTS.md` in the same change.
- If a lower-level AGENTS rule conflicts with root policy, follow the lower-level file only for its scoped directory.

## Context Map (Action-Based Routing)

- **[FastAPI entrypoint and app wiring](./app/AGENTS.md)** — API lifecycle, router registration, middleware, and app-level conventions.
- **[HTTP endpoint behavior and permissions](./app/routers/AGENTS.md)** — Route contracts, auth/authorization checks, and request/response semantics.
- **[Roster engines and business services](./app/services/AGENTS.md)** — Optimization logic, service orchestration, and domain rules.
- **[Database sessions and models](./app/db/AGENTS.md)** — DB connections, SQLAlchemy models, and data boundary rules.
- **[LLM/graph analyzers](./app/agents/AGENTS.md)** — LangGraph state flow and analyzer-specific constraints.
- **[Operational and evaluation scripts](./scripts/AGENTS.md)** — One-off analysis scripts and execution patterns.
- **[RL mini project sandbox](./rl_mini_project/AGENTS.md)** — Experimental RL scheduling code isolated from production API paths.
