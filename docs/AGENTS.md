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

## Daily Development Journal (Notion)

### Target location (Mandatory)
- Parent page: `개발일지` (`https://www.notion.so/311879e089a581e5bfc2e3cfadb7d824`).
- Journal unit: one child page per date.

### Update workflow (Mandatory)
1. Check whether today's child page already exists under `개발일지`.
2. If missing, create today's page. If present, update that same page.
3. Collect today's commits with branch context (focus on commits authored today on the current working branch path).
4. Convert commit intent into user-facing work summary (feature-level + key sub-tasks).
5. Write/update the Notion entry using bullet points only; do not include commit hashes.

### Writing format (Mandatory)
- Use short bullet points that describe:
  - What feature or improvement was worked on today
  - What key sub-tasks were done to complete that work
  - What user-visible or operational effect is expected
- Keep wording plain and intuitive so non-developers can understand it quickly.
- Avoid low-level implementation detail unless it changes business behavior.

### Example skeleton
- `오늘 진행한 기능`
  - 예약/스케줄 관련 어떤 기능을 개선했는지 한 줄 요약
- `세부 작업`
  - 데이터 검증/권한 처리/응답 개선 등 핵심 작업 항목
- `기대 효과`
  - 현장 사용자가 체감하는 변화 또는 운영상 이점

### Operational checklist
- Use `docs/DAILY_JOURNAL_CHECKLIST.md` when creating or updating the daily Notion journal entry.
