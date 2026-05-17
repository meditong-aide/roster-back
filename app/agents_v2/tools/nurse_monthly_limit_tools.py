"""NurseMonthlyLimit tools — 개인별 월 한도 (D/E/N/O × min/max/exact) read/upsert."""

from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import NurseMonthlyLimit, Nurse


# 12 fields: {shift_code}_{bound} where shift_code ∈ {d,e,n,o} bound ∈ {min,max,exact}
LIMIT_FIELDS = (
    "d_min", "d_max", "d_exact",
    "e_min", "e_max", "e_exact",
    "n_min", "n_max", "n_exact",
    "o_min", "o_max", "o_exact",
)


def get_monthly_limit(
    db: Session,
    nurse_id: str,
    group_id: str,
    year: int,
    month: int,
) -> dict | None:
    """단일 간호사의 월 한도 조회. row 없으면 None."""
    row = (
        db.query(NurseMonthlyLimit)
        .filter(
            NurseMonthlyLimit.nurse_id == nurse_id,
            NurseMonthlyLimit.group_id == group_id,
            NurseMonthlyLimit.year == year,
            NurseMonthlyLimit.month == month,
        )
        .first()
    )
    if not row:
        return None
    return _to_dict(row)


def list_monthly_limits(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    *,
    nurse_ids: list[str] | None = None,
) -> list[dict]:
    """병동 전체 (또는 일부) 의 월 한도 목록."""
    q = (
        db.query(NurseMonthlyLimit, Nurse.name)
        .outerjoin(Nurse, Nurse.nurse_id == NurseMonthlyLimit.nurse_id)
        .filter(
            NurseMonthlyLimit.group_id == group_id,
            NurseMonthlyLimit.year == year,
            NurseMonthlyLimit.month == month,
        )
    )
    if nurse_ids:
        q = q.filter(NurseMonthlyLimit.nurse_id.in_(nurse_ids))
    rows = q.all()
    return [{**_to_dict(r), "nurse_name": name} for r, name in rows]


def upsert_monthly_limit(
    db: Session,
    nurse_id: str,
    group_id: str,
    year: int,
    month: int,
    updates: dict,
    *,
    preview_only: bool = False,
) -> dict:
    """월 한도 upsert. updates 의 LIMIT_FIELDS 만 반영. preview_only=True 면 DB 미적용."""
    invalid = [k for k in updates if k not in LIMIT_FIELDS]
    if invalid:
        return {"error": f"Unknown limit fields: {invalid}", "allowed": list(LIMIT_FIELDS)}

    row = (
        db.query(NurseMonthlyLimit)
        .filter(
            NurseMonthlyLimit.nurse_id == nurse_id,
            NurseMonthlyLimit.group_id == group_id,
            NurseMonthlyLimit.year == year,
            NurseMonthlyLimit.month == month,
        )
        .first()
    )

    changes = {}
    if row is None:
        # 신규 row 생성 대상
        for k, v in updates.items():
            changes[k] = {"old": None, "new": v}
    else:
        for k, v in updates.items():
            old = getattr(row, k)
            if old != v:
                changes[k] = {"old": old, "new": v}

    if preview_only:
        return {
            "preview": True,
            "nurse_id": nurse_id,
            "year": year,
            "month": month,
            "changes": changes,
            "creating_new_row": row is None,
        }

    if row is None:
        row = NurseMonthlyLimit(
            nurse_id=nurse_id, group_id=group_id, year=year, month=month
        )
        for k, v in updates.items():
            setattr(row, k, v)
        db.add(row)
    else:
        for k, v in updates.items():
            setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return {
        "preview": False,
        "nurse_id": nurse_id,
        "year": year,
        "month": month,
        "changes": changes,
        "row": _to_dict(row),
    }


# ── private ──────────────────────────────────────────────


def _to_dict(row: NurseMonthlyLimit) -> dict:
    d = {
        "id": row.id,
        "nurse_id": row.nurse_id,
        "group_id": row.group_id,
        "year": row.year,
        "month": row.month,
    }
    for f in LIMIT_FIELDS:
        d[f] = getattr(row, f)
    return d
