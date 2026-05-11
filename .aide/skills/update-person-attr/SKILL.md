---
name: update-person-attr
description: Use when the user wants to modify nurse attributes such as dispatch status, night exclusion, education period, resignation, preceptor assignment, team assignment, grade, fixed shift, or pair restrictions (work-together / work-apart).
---

# update-person-attr

## Applicable When
- User references a specific nurse and an attribute change
- Examples: "파견 처리", "나이트 제외", "프리셉터 지정", "같은 조 금지", "같은 조 배치"

## Preconditions
- Target nurse must be entity-grounded (name → nurse_id)
- Attribute type and value must be clear
- group_id must be resolved

## Steps
1. Entity-ground the target nurse(s)
2. Read current attribute value
3. Preview the change
4. Apply after approval if needed
5. If this affects generation, inform user they may want to regenerate

## Never
- Modify nurses outside the current group without explicit request
- Change RBAC-protected fields without proper permission check
