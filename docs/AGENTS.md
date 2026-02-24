# AGENTS for `docs`

## Scope
- Applies to all development notes and changelog/spec documents under `docs/`.
- Use this file as the authoritative template policy for documenting feature/fix/refactor changes.

## Dev Note Template (Required)
Each document SHOULD include these sections in order:
1. Purpose / Why this change was needed
2. What changed
3. Backend impact
4. Frontend impact (if applicable)
5. Validation checklist
6. DB Changes (MANDATORY)

## DB Changes Section Rules (Mandatory)
- Always include SQL fenced blocks using `sql` language tags.
- If DB changed, include:
  1) Apply SQL
  2) Verification SQL
- If DB did not change, include a SQL block that verifies "no schema change" assumptions.

### Required SQL block examples

#### A) DB changed
```sql
-- Apply SQL
ALTER TABLE dbo.example_table
ADD example_col INT NOT NULL CONSTRAINT DF_example_table_example_col DEFAULT 0;

-- Verification SQL
SELECT COLUMN_NAME
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'example_table' AND COLUMN_NAME = 'example_col';
```

#### B) No DB change
```sql
-- No schema change verification
SELECT COLUMN_NAME
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo'
  AND TABLE_NAME IN ('roster_config', 'daily_shift')
  AND COLUMN_NAME IN ('use_mid', 'm_count');
```

## File Naming Convention
- Feature spec: `FEATURE_NAME_BACKEND_FRONT_SPEC.md`
- Fix changelog: `TOPIC_CHANGELOG.md`
- Analysis note: `TOPIC_ANALYSIS_YYYY_MM.md`

## Writing Guidelines
- Keep wording implementation-focused and reproducible.
- Include concrete API paths, model fields, and config keys when relevant.
- Avoid ambiguous terms; use exact code identifiers in backticks.
- Keep checklists action-oriented and verifiable.
