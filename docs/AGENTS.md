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
- Use one top-level heading: `주요 내용`.
- Under that heading, use numbered subheadings per work item:
  - Format: `N. [카테고리]: [작업명] [소요시간]`
  - Example category: `근무표 AI`, `코드 리뷰`, `운영 개선`
- For each subheading, add 2-5 bullet points that explain:
  - What was done
  - Why it was needed (problem/risk)
  - What changed for users or operations
- Keep each bullet to one short sentence when possible.
- Use plain, non-technical wording so non-developers can understand quickly.
- Do not include commit hashes, deep internal formulas, or code-level jargon.

### Example skeleton
- `주요 내용`
  - `근무표 AI`
    - `1. v2 일정 수립 [80분]`
      - 세브란스 관련 개발 항목을 정리하고 우선순위를 맞춤
      - 항목을 주제별로 나눠 담당자와 일정을 배정함
    - `2. 알고리즘 고도화 [240분]`
      - 주휴 근처 OFF 배치가 불안정한 문제를 줄이도록 보완함
      - 휴가가 많은 경우에도 OFF가 누락되지 않도록 계산 조건을 개선함
      - 부작용 가능 구간은 추가 테스트 대상으로 분리해 추적 중임
  - `코드 리뷰`
    - `3. 에이전트 프롬프트 테스트 리뷰 [50분]`
      - 동적 프롬프트 변경 건을 검토하고 병합함
      - 최적화 roster 처리 관련 변경사항을 함께 확인함

### Operational checklist
- Use `docs/DAILY_JOURNAL_CHECKLIST.md` when creating or updating the daily Notion journal entry.
