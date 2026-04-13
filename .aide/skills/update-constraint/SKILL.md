---
name: update-constraint
description: Use when the user wants to modify scheduling constraints such as max consecutive nights, min rest hours, staffing levels, holiday adjustments, weekend distribution, grade requirements, or any roster generation parameter.
---

# update-constraint

## Applicable When
- User mentions scheduling rules, limits, or generation parameters to change
- Examples: "연속 나이트 최대 2일", "공휴일 1명 적게", "주말 분산", "최소 휴식시간 8시간"

## Preconditions
- group_id must be resolved
- target year/month should be known or clarified
- specific constraint type and value must be grounded

## Steps
1. Identify which constraint type is being modified
2. Read current constraint settings for the group
3. Show current value vs requested value (preview)
4. Apply the change
5. If user also wants generation, chain to generate-schedule skill

## Never
- Apply constraint changes without showing the current value first
- Guess constraint parameter names — read the config schema
