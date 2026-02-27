# AGENTS for `app`

## Module Context
- This directory contains the production FastAPI application, including routers, services, DB access, schemas, and AI analyzers.
- `app/main.py` is the central wiring point that mounts middleware, static files, and router modules.
- Treat everything under `app` as production-critical unless explicitly marked as legacy.

## Tech Stack & Constraints
- FastAPI, Starlette, SQLAlchemy ORM, Pydantic schemas.
- Scheduler logic depends on OR-Tools and roster configuration models.
- DB connectivity uses environment variables from `.env`; never hardcode connection values.
- Keep compatibility with Python 3.13+ and current dependency set in `pyproject.toml`.

## Implementation Patterns
- Add new HTTP behavior by editing `app/routers/*` first, then delegating business logic to `app/services/*`.
- Keep direct DB writes in service layer or dedicated DB utility functions.
- Reuse existing auth dependency patterns (`get_current_user_from_cookie`) for protected routes.
- Keep file boundaries clear: routers for I/O contracts, services for orchestration, db for persistence layer.

## Testing Strategy
- Full suite: `pytest -q`
- API import sanity: `python -m compileall app`
- Solver smoke test after schedule-engine changes: `python app/services/cp_sat_simple_test.py`
- For router/service changes, run endpoint-focused manual checks with local API server.

## Local Golden Rules

### Do's
- Preserve office/group scoping checks in every write path.
- Keep response shapes stable unless schema migration is intentional.
- Prefer extending existing service functions over adding parallel duplicates.

### Don'ts
- Do not place heavy optimization logic directly inside router functions.
- Do not bypass snapshot/version bookkeeping for schedule publish flows.
- Do not introduce implicit cross-module side effects at import time.
