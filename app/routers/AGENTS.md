# AGENTS for `app/routers`

## Module Context
- FastAPI endpoint handlers live here.
- Router files define request/response contracts, dependency injection, and permission gates.
- Most domain processing must be delegated to `app/services`.

## Tech Stack & Constraints
- FastAPI `APIRouter`, dependency injection, and HTTP exception patterns.
- Auth context comes from cookie/token dependencies in auth router utilities.
- Router code must remain lightweight and deterministic.

## Implementation Patterns
- Validate user identity first; fail fast with 401/403 where required.
- Resolve target scope (office/group) in router when user role affects query target.
- Call a service function for business operations and keep router logic thin.
- Use existing response and error conventions already used in nearby routes.

## Testing Strategy
- Start API locally: `uv run uvicorn app.main:app --reload`
- Verify changed endpoints manually (auth + role variations).
- Run regression checks: `pytest -q`
- Run syntax/import sanity for touched router modules: `python -m compileall app/routers`

## Local Golden Rules

### Do's
- Enforce role checks (`is_head_nurse`, `is_master_admin`) consistently.
- Preserve `group_id` override validation for admin-only access paths.
- Keep router-level validation clear and explicit.

### Don'ts
- Do not embed CP-SAT engine orchestration directly in routes.
- Do not swallow exceptions silently; convert to explicit HTTP errors.
- Do not return ad-hoc response shapes without matching schema contracts.
