"""Wanted (shift preference request) tools."""

from __future__ import annotations

from datetime import datetime, date as date_type

from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from db.models import (
    Nurse,
    Shift,
    Wanted,
    WantedRequest,
    NurseShiftRequest,
    NursePairRequest,
    FixedWantedEntry,
    ShiftPreference,
)


def _wanted_request_month_str(year: int, month: int) -> str:
    """WantedRequest.month is CHAR(7) format 'YYYY-MM'."""
    return f"{year}-{month:02d}"


def get_wanted_status(
    db: Session, group_id: str, year: int, month: int
) -> dict | None:
    """Get the wanted campaign status for a group/month."""
    row = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == group_id,
            Wanted.year == year,
            Wanted.month == month,
        )
        .first()
    )
    if not row:
        return None
    return {
        "group_id": row.group_id,
        "year": row.year,
        "month": row.month,
        "exp_date": str(row.exp_date) if row.exp_date else None,
        "status": row.status,
    }


def get_wanted_submissions(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    *,
    nurse_id: str | None = None,
    submitted_only: bool = True,
    submitted_after: str | None = None,
    submitted_before: str | None = None,
) -> list[dict]:
    """Get shift request detail rows for a group/month.

    Args:
        submitted_only: If True, only return submitted requests. If False, include all.
        submitted_after: ISO date string — only requests submitted on or after this date.
        submitted_before: ISO date string — only requests submitted on or before this date.
    """
    month_str = _wanted_request_month_str(year, month)
    q = (
        db.query(NurseShiftRequest, WantedRequest.is_submitted, WantedRequest.submitted_at)
        .join(WantedRequest, NurseShiftRequest.request_id == WantedRequest.request_id)
        .join(Nurse, NurseShiftRequest.nurse_id == Nurse.nurse_id)
        .filter(WantedRequest.month == month_str)
        .filter(Nurse.group_id == group_id)
    )
    if nurse_id:
        q = q.filter(NurseShiftRequest.nurse_id == nurse_id)
    if submitted_only:
        q = q.filter(WantedRequest.is_submitted == True)
    if submitted_after:
        q = q.filter(WantedRequest.submitted_at >= submitted_after)
    if submitted_before:
        q = q.filter(WantedRequest.submitted_at <= submitted_before)
    rows = q.all()
    return [
        {
            "nurse_id": r[0].nurse_id,
            "request_id": r[0].request_id,
            "shift_date": str(r[0].shift_date) if r[0].shift_date else None,
            "shift": r[0].shift,
            "shifts_table_id": r[0].shifts_table_id,
            "score": r[0].score,
            "comment": r[0].comment,
            "is_submitted": bool(r[1]),
            "submitted_at": str(r[2]) if r[2] else None,
        }
        for r in rows
    ]


def get_wanted_adjustments(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    *,
    nurse_id: str | None = None,
    date_range: tuple[str, str] | None = None,
    source_types: list[str] | None = None,
    is_applied: bool | None = None,
) -> list[dict]:
    """Get fixed wanted (adjustment) entries for a group/month.

    Returned rows expose ``source_type`` and ``original_shift_id`` so the caller
    can distinguish nurse-submitted intent from head-nurse interventions:
      - ``original``: nurse-submitted, shift_id is the answer
      - ``modified``: nurse-submitted but head nurse changed code → original_shift_id is the nurse's intent
      - ``added``: head-nurse only, no nurse submission
      - ``weekly_off``: auto-assigned weekly off
    """
    q = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year,
        FixedWantedEntry.month == month,
    )
    if nurse_id:
        q = q.filter(FixedWantedEntry.nurse_id == nurse_id)
    if date_range:
        start, end = date_range
        q = q.filter(FixedWantedEntry.shift_date >= start, FixedWantedEntry.shift_date <= end)
    if source_types:
        q = q.filter(FixedWantedEntry.source_type.in_(source_types))
    if is_applied is not None:
        q = q.filter(FixedWantedEntry.is_applied == is_applied)
    rows = q.all()
    return [
        {
            "entry_id": r.id,
            "group_id": r.group_id,
            "nurse_id": r.nurse_id,
            "year": r.year,
            "month": r.month,
            "work_date": str(r.shift_date) if r.shift_date else None,
            "shift_id": r.shift_id,
            "source_type": r.source_type,
            "original_shift_id": r.original_shift_id,
            "is_applied": bool(r.is_applied) if hasattr(r, "is_applied") else True,
            "reason": r.reason,
            "head_nurse_memo": r.head_nurse_memo,
        }
        for r in rows
    ]


