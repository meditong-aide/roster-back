# AGENTS for `app/agents`

## Module Context
- This directory contains LangGraph/LangChain-based analyzers for parsing and classifying nurse preference inputs.
- Graph composition and node behavior here influence how natural language requests become structured scheduling inputs.
- Outputs must remain compatible with downstream service and schema expectations.

## Tech Stack & Constraints
- LangGraph state graphs and node orchestration.
- LangChain-compatible model/tool invocation patterns.
- State types must stay explicit and stable; avoid ad-hoc shape changes.
- Keep analyzer logic deterministic where possible and auditable via structured outputs.

## Implementation Patterns
- Define or update typed state fields before introducing new graph edges.
- Keep each analyzer node focused on one responsibility.
- Preserve node naming conventions used by graph wiring.
- Document and validate assumptions about query categories and output payload keys.

## Testing Strategy
- Compile analyzer modules: `python -m compileall app/agents`
- Run API startup to ensure imports and graph wiring remain valid.
- Run `pytest -q` for regression checks when graph/state contracts change.
- For behavior changes, execute representative prompts and inspect structured outputs.

## Local Golden Rules

### Do's
- Keep graph transitions explicit and easy to trace.
- Return schema-stable dictionaries/lists expected by calling layers.
- Isolate model invocation details from routing/business code.

### Don'ts
- Do not mix DB write operations directly into analyzer nodes.
- Do not add hidden mutable global state for prompt/session behavior.
- Do not silently ignore malformed analyzer output; fail clearly or normalize explicitly.
