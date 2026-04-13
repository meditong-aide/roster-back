---
name: bulk-mutation
description: Use when the user wants to perform batch modifications — such as disabling/enabling wanted adjustment entries by code, canceling wanted requests, updating wanted deadlines, or modifying multiple schedule entries at once. Also covers single-entry edits on schedules or wanted data.
---

# bulk-mutation

## Applicable When
- User asks to "적용 안되게", "취소해줘", "마감일 수정", "바꿔줘", "수정해줘"
- Targets wanted adjustments, wanted requests, or schedule entries

## Preconditions
- Target scope must be resolved (wanted_adjustment / wanted_submissions / schedule)
- Affected records must be identifiable
- For code-based filtering, code must be grounded via shift definitions

## Steps
1. Ground target scope and filter criteria
2. Query affected records
3. Show preview (affected count, sample records)
4. Require approval
5. Execute mutation
6. If followup action needed (e.g., generate), chain to next skill

## Never
- Execute bulk mutations without preview
- Modify records across multiple groups in one operation