def get_submission_status(
    db: Session, group_id: str, year: int, month: int
) -> dict:
    """Get submission status summary — who submitted, who didn't.

    group_id-scoped single outerjoin: 한 쿼리로 DB-level filter, RBAC 격리.
    """
    from db.models import Nurse

    month_str = _wanted_request_month_str(year, month)
    rows = (
        db.query(Nurse.nurse_id, Nurse.name, WantedRequest.is_submitted)
        .outerjoin(
            WantedRequest,
            (WantedRequest.nurse_id == Nurse.nurse_id)
            & (WantedRequest.month == month_str)
            & (WantedRequest.group_id == group_id)
            & (WantedRequest.is_submitted == True),
        )
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )
    submitted, not_submitted = [], []
    for nurse_id, name, is_sub in rows:
        bucket = submitted if is_sub else not_submitted
        bucket.append({"nurse_id": nurse_id, "name": name})
    return {
        "total": len(rows),
        "submitted_count": len(submitted),
        "not_submitted_count": len(not_submitted),
        "submitted": submitted,
        "not_submitted": not_submitted,
    }


def bulk_update_wanted_adjustments(
    db: Session,
    entry_ids: list[str],
    field: str,
    value,
    *,
    preview_only: bool = False,
) -> dict:
    """Bulk update fixed wanted entries. Returns affected count."""
    rows = (
        db.query(FixedWantedEntry)
        .filter(FixedWantedEntry.id.in_(entry_ids))
        .all()
    )
    if preview_only:
        return {
            "preview": True,
            "affected_count": len(rows),
            "entry_ids": [r.id for r in rows],
        }
    for row in rows:
        setattr(row, field, value)
    db.commit()
    return {
        "preview": False,
        "affected_count": len(rows),
        "entry_ids": [r.id for r in rows],
    }


def update_wanted_deadline(
    db: Session, group_id: str, year: int, month: int, new_deadline: str
) -> dict:
    """Update wanted campaign deadline."""
    from datetime import datetime as dt

    row = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == group_id,
            Wanted.year == year,
            Wanted.month == month,
        )
        .first()
    )
    if not row:
        return {"error": "Wanted campaign not found"}
    old_deadline = str(row.exp_date) if row.exp_date else None
    # Parse string to datetime for DB compatibility (SQLite requires datetime objects)
    try:
        row.exp_date = dt.strptime(new_deadline, "%Y-%m-%d")
    except (ValueError, TypeError):
        row.exp_date = new_deadline
    db.commit()
    return {
        "group_id": group_id,
        "year": year,
        "month": month,
        "old_deadline": old_deadline,
        "new_deadline": new_deadline,
    }


def cancel_wanted_request(
    db: Session, nurse_id: str, group_id: str, year: int, month: int, request_id: int | None = None
) -> dict:
    """Cancel (retract) a wanted request submission.

    group_id 필터로 cross-group mutation 차단 (defense-in-depth).
    """
    month_str = _wanted_request_month_str(year, month)
    q = db.query(WantedRequest).filter(
        WantedRequest.nurse_id == nurse_id,
        WantedRequest.group_id == group_id,
        WantedRequest.month == month_str,
        WantedRequest.is_submitted == True,
    )
    if request_id:
        q = q.filter(WantedRequest.request_id == request_id)
    row = q.order_by(desc(WantedRequest.submitted_at)).first()
    if not row:
        return {"error": "No submitted request found"}
    row.is_submitted = False
    row.submitted_at = None
    db.commit()
    return {
        "nurse_id": nurse_id,
        "request_id": row.request_id,
        "month_str": month_str,
        "status": "retracted",
    }


def _parse_date(val) -> date_type:
    """Convert string or datetime to date object (SQLite compatibility)."""
    if isinstance(val, date_type):
        return val
    if isinstance(val, str):
        return datetime.strptime(val, "%Y-%m-%d").date()
    return val


# ── Per-date wanted CRUD ──────────────────────────────────


def _get_latest_submitted_request(
    db: Session, nurse_id: str, month_str: str
) -> WantedRequest | None:
    """Find the latest submitted WantedRequest for a nurse+month."""
    return (
        db.query(WantedRequest)
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
        )
        .order_by(desc(WantedRequest.submitted_at))
        .first()
    )


