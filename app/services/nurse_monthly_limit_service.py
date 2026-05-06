from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from db.models import Nurse, NurseAssignment, NurseMonthlyLimit
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import NurseMonthlyLimitItem
from services.day_windows import build_blocked_days


_SHIFT_PREFIXES = ("d", "e", "n", "o")
_BOUND_SUFFIXES = ("min", "max", "exact")


def _normalize_row(row: Dict) -> Dict:
    out = dict(row)
    for p in _SHIFT_PREFIXES:
        exact_key = f"{p}_exact"
        min_key = f"{p}_min"
        max_key = f"{p}_max"
        exact_v = out.get(exact_key)
        if exact_v is not None:
            out[min_key] = exact_v
            out[max_key] = exact_v
        mn, mx = out.get(min_key), out.get(max_key)
        if mn is not None and mx is not None and mn > mx:
            raise HTTPException(
                status_code=400,
                detail=f"{p.upper()} 제한이 잘못되었습니다: min({mn}) > max({mx})",
            )
    return out


def _group_active_capacity_days(
    db: Session,
    nurse_id: str,
    group_id: str,
    year: int,
    month: int,
    inbound: bool,
) -> int:
    month_start = date(year, month, 1)
    days_in_month = monthrange(year, month)[1]
    assignments = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id == nurse_id,
            NurseAssignment.status != "cancelled",
        )
        .all()
    )
    blocked = build_blocked_days(
        assignments=assignments,
        nurse_db_id=str(nurse_id),
        group_id=str(group_id),
        month_start=month_start,
        days_in_month=days_in_month,
        is_inbound=inbound,
    )
    return max(0, days_in_month - len(blocked))


def _sum_min_required(row: Dict) -> int:
    total = 0
    for p in _SHIFT_PREFIXES:
        total += int(row.get(f"{p}_min") or 0)
    return total


def _sum_exact_required(row: Dict) -> int:
    total = 0
    for p in _SHIFT_PREFIXES:
        v = row.get(f"{p}_exact")
        if v is not None:
            total += int(v)
    return total


def list_nurse_monthly_limits_service(
    db: Session,
    current_user: UserSchema,
    year: int,
    month: int,
    group_id: Optional[str] = None,
) -> List[NurseMonthlyLimitItem]:
    gid = group_id or current_user.group_id
    q = db.query(NurseMonthlyLimit).filter(
        NurseMonthlyLimit.year == year,
        NurseMonthlyLimit.month == month,
    )
    if not current_user.is_master_admin:
        q = q.filter(NurseMonthlyLimit.group_id == gid)
    rows = q.all()
    return [
        NurseMonthlyLimitItem(
            nurse_id=r.nurse_id,
            group_id=r.group_id,
            year=r.year,
            month=r.month,
            d_min=r.d_min,
            d_max=r.d_max,
            d_exact=r.d_exact,
            e_min=r.e_min,
            e_max=r.e_max,
            e_exact=r.e_exact,
            n_min=r.n_min,
            n_max=r.n_max,
            n_exact=r.n_exact,
            o_min=r.o_min,
            o_max=r.o_max,
            o_exact=r.o_exact,
        )
        for r in rows
    ]


