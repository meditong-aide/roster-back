from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from db.models import Nurse, NurseAssignment, NurseMonthlyLimit, RosterConfig
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import (
    NurseMonthlyLimitItem,
    NurseMonthlyLimitMeta,
    NurseMonthlyLimitWarning,
)
from services.day_windows import build_blocked_days


_SHIFT_PREFIXES = ("d", "e", "n", "o")
_BOUND_SUFFIXES = ("min", "max", "exact")
_RECOMMENDED_OVERRIDE_RATIO = 0.30


def _normalize_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(row)
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


def _sum_min_required(row: Mapping[str, Any]) -> int:
    total = 0
    for p in _SHIFT_PREFIXES:
        total += int(row.get(f"{p}_min") or 0)
    return total


def _sum_exact_required(row: Mapping[str, Any]) -> int:
    total = 0
    for p in _SHIFT_PREFIXES:
        v = row.get(f"{p}_exact")
        if v is not None:
            total += int(v)
    return total


def _is_all_bounds_empty(row: Mapping[str, Any]) -> bool:
    for p in _SHIFT_PREFIXES:
        for s in _BOUND_SUFFIXES:
            if row.get(f"{p}_{s}") is not None:
                return False
    return True


def _compute_override_meta_and_warnings(
    db: Session,
    *,
    group_id: str,
    year: int,
    month: int,
) -> Tuple[NurseMonthlyLimitMeta, List[NurseMonthlyLimitWarning]]:
    active_nurse_count = (
        db.query(Nurse)
        .filter(
            Nurse.group_id == group_id,
            Nurse.active == 1,
        )
        .count()
    )
    target_nurse_count = (
        db.query(NurseMonthlyLimit.nurse_id)
        .filter(
            NurseMonthlyLimit.group_id == group_id,
            NurseMonthlyLimit.year == year,
            NurseMonthlyLimit.month == month,
        )
        .distinct()
        .count()
    )
    override_ratio = (
        float(target_nurse_count) / float(active_nurse_count)
        if active_nurse_count > 0
        else 0.0
    )
    meta = NurseMonthlyLimitMeta(
        target_nurse_count=target_nurse_count,
        active_nurse_count=active_nurse_count,
        override_ratio=override_ratio,
        recommended_ratio=_RECOMMENDED_OVERRIDE_RATIO,
    )
    warnings: List[NurseMonthlyLimitWarning] = []
    if override_ratio > _RECOMMENDED_OVERRIDE_RATIO:
        warnings.append(
            NurseMonthlyLimitWarning(
                code="OVERRIDE_RATIO_EXCEEDED",
                message=(
                    "개별 OFF cap 설정 인원이 권장 비율(30%)을 초과했습니다. "
                    f"현재 {target_nurse_count}/{active_nurse_count}명 "
                    f"({override_ratio * 100:.1f}%)."
                ),
            )
        )
    return meta, warnings