def _create_draft_with_copy(
    db: Session,
    nurse_id: str,
    month_str: str,
    old_request_id: int | None,
    *,
    group_id: str,
    exclude_date: str | None = None,
    modify_date: str | None = None,
    modify_shift: str | None = None,
    modify_shifts_table_id: int | None = None,
    modify_comment: str | None = None,
    add_date: str | None = None,
    add_shift: str | None = None,
    add_shifts_table_id: int | None = None,
    add_comment: str | None = None,
) -> tuple[WantedRequest, int]:
    """Create a new draft WantedRequest and copy shift+pair data with modifications.

    Returns (new_draft, shift_count).
    """
    # 1. New request_id = MAX + 1
    max_rid = (
        db.query(func.max(WantedRequest.request_id))
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
        )
        .scalar()
    ) or 0
    new_request_id = max_rid + 1

    # 2. Create draft WantedRequest
    draft = WantedRequest(
        nurse_id=nurse_id,
        request_id=new_request_id,
        month=month_str,
        group_id=group_id,
        request="",
        is_submitted=False,
        created_at=datetime.now(),
        submitted_at=None,
    )
    db.add(draft)

    # 3. Copy NurseShiftRequest rows from old request_id (with modifications)
    detailed_id = 1
    if old_request_id is not None:
        old_rows = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == old_request_id,
            )
            .all()
        )
        for row in old_rows:
            date_str = str(row.shift_date) if row.shift_date else None
            # Skip the excluded date (delete case)
            if exclude_date and date_str == exclude_date:
                continue
            # Modify the target date (modify case)
            shift = row.shift
            s_table_id = row.shifts_table_id
            comment = row.comment
            if modify_date and date_str == modify_date:
                shift = modify_shift or shift
                s_table_id = modify_shifts_table_id if modify_shifts_table_id is not None else s_table_id
                if modify_comment is not None:
                    comment = modify_comment
            db.add(NurseShiftRequest(
                nurse_id=nurse_id,
                request_id=new_request_id,
                detailed_request_id=detailed_id,
                shift_date=row.shift_date,
                group_id=group_id,
                shift=shift,
                shifts_table_id=s_table_id,
                score=row.score,
                partial_request=row.partial_request,
                comment=comment,
            ))
            detailed_id += 1

    # 4. Add new date (add case)
    if add_date and add_shift:
        parsed_date = _parse_date(add_date)
        db.add(NurseShiftRequest(
            nurse_id=nurse_id,
            request_id=new_request_id,
            detailed_request_id=detailed_id,
            shift_date=parsed_date,
            group_id=group_id,
            shift=add_shift,
            shifts_table_id=add_shifts_table_id,
            score=1.0,
            partial_request="",
            comment=add_comment or "",
        ))
        detailed_id += 1

    # 5. Copy NursePairRequest from old request_id
    if old_request_id is not None:
        old_pairs = (
            db.query(NursePairRequest)
            .filter(
                NursePairRequest.nurse_id == nurse_id,
                NursePairRequest.request_id == old_request_id,
                NursePairRequest.month == month_str,
            )
            .all()
        )
        pair_id = 1
        for p in old_pairs:
            db.add(NursePairRequest(
                nurse_id=nurse_id,
                request_id=new_request_id,
                month=month_str,
                detailed_request_id=pair_id,
                target_id=p.target_id,
                group_id=group_id,
                score=p.score,
                partial_request=p.partial_request,
            ))
            pair_id += 1

    db.flush()
    return draft, detailed_id - 1


def _resolve_shifts_table_id(db: Session, group_id: str, shift_code: str) -> int | None:
    """Map shift_id → shifts.id for a group."""
    row = (
        db.query(Shift.id)
        .filter(Shift.group_id == group_id, Shift.shift_id == shift_code)
        .first()
    )
    return row[0] if row else None


def _submit_draft(db: Session, draft: WantedRequest) -> None:
    """Mark a draft as submitted."""
    draft.is_submitted = True
    draft.submitted_at = datetime.now()
    db.commit()


