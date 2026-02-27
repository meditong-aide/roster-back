# AGENTS for `app/services`

## Module Context
- This is the core business layer for roster generation, validation, publishing, and supporting workflows.
- CP-SAT optimization modules and constraint handling live here.
- Service functions are the primary bridge between routers and DB models.

## Tech Stack & Constraints
- OR-Tools CP-SAT for optimization and feasibility checks.
- SQLAlchemy session usage via callers from routers.
- Business invariants must preserve staffing, fairness, and safety constraints.
- Prefer deterministic outputs for the same input/config whenever practical.

## Implementation Patterns
- Keep public service functions focused and cohesive around one use case.
- Split solver logic, post-processing, and DB persistence into explicit sub-steps.
- Reuse existing helper modules under `cp_sat`, `constraints`, `repairs`, and `objectives`.
- When adding config behavior, maintain backward compatibility for existing records.

## Testing Strategy
- Solver smoke test: `python app/services/cp_sat_simple_test.py`
- Project tests: `pytest -q`
- Compile checks for service code: `python -m compileall app/services`
- For changed constraints, run targeted scenario inputs and compare violations/output metrics.

## Local Golden Rules

### Do's
- Keep hard constraints explicit and non-optional unless feature-flagged/configured.
- Preserve snapshot/version semantics when services touch publish/unpublish flows.
- Validate both data integrity and permission boundaries for admin override flows.

### Don'ts
- Do not hide infeasibility by silently relaxing constraints.
- Do not couple unrelated concerns (auth, persistence, optimization) in one large function.
- Do not introduce non-reproducible randomness without explicit seed control.
