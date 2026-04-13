---
name: generate-schedule
description: Use when the user wants to create or regenerate a roster/schedule. This includes initial generation, regeneration with modified constraints, and generation after bulk mutations.
---

# generate-schedule

## Applicable When
- User asks to "생성", "만들어줘", "다시 돌려줘", "배치해줘" in the context of creating a schedule
- Often chained after update-constraint or bulk-mutation

## Preconditions
- group_id, year, month must be resolved
- All prerequisite constraint/attribute changes should be applied first
- Current config should be validated

## Steps
1. Confirm generation parameters (preview config summary)
2. Require approval for generation
3. Trigger async generation
4. Return job id and status
5. Optionally poll for completion

## Never
- Generate without showing which constraints/config will be used
- Skip approval step