def delete_wanted_by_date(
    db: Session, nurse_id: str, group_id: str, year: int, month: int, date: str,
    *, preview_only: bool = True,
) -> dict:
    """Delete a specific date from wanted submissions."""
    month_str = _wanted_request_month_str(year, month)
    old_wr = _get_latest_submitted_request(db, nurse_id, month_str)
    if not old_wr:
        return {"error": "제출된 원티드가 없습니다."}

    # Check the target date exists
    target = (
        db.query(NurseShiftRequest)
        .filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == old_wr.request_id,
            NurseShiftRequest.shift_date == _parse_date(date),
        )
        .first()
    )
    if not target:
        return {"error": f"{date}에 해당하는 원티드가 없습니다."}

    if preview_only:
        return {
            "preview_only": True,
            "action": "delete_wanted_date",
            "nurse_id": nurse_id,
            "date": date,
            "shift": target.shift,
            "comment": target.comment or "",
            "message": f"{date} {target.shift} 원티드를 취소합니다.",
        }

    draft, count = _create_draft_with_copy(
        db, nurse_id, month_str, old_wr.request_id,
        group_id=group_id,
        exclude_date=date,
    )
    _submit_draft(db, draft)
    return {
        "action": "delete_wanted_date",
        "nurse_id": nurse_id,
        "request_id": draft.request_id,
        "date": date,
        "deleted_shift": target.shift,
        "remaining_count": count,
        "status": "submitted",
    }


def add_wanted_by_date(
    db: Session, nurse_id: str, group_id: str,
    year: int, month: int, date: str, shift_code: str,
    *, comment: str = "", preview_only: bool = True,
) -> dict:
    """Add a new date to wanted submissions."""
    month_str = _wanted_request_month_str(year, month)
    old_wr = _get_latest_submitted_request(db, nurse_id, month_str)
    old_rid = old_wr.request_id if old_wr else None

    # Check duplicate
    if old_rid is not None:
        dup = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == old_rid,
                NurseShiftRequest.shift_date == _parse_date(date),
            )
            .first()
        )
        if dup:
            return {"error": f"{date}에 이미 {dup.shift} 원티드가 있습니다. 수정을 원하시면 변경 요청을 해주세요."}

    shifts_table_id = _resolve_shifts_table_id(db, group_id, shift_code)

    if preview_only:
        msg = f"{date}에 {shift_code} 원티드를 추가합니다."
        if comment:
            msg = f"{date}에 '{comment}' 사유로 {shift_code} 원티드를 추가합니다."
        return {
            "preview_only": True,
            "action": "add_wanted_date",
            "nurse_id": nurse_id,
            "date": date,
            "shift": shift_code,
            "comment": comment,
            "message": msg,
        }

    draft, count = _create_draft_with_copy(
        db, nurse_id, month_str, old_rid,
        group_id=group_id,
        add_date=date,
        add_shift=shift_code,
        add_shifts_table_id=shifts_table_id,
        add_comment=comment,
    )
    _submit_draft(db, draft)
    return {
        "action": "add_wanted_date",
        "nurse_id": nurse_id,
        "request_id": draft.request_id,
        "date": date,
        "shift": shift_code,
        "comment": comment,
        "total_count": count,
        "status": "submitted",
    }


def modify_wanted_by_date(
    db: Session, nurse_id: str, group_id: str,
    year: int, month: int, date: str, new_shift_code: str,
    *, comment: str | None = None, preview_only: bool = True,
) -> dict:
    """Modify the shift of an existing wanted date."""
    month_str = _wanted_request_month_str(year, month)
    old_wr = _get_latest_submitted_request(db, nurse_id, month_str)
    if not old_wr:
        return {"error": "제출된 원티드가 없습니다."}

    target = (
        db.query(NurseShiftRequest)
        .filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == old_wr.request_id,
            NurseShiftRequest.shift_date == _parse_date(date),
        )
        .first()
    )
    if not target:
        return {"error": f"{date}에 해당하는 원티드가 없습니다."}

    old_shift = target.shift
    new_shifts_table_id = _resolve_shifts_table_id(db, group_id, new_shift_code)

    if preview_only:
        msg = f"{date} 원티드를 {old_shift} → {new_shift_code}로 변경합니다."
        if comment:
            msg = f"{date} 원티드를 '{comment}' 사유로 {old_shift} → {new_shift_code}로 변경합니다."
        return {
            "preview_only": True,
            "action": "modify_wanted_date",
            "nurse_id": nurse_id,
            "date": date,
            "old_shift": old_shift,
            "new_shift": new_shift_code,
            "comment": comment or "",
            "message": msg,
        }

    draft, count = _create_draft_with_copy(
        db, nurse_id, month_str, old_wr.request_id,
        group_id=group_id,
        modify_date=date,
        modify_shift=new_shift_code,
        modify_shifts_table_id=new_shifts_table_id,
        modify_comment=comment,
    )
    _submit_draft(db, draft)
    return {
        "action": "modify_wanted_date",
        "nurse_id": nurse_id,
        "request_id": draft.request_id,
        "date": date,
        "old_shift": old_shift,
        "new_shift": new_shift_code,
        "comment": comment or "",
        "total_count": count,
        "status": "submitted",
    }
