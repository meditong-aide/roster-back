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
  1. Apply SQL
  2. Verification SQL
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

0. Verify Notion MCP connectivity first with `opencode mcp list` and confirm `notion` is connected.
1. Check whether today's child page already exists under `개발일지`.
2. If missing, create today's page. If present, update that same page.
3. Collect today's commits with branch context (focus on commits authored today on the current working branch path).
4. Convert commit intent into user-facing work summary (feature-level + key sub-tasks).
5. Write/update the Notion entry using bullet points only; do not include commit hashes.
6. If the first MCP write attempt fails, retry with explicit model pin:
   - `opencode run --model openai/gpt-5.3-codex "<Notion update instruction>"`
7. If MCP is unavailable, explicitly record blocker reason (auth/billing/connectivity) and return a ready-to-paste body.

### Writing format (Mandatory)

- Use one top-level heading: `주요 내용`.
- Under that heading, use numbered subheadings per work item:
  - Format: `N. [카테고리]: [작업명] [소요시간]`
  - Example category: `근무표 AI`, `코드 리뷰`, `운영 개선`
- For each subheading, add 3-6 bullet points that MUST include all of the following:
  - What problem was observed
  - What fix/change direction was planned
  - What was actually implemented
  - What tests/experiments were performed
  - What final result/decision was made (or what was changed)
  - Total time spent for that item (match the title duration)
- Keep each bullet to one short sentence when possible.
- Use plain, non-technical wording so non-developers can understand quickly.
- Do not include commit hashes, deep internal formulas, or code-level jargon.

### Required per-item checklist (Mandatory)

For every numbered work item, verify this checklist before saving:

- `문제`: 어떤 문제가 있었는지 적었는가
- `계획`: 어떤 수정 방향을 잡았는지 적었는가
- `수행`: 실제로 무엇을 바꿨는지 적었는가
- `실험`: 어떤 테스트/실험을 했는지 적었는가
- `결론`: 최종적으로 무엇으로 결정/변경했는지 적었는가
- `시간`: 해당 항목 총 소요시간을 적었는가

### Example skeleton

- `주요 내용`
  - `근무표 AI`
    - `1. v2 일정 수립 [80분]`
      - 문제: 우선순위 기준이 없어 일정이 자주 밀리는 상황이 있었음
      - 계획: 업무를 난이도와 영향도로 나눠 먼저 처리할 항목을 정하기로 함
      - 수행: 개발 항목을 주제별로 묶고 담당자 기준으로 일정안을 다시 작성함
      - 실험: 하루 단위로 적용해보고 지연 항목이 줄어드는지 확인함
      - 결론: 새 우선순위 방식으로 운영하기로 결정함
      - 시간: 총 80분 사용
    - `2. 알고리즘 고도화 [240분]`
      - 문제: 특정 달에서 근무가 일부 날짜에 몰리는 현상이 반복됨
      - 계획: 분배 기준을 보완하고 부작용 구간을 따로 검증하기로 함
      - 수행: 분배 로직과 설정값을 조정해 몰림 완화 방향으로 수정함
      - 실험: 인원 많은 그룹과 적은 그룹을 나눠 시나리오 테스트를 진행함
      - 결론: 기본값을 조정하고 추가 검증 항목을 운영 체크리스트에 반영함
      - 시간: 총 240분 사용
  - `코드 리뷰`
    - `3. 에이전트 프롬프트 테스트 리뷰 [50분]`
      - 문제: 프롬프트 변경 후 응답 품질 편차가 생길 수 있다는 우려가 있었음
      - 계획: 실제 요청 흐름 기준으로 품질을 확인하고 병합 여부를 판단하기로 함
      - 수행: 주요 시나리오 결과를 비교 검토하고 리뷰 의견을 반영함
      - 실험: 동일 질문 반복 테스트로 응답 일관성을 확인함
      - 결론: 안정성이 확보된 항목만 병합하기로 결정함
      - 시간: 총 50분 사용

### Operational checklist

- Use `docs/DAILY_JOURNAL_CHECKLIST.md` when creating or updating the daily Notion journal entry.