def _row_to_item(r: NurseMonthlyLimit) -> NurseMonthlyLimitItem:
    return NurseMonthlyLimitItem(
        nurse_id=str(r.nurse_id),
        group_id=str(r.group_id),
        year=int(r.year),
        month=int(r.month),
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


def list_nurse_monthly_limits_service(
    db: Session,
    current_user: UserSchema,
    group_id: str,
    nurse_id: str,
) -> List[NurseMonthlyLimitItem]:
    from services.group_access import can_caller_access_nurse
    if not can_caller_access_nurse(db, current_user, nurse_id):
        raise HTTPException(status_code=403, detail="현재 그룹 외 limits는 조회할 수 없습니다.")
    rows = (
        db.query(NurseMonthlyLimit)
        .filter(
            NurseMonthlyLimit.group_id == group_id,
            NurseMonthlyLimit.nurse_id == nurse_id,
        )
        .all()
    )
    return [_row_to_item(r) for r in rows]


def _list_by_year_month(
    db: Session,
    current_user: UserSchema,
    year: int,
    month: int,
    group_id: Optional[str] = None,
) -> Tuple[List[NurseMonthlyLimitItem], Optional[NurseMonthlyLimitMeta], List[NurseMonthlyLimitWarning]]:
    gid = group_id or current_user.group_id
    q = db.query(NurseMonthlyLimit).filter(
        NurseMonthlyLimit.year == year,
        NurseMonthlyLimit.month == month,
    )
    if current_user.is_master_admin:
        if group_id:
            q = q.filter(NurseMonthlyLimit.group_id == group_id)
    else:
        q = q.filter(NurseMonthlyLimit.group_id == gid)
    items = [_row_to_item(r) for r in q.all()]
    meta: Optional[NurseMonthlyLimitMeta] = None
    warnings: List[NurseMonthlyLimitWarning] = []
    if gid and (not current_user.is_master_admin or group_id is not None):
        meta, warnings = _compute_override_meta_and_warnings(
            db,
            group_id=str(gid),
            year=year,
            month=month,
        )
    return items, meta, warnings


def upsert_nurse_monthly_limits_service(
    db: Session,
    current_user: UserSchema,
    year: int,
    month: int,
    limits: List[Dict[str, Any]],
) -> Tuple[List[NurseMonthlyLimitItem], Optional[NurseMonthlyLimitMeta], List[NurseMonthlyLimitWarning]]:
    if not current_user.is_master_admin and not current_user.is_head_nurse:
        raise HTTPException(status_code=403, detail="수간호사 또는 관리자만 수정할 수 있습니다.")

    if not limits:
        return [], None, []

    normalized: List[Dict[str, Any]] = []
    seen_scopes = set()
    _managed_cache: Optional[set] = None
    for raw in limits:
        row = _normalize_row(raw)
        if int(row.get("year")) != year or int(row.get("month")) != month:
            raise HTTPException(status_code=400, detail="요청 year/month와 항목 year/month가 일치해야 합니다.")
        if not current_user.is_master_admin:
            row_gid = str(row.get("group_id"))
            if row_gid != str(current_user.group_id):
                # HN multi-group: 본인이 관리하는 group 인지 검증
                if _managed_cache is None:
                    from services.group_access import resolve_managed_group_ids
                    _managed_cache = {str(g) for g in resolve_managed_group_ids(db, current_user)}
                if row_gid not in _managed_cache:
                    raise HTTPException(status_code=403, detail="현재 그룹 외 limits는 수정할 수 없습니다.")
        scope = (
            str(row.get("nurse_id")),
            str(row.get("group_id")),
            int(row.get("year")),
            int(row.get("month")),
        )
        if scope in seen_scopes:
            raise HTTPException(
                status_code=400,
                detail=(
                    "동일한 (nurse_id, group_id, year, month) 항목이 요청에 중복되었습니다: "
                    f"{scope[0]}, {scope[1]}, {scope[2]}-{scope[3]:02d}"
                ),
            )
        seen_scopes.add(scope)
        normalized.append(row)

    # cross-group consistency / capacity checks per nurse-month
    from services.precheck.monthly_limit_validator import (
        validate_monthly_limit_row,
        build_validation_payload,
    )

    by_nurse: Dict[str, List[Dict[str, Any]]] = {}
    for r in normalized:
        by_nurse.setdefault(str(r["nurse_id"]), []).append(r)

    issues_all: List[Dict[str, Any]] = []

    def _push_issue(code: str, nurse_id: str, nurse_name: Optional[str], evidence: Dict[str, Any], msg: str, fixes: List[str]):
        issues_all.append({
            "reason_code": code,
            "severity": "blocking",
            "evidence": {**{"nurse_id": nurse_id}, **({"nurse_name": nurse_name} if nurse_name else {}), **evidence},
            "human_message_ko": msg,
            "fix_suggestions_ko": fixes,
        })

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
        nurse_name = getattr(nurse, "name", None)
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

            # 새 산술 모순 검사 (nurse 특성 + 강제 OFF + sum coverage 등)
            issues_all.extend(
                validate_monthly_limit_row(
                    row=rr, nurse=nurse, cap_days=cap_days, year=year, month=month,
                )
            )

            # 기존 검사: 그룹별 sum
            exact_sum = _sum_exact_required(rr)
            if exact_sum > cap_days:
                _push_issue(
                    "MONTHLY_LIMIT_GROUP_EXACT_SUM_EXCEEDS",
                    nurse_id, nurse_name,
                    {"group_id": rr.get("group_id"), "exact_sum": exact_sum, "cap_days": cap_days},
                    f"그룹 {rr.get('group_id')}의 exact 합({exact_sum})이 그룹 가용일 {cap_days}일을 초과합니다.",
                    [f"exact 합을 {cap_days} 이하로 설정하세요."],
                )
            min_sum = _sum_min_required(rr)
            if min_sum > cap_days:
                _push_issue(
                    "MONTHLY_LIMIT_GROUP_MIN_SUM_EXCEEDS",
                    nurse_id, nurse_name,
                    {"group_id": rr.get("group_id"), "min_sum": min_sum, "cap_days": cap_days},
                    f"그룹 {rr.get('group_id')}의 min 합({min_sum})이 그룹 가용일 {cap_days}일을 초과합니다.",
                    [f"min 합을 {cap_days} 이하로 설정하세요."],
                )

        # 월 전체 합산 검증(그룹별 row 모순 방지)
        month_active_upper = min(days_in_month, total_active_est)
        total_exact_all = sum(_sum_exact_required(r) for r in merged_rows)
        total_min_all = sum(_sum_min_required(r) for r in merged_rows)
        if total_exact_all > month_active_upper:
            _push_issue(
                "MONTHLY_LIMIT_MONTH_EXACT_SUM_EXCEEDS",
                nurse_id, nurse_name,
                {"total_exact": total_exact_all, "month_active": month_active_upper},
                f"월 합산 exact({total_exact_all})이 월 가용일 {month_active_upper}일을 초과합니다.",
                ["월 전체 그룹의 exact 합을 줄이세요."],
            )
        if total_min_all > month_active_upper:
            _push_issue(
                "MONTHLY_LIMIT_MONTH_MIN_SUM_EXCEEDS",
                nurse_id, nurse_name,
                {"total_min": total_min_all, "month_active": month_active_upper},
                f"월 합산 min({total_min_all})이 월 가용일 {month_active_upper}일을 초과합니다.",
                ["월 전체 그룹의 min 합을 줄이세요."],
            )

    # ── 그룹 단위 N pool 산술 검사 (cross-nurse) ──
    from services.precheck.monthly_limit_validator import (
        check_group_n_pool,
        _allowed_work_shifts,  # private이지만 같은 패키지 활용
    )

    group_ids_in_request = {str(r["group_id"]) for r in normalized}
    days_in_month_full = monthrange(year, month)[1]

    for gid in group_ids_in_request:
        try:
            # RosterConfig → daily N 요구치
            roster_cfg = (
                db.query(RosterConfig)
                .filter(RosterConfig.group_id == gid)
                .order_by(RosterConfig.config_id.desc())
                .first()
            )
            if roster_cfg is None:
                continue
            n_daily = int(getattr(roster_cfg, "nig_req", 0) or 0)
            if n_daily <= 0:
                continue  # N 요구 없으면 검사 skip
            monthly_n_demand = n_daily * days_in_month_full
            # daily N max는 RosterConfig에 직접 저장 안 됨; 보수적으로 None.
            daily_n_max_total = None

            # group의 모든 nurse + N 가능 여부
            group_nurses = (
                db.query(Nurse)
                .filter(Nurse.group_id == gid, Nurse.active == 1)
                .all()
            )
            n_capable_nurses = [
                nu for nu in group_nurses
                if "N" in (_allowed_work_shifts(nu) or {"D", "E", "N"})
            ]
            total_n_capable = len(n_capable_nurses)

            # 요청 + 기존 limit 병합 (nurse_id 단위)
            request_by_nurse: Dict[str, Dict[str, Any]] = {
                str(r["nurse_id"]): r for r in normalized if str(r["group_id"]) == gid
            }
            existing_limits = (
                db.query(NurseMonthlyLimit)
                .filter(
                    NurseMonthlyLimit.group_id == gid,
                    NurseMonthlyLimit.year == year,
                    NurseMonthlyLimit.month == month,
                )
                .all()
            )
            existing_by_nurse = {str(e.nurse_id): e for e in existing_limits}

            forced_n_sum = 0
            nurses_with_n_forced = 0
            free_capacity_sum = 0

            for nu in n_capable_nurses:
                nid = str(nu.nurse_id)
                # request 우선, 없으면 기존, 없으면 free
                if nid in request_by_nurse:
                    rr = request_by_nurse[nid]
                    n_exact = rr.get("n_exact")
                    n_max = rr.get("n_max")
                else:
                    e = existing_by_nurse.get(nid)
                    n_exact = getattr(e, "n_exact", None) if e else None
                    n_max = getattr(e, "n_max", None) if e else None

                # 가용일 (그룹 active)
                inbound = False
                cap = _group_active_capacity_days(
                    db, nurse_id=nid, group_id=gid, year=year, month=month, inbound=inbound,
                )

                if n_exact is not None:
                    forced_n_sum += int(n_exact)
                    nurses_with_n_forced += 1
                elif n_max is not None:
                    free_capacity_sum += min(int(n_max), cap)
                else:
                    free_capacity_sum += cap

            pool_issues = check_group_n_pool(
                group_id=gid,
                monthly_n_demand=monthly_n_demand,
                forced_n_sum=forced_n_sum,
                free_capacity_sum=free_capacity_sum,
                nurses_with_n_forced=nurses_with_n_forced,
                total_n_capable_nurses=total_n_capable,
                daily_n_max_total=daily_n_max_total,
                days_in_month=days_in_month_full,
            )
            issues_all.extend(pool_issues)
        except Exception as _pool_exc:
            print(f"[MonthlyLimit] group N pool 검사 실패(무시) gid={gid}: {_pool_exc}")

    if issues_all:
        payload = build_validation_payload(issues_all)
        print(
            f"[MonthlyLimit][BLOCKING] {len(issues_all)}건 — "
            f"codes={sorted({i['reason_code'] for i in issues_all})}"
        )
        for i in issues_all[:5]:
            print(f"[MonthlyLimit][BLOCKING][message] {i.get('human_message_ko')}")
        raise HTTPException(status_code=500, detail=payload)

    # upsert/delete
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
        if _is_all_bounds_empty(payload):
            if rec is not None:
                db.delete(rec)
            continue
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
    groups_touched = {str(r.get("group_id")) for r in normalized}
    return _list_by_year_month(
        db,
        current_user,
        year,
        month,
        group_id=next(iter(groups_touched)) if len(groups_touched) == 1 else None,
    )


def fetch_effective_monthly_limits_by_nurse(
    db: Session,
    year: int,
    month: int,
    nurse_ids: List[str],
    group_id: str,
) -> Dict[str, Dict[str, Optional[int]]]:
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
    out: Dict[str, Dict[str, Optional[int]]] = {}
    for r in rows:
        out[str(r.nurse_id)] = {
            "d_min": r.d_min, "d_max": r.d_max, "d_exact": r.d_exact,
            "e_min": r.e_min, "e_max": r.e_max, "e_exact": r.e_exact,
            "n_min": r.n_min, "n_max": r.n_max, "n_exact": r.n_exact,
            "o_min": r.o_min, "o_max": r.o_max, "o_exact": r.o_exact,
        }
    return out
