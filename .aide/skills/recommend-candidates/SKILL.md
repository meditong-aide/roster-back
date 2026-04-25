---
name: recommend-candidates
description: Use when the user wants candidate recommendations such as emergency off replacements, leave substitutes, fair workload redistribution targets, or any ranked list of nurses based on scheduling criteria.
---

# recommend-candidates

## Applicable When
- User asks for "추천", "후보", "우선순위", "대체 인력"
- Examples: "응급 오프 후보 5명", "미사용 연차 많은 순", "Grade 1 없는 날짜에 배치할 사람"

## Preconditions
- Recommendation criteria must be grounded
- Current schedule state must be queryable
- group_id and time range must be resolved

## Steps
1. Clarify recommendation criteria if ambiguous
2. Query current schedule and nurse data
3. Apply ranking/scoring logic
4. Return ranked candidate list with reasoning

## Never
- Recommend without showing the basis for ranking
- Auto-apply recommendations without user approval
