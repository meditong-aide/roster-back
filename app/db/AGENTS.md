# AGENTS for `app/db`

## Module Context
- This directory contains DB clients, SQLAlchemy models, and roster/nurse configuration model glue.
- It defines persistence boundaries for API and solver layers.
- DB adapter behavior impacts all modules, so backward compatibility is critical.

## Tech Stack & Constraints
- SQLAlchemy ORM models and session lifecycle helpers.
- Mixed database integration (MSSQL and MariaDB connectors).
- Connection settings come from environment variables only.
- Schema assumptions must match live database and migration state.

## Implementation Patterns
- Keep session provider functions minimal and side-effect free.
- Model changes must be additive/safe unless coordinated with migrations.
- Use clear column semantics for office/group ownership and schedule versioning.
- Centralize repeated DB access patterns in reusable helpers when patterns repeat.

## Testing Strategy
- Compile DB layer: `python -m compileall app/db`
- Run integration smoke tests through API startup: `uv run uvicorn app.main:app --reload`
- Run regression tests after model/client changes: `pytest -q`
- Validate connection env wiring in local `.env` without committing secrets.

## Local Golden Rules

### Do's
- Preserve referential assumptions used by routers/services.
- Keep timezone/date handling consistent with existing model usage.
- Ensure new fields have safe defaults or nullable handling where needed.

### Don'ts
- Do not hardcode DSN, host, or credentials.
- Do not change primary identifiers (`office_id`, `group_id`, `schedule_id`) semantics casually.
- Do not embed business policy logic in low-level DB client code.
