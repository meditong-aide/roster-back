---
name: query-schedule
description: Use when the user wants to view, search, filter, count, or aggregate data from schedules, wanted submissions, wanted adjustments, nurse lists, or shift definitions. Covers all read-only data retrieval including distribution analysis, submission status, and filtered views.
---

# query-schedule

## Applicable When
- User asks to "보여줘", "조회해줘", "알려줘", "몇 명", "누구", "리스트", "집계", "분포"
- Data source can be any scope: published/draft schedule, wanted submissions/adjustments, nurse info

## Preconditions
- scope must be resolved (which data source)
- year/month must be known or inferred from context
- schedule version must be resolved (published > latest version as default)

## Steps
1. Resolve scope and time range
2. Resolve schedule version if schedule-related
3. Ground any domain concepts (temporal, status, entity) as needed
4. Query the data via appropriate tools
5. Apply filters, aggregation, sorting as requested
6. Format and return results

## Never
- Return raw data without applying the user's requested filter/sort
- Assume schedule version without checking what exists
