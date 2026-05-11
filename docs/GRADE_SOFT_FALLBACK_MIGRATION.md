# Grade hard-default migration (`allow_soft_fallback`)

To make grade constraints hard by default while keeping an explicit soft mode option,
add the `allow_soft_fallback` column to `roster_grade_config`.

## MSSQL migration SQL

```sql
IF COL_LENGTH('roster_grade_config', 'allow_soft_fallback') IS NULL
BEGIN
    ALTER TABLE roster_grade_config
    ADD allow_soft_fallback TINYINT NOT NULL
        CONSTRAINT DF_roster_grade_config_allow_soft_fallback DEFAULT (0);
END
```

- `0` = hard constraint mode (default)
- `1` = soft fallback mode (slack + penalty)

## Backfill behavior

Existing rows automatically get `0` via `NOT NULL DEFAULT (0)`.