def upsert_nurse_monthly_limits_service(
    db: Session,
    current_user: UserSchema,
    year: int,
    month: int,
    limits: List[Dict],
) -> List[NurseMonthlyLimitItem]:
    if not current_user.is_master_admin and not current_user.is_head_nurse:
        raise HTTPException(status_code=403, detail="수간호사 또는 관리자만 수정할 수 있습니다.")

    if not limits:
        return []

    normalized: List[Dict] = []
    for raw in limits:
        row = _normalize_row(raw)
        if int(row.get("year")) != year or int(row.get("month")) != month:
            raise HTTPException(status_code=400, detail="요청 year/month와 항목 year/month가 일치해야 합니다.")
        if not current_user.is_master_admin and str(row.get("group_id")) != str(current_user.group_id):
            raise HTTPException(status_code=403, detail="현재 그룹 외 limits는 수정할 수 없습니다.")
        normalized.append(row)

    # cross-group consistency / capacity checks per nurse-month
    by_nurse: Dict[str, List[Dict]] = {}
    for r in normalized:
        by_nurse.setdefault(str(r["nurse_id"]), []).append(r)

    for nurse_id, rows in by_nurse.items():
        nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
        if nurse is None:
            raise HTTPException(status_code=404, detail=f"간호사를 찾을 수 없습니다: {nurse_id}")

        # combine with existing other-group rows not included in this request
        existing = (
            db.query(NurseMonthlyLimit)
            .filter(
                NurseMonthlyLimit.nurse_id == nurse_id,
                NurseMonthlyLimit.year == year,
                NurseMonthlyLimit.month == month,
            )
            .all()
        )
        req_keys = {(str(r["group_id"])) for r in rows}
        merged_rows = [dict(
            nurse_id=e.nurse_id,
            group_id=e.group_id,
            year=e.year,
            month=e.month,
            d_min=e.d_min, d_max=e.d_max, d_exact=e.d_exact,
            e_min=e.e_min, e_max=e.e_max, e_exact=e.e_exact,
            n_min=e.n_min, n_max=e.n_max, n_exact=e.n_exact,
            o_min=e.o_min, o_max=e.o_max, o_exact=e.o_exact,
        ) for e in existing if str(e.group_id) not in req_keys]
        merged_rows.extend(rows)

        days_in_month = monthrange(year, month)[1]
        total_active_est = 0
        for rr in merged_rows:
            inbound = str(rr.get("group_id")) != str(nurse.group_id)
            cap_days = _group_active_capacity_days(
                db,
                nurse_id=nurse_id,
                group_id=str(rr.get("group_id")),
                year=year,
                month=month,
                inbound=inbound,
            )
            total_active_est += cap_days
            exact_sum = _sum_exact_required(rr)
            if exact_sum > cap_days:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{nurse_id} / group {rr.get('group_id')} 설정 불가: "
                        f"exact 합({exact_sum}) > 그룹 가용일({cap_days})"
                    ),
                )
            min_sum = _sum_min_required(rr)
            if min_sum > cap_days:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{nurse_id} / group {rr.get('group_id')} 설정 불가: "
                        f"min 합({min_sum}) > 그룹 가용일({cap_days})"
                    ),
                )

        # 월 전체 합산 검증(그룹별 row 모순 방지)
        month_active_upper = min(days_in_month, total_active_est)
        total_exact_all = sum(_sum_exact_required(r) for r in merged_rows)
        total_min_all = sum(_sum_min_required(r) for r in merged_rows)
        if total_exact_all > month_active_upper:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{nurse_id} 월 합산 설정 불가: exact 합({total_exact_all}) > 월 가용일({month_active_upper})"
                ),
            )
        if total_min_all > month_active_upper:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{nurse_id} 월 합산 설정 불가: min 합({total_min_all}) > 월 가용일({month_active_upper})"
                ),
            )

    # upsert
    for row in normalized:
        rec = (
            db.query(NurseMonthlyLimit)
            .filter(
                NurseMonthlyLimit.nurse_id == row["nurse_id"],
                NurseMonthlyLimit.group_id == row["group_id"],
                NurseMonthlyLimit.year == year,
                NurseMonthlyLimit.month == month,
            )
            .first()
        )
        payload = {
            "d_min": row.get("d_min"), "d_max": row.get("d_max"), "d_exact": row.get("d_exact"),
            "e_min": row.get("e_min"), "e_max": row.get("e_max"), "e_exact": row.get("e_exact"),
            "n_min": row.get("n_min"), "n_max": row.get("n_max"), "n_exact": row.get("n_exact"),
            "o_min": row.get("o_min"), "o_max": row.get("o_max"), "o_exact": row.get("o_exact"),
        }
        if rec is None:
            rec = NurseMonthlyLimit(
                nurse_id=row["nurse_id"],
                group_id=row["group_id"],
                year=year,
                month=month,
                **payload,
            )
            db.add(rec)
        else:
            for k, v in payload.items():
                setattr(rec, k, v)

    db.commit()
    return list_nurse_monthly_limits_service(db, current_user, year, month)


def fetch_effective_monthly_limits_by_nurse(
    db: Session,
    year: int,
    month: int,
    nurse_ids: List[str],
    group_id: str,
) -> Dict[str, Dict]:
    if not nurse_ids:
        return {}
    rows = (
        db.query(NurseMonthlyLimit)
        .filter(
            NurseMonthlyLimit.year == year,
            NurseMonthlyLimit.month == month,
            NurseMonthlyLimit.group_id == group_id,
            NurseMonthlyLimit.nurse_id.in_(nurse_ids),
        )
        .all()
    )
    out: Dict[str, Dict] = {}
    for r in rows:
        out[str(r.nurse_id)] = {
            "d_min": r.d_min, "d_max": r.d_max, "d_exact": r.d_exact,
            "e_min": r.e_min, "e_max": r.e_max, "e_exact": r.e_exact,
            "n_min": r.n_min, "n_max": r.n_max, "n_exact": r.n_exact,
            "o_min": r.o_min, "o_max": r.o_max, "o_exact": r.o_exact,
        }
    return out
