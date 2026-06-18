from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from db.models import Nurse, NurseAssignment, NurseMonthlyLimit, RosterConfig
from schemas.auth_schema import User as UserSchema
from services.group_access import (
    assert_caller_can_access_group,
    caller_is_head_nurse,
    resolve_home_group_id,
)
from schemas.roster_schema import (
    NurseMonthlyLimitItem,
    NurseMonthlyLimitMeta,
    NurseMonthlyLimitWarning,
)
from services.day_windows import build_blocked_days
from services.nurse_effective import get_active_assignment, get_nurse_effective_attr


_SHIFT_PREFIXES = ("d", "e", "n", "o")
_BOUND_SUFFIXES = ("min", "max", "exact")
_RECOMMENDED_OVERRIDE_RATIO = 0.30


class _EffectiveNurseView:
    """파견 인바운드 capability 오버레이를 입힌 읽기전용 nurse 뷰.

    세션의 Nurse 객체를 변형하지 않고 지정 attr만 override, 나머지는 원본에 위임한다.
    monthly-limit 검증이 파견 '대상 그룹' 기준의 유효 capability(is_night_nurse=target_shift_types)를
    보도록 하기 위함. (엔진 roster_create_service 의 inbound override 와 동일 규칙)
    """

    __slots__ = ("_nurse", "_overrides")

    def __init__(self, nurse: Nurse, overrides: Dict[str, Any]):
        object.__setattr__(self, "_nurse", nurse)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_nurse"), name)


def _effective_nurse_for_group(
    db: Session, nurse: Nurse, group_id: str, year: int, month: int
) -> Any:
    """파견 인바운드면 해당 대상 그룹의 capability 오버레이를 입힌 effective nurse 뷰 반환.

    그 달에 group_id 를 target 으로 하는 활성 NurseAssignment 가 있으면
    is_night_nurse(←target_shift_types)·fixed_shift(←target_fixed_shift) 를 override.
    없으면 원본 nurse 그대로 폴백. (월말 as_of 기준 — 파견이 월 중 시작해도 포함)
    """
    as_of = date(year, month, monthrange(year, month)[1])
    assignment = get_active_assignment(db, str(nurse.nurse_id), as_of)
    if assignment is None or str(getattr(assignment, "target_group_id", "")) != str(group_id):
        return nurse
    overrides: Dict[str, Any] = {
        attr: get_nurse_effective_attr(db, nurse, attr, as_of, _assignment_cache=assignment)
        for attr in ("is_night_nurse", "fixed_shift")
    }
    return _EffectiveNurseView(nurse, overrides)


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
    assignments: Optional[List[NurseAssignment]] = None,
) -> int:
    month_start = date(year, month, 1)
    days_in_month = monthrange(year, month)[1]
    # assignments 가 주어지면(배치 prefetch) 재조회하지 않는다(N+1 제거).
    if assignments is None:
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
    # HN multi-group: 홈/original/관리 그룹(파견·병동이동 HN 포함)이면 허용. 홈 한정이 아니다.
    assert_caller_can_access_group(db, current_user, group_id)
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
    gid = group_id or resolve_home_group_id(db, current_user)
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
    if not current_user.is_master_admin and not caller_is_head_nurse(db, current_user):
        raise HTTPException(status_code=403, detail="수간호사 또는 관리자만 수정할 수 있습니다.")

    if not limits:
        return [], None, []

    normalized: List[Dict[str, Any]] = []
    seen_scopes = set()
    _managed_cache: Optional[set] = None
    for raw in limits:
        # [야간 고정/최대 상호배타] 한 행에 n_exact·n_max 동시 입력 금지.
        # (프론트에서 원천 차단하지만, normalize 가 exact→max 동기화로 n_max 를 덮어써
        #  조용히 손실되므로 방어적으로 명시 차단한다.)
        if raw.get("n_exact") is not None and raw.get("n_max") is not None:
            raise HTTPException(
                status_code=400,
                detail="야간 개수는 '고정'(n_exact) 또는 '최대'(n_max) 중 하나만 입력할 수 있습니다.",
            )
        row = _normalize_row(raw)
        if int(row.get("year")) != year or int(row.get("month")) != month:
            raise HTTPException(status_code=400, detail="요청 year/month와 항목 year/month가 일치해야 합니다.")
        # HN multi-group: 홈/original/관리 그룹이면 수정 허용(조회와 동일 스코프).
        assert_caller_can_access_group(db, current_user, row.get("group_id"))
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
        warn_night_dedicated_low_n,
        build_validation_payload,
    )

    by_nurse: Dict[str, List[Dict[str, Any]]] = {}
    for r in normalized:
        by_nurse.setdefault(str(r["nurse_id"]), []).append(r)

    # ── 배치 prefetch (N+1 제거) — 간호사/기존한도/배정을 한 번에 조회 ──
    #   일괄 저장(수십 명) 시 간호사별 개별 쿼리가 누적돼 느려지던 문제 해소.
    _nurse_ids = list(by_nurse.keys())
    pf_ids = set(_nurse_ids)
    pf_nurses: Dict[str, Nurse] = {
        str(n.nurse_id): n
        for n in db.query(Nurse).filter(Nurse.nurse_id.in_(_nurse_ids)).all()
    }
    _existing_all = (
        db.query(NurseMonthlyLimit)
        .filter(
            NurseMonthlyLimit.nurse_id.in_(_nurse_ids),
            NurseMonthlyLimit.year == year,
            NurseMonthlyLimit.month == month,
        )
        .all()
    )
    pf_existing_by_nurse: Dict[str, List[NurseMonthlyLimit]] = {}
    pf_rec_map: Dict[Tuple[str, str], NurseMonthlyLimit] = {}
    for _e in _existing_all:
        pf_existing_by_nurse.setdefault(str(_e.nurse_id), []).append(_e)
        pf_rec_map[(str(_e.nurse_id), str(_e.group_id))] = _e
    pf_assignments: Dict[str, List[NurseAssignment]] = {}
    for _a in (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id.in_(_nurse_ids),
            NurseAssignment.status != "cancelled",
        )
        .all()
    ):
        pf_assignments.setdefault(str(_a.nurse_id), []).append(_a)

    def _cap_assignments(nid: str) -> Optional[List[NurseAssignment]]:
        # prefetch 대상이면 캐시(없으면 빈 리스트=배정 없음), 아니면 None→개별 조회 폴백.
        return pf_assignments.get(nid, []) if nid in pf_ids else None

    issues_all: List[Dict[str, Any]] = []
    # soft 경고(저장은 허용, 프론트 토스트로 안내). 하드 차단 issues_all 과 분리.
    soft_warnings: List[Dict[str, Any]] = []

    def _push_issue(code: str, nurse_id: str, nurse_name: Optional[str], evidence: Dict[str, Any], msg: str, fixes: List[str]):
        issues_all.append({
            "reason_code": code,
            "severity": "blocking",
            "evidence": {**{"nurse_id": nurse_id}, **({"nurse_name": nurse_name} if nurse_name else {}), **evidence},
            "human_message_ko": msg,
            "fix_suggestions_ko": fixes,
        })

    # 그룹별 월간 N 상한(roster_config.max_nig_per_month) lazy 캐시.
    # 0/None은 솔버 런타임 보정(cp_sat_basic: <=0 → 15)과 동일하게 15로 본다.
    _max_night_cache: Dict[str, int] = {}

    def _group_max_night(gid: str) -> int:
        if gid not in _max_night_cache:
            _cfg = (
                db.query(RosterConfig)
                .filter(RosterConfig.group_id == gid)
                .order_by(RosterConfig.config_id.desc())
                .first()
            )
            _raw = int(getattr(_cfg, "max_nig_per_month", 0) or 0) if _cfg is not None else 0
            _max_night_cache[gid] = _raw if _raw > 0 else 15
        return _max_night_cache[gid]

    for nurse_id, rows in by_nurse.items():
        nurse = pf_nurses.get(str(nurse_id))
        if nurse is None:
            raise HTTPException(status_code=404, detail=f"간호사를 찾을 수 없습니다: {nurse_id}")

        # combine with existing other-group rows not included in this request
        existing = pf_existing_by_nurse.get(str(nurse_id), [])
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
                assignments=_cap_assignments(str(nurse_id)),
            )
            total_active_est += cap_days

            # 파견 인바운드 행은 대상 그룹 capability 오버레이(target_shift_types)를 반영한
            # effective nurse 로 검증한다. base nurses.is_night_nurse 만 보면 파견지에서
            # 가능해진 N 을 불가로 오판해 MONTHLY_LIMIT_NOT_IN_WORK_SHIFTS 로 오차단된다.
            eff_nurse = (
                _effective_nurse_for_group(db, nurse, str(rr.get("group_id")), year, month)
                if inbound else nurse
            )

            # 새 산술 모순 검사 (nurse 특성 + 강제 OFF + sum coverage 등)
            issues_all.extend(
                validate_monthly_limit_row(
                    row=rr, nurse=eff_nurse, cap_days=cap_days, year=year, month=month,
                    max_night=_group_max_night(str(rr.get("group_id"))),
                )
            )
            # soft 경고: N전담 + 낮은 N 한도(커버리지 의존이라 차단 대신 안내)
            soft_warnings.extend(
                warn_night_dedicated_low_n(
                    rr, nurse_id=nurse_id, nurse_name=nurse_name, nurse=eff_nurse, cap_days=cap_days,
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
                    assignments=_cap_assignments(nid),
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
        # 사용자 데이터 모순 → 422(서버 오류 500 아님).
        raise HTTPException(status_code=422, detail=payload)

    # upsert/delete
    for row in normalized:
        rec = pf_rec_map.get((str(row["nurse_id"]), str(row["group_id"])))
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
    items, meta, warnings = _list_by_year_month(
        db,
        current_user,
        year,
        month,
        group_id=next(iter(groups_touched)) if len(groups_touched) == 1 else None,
    )
    # soft 경고 병합(중복 message 제거). 프론트는 onSuccess 에서 warnings 를 토스트로 띄운다.
    if soft_warnings:
        merged = list(warnings or [])
        _seen_msgs = {w.message for w in merged}
        for sw in soft_warnings:
            if sw["message"] in _seen_msgs:
                continue
            _seen_msgs.add(sw["message"])
            merged.append(NurseMonthlyLimitWarning(code=sw["code"], message=sw["message"]))
        warnings = merged
    return items, meta, warnings


def night_bulk_apply_service(
    db: Session,
    current_user: UserSchema,
    *,
    group_id: str,
    year: int,
    month: int,
    kind: str,
    value: int,
) -> Tuple[List[NurseMonthlyLimitItem], Optional[NurseMonthlyLimitMeta], List[NurseMonthlyLimitWarning]]:
    """현재 병동(group_id)·현재월(year/month)의 야간 가능 active 근무자 '전체'에
    동일한 나이트 한도를 일괄 적용한다(kind=fixed→n_exact 고정, max→n_max 최대).

    검증(필드별)·조합 에러(_ko)·upsert 는 upsert_nurse_monthly_limits_service 를
    그대로 재사용한다(단일 진실원천). 여기서는 명단 resolve + 균일 limits 조립만 한다.
    나이트 개수는 N 가능자에게만 의미가 있으므로 check_group_n_pool 과 동일 기준으로
    N 가능 근무자만 대상으로 한다.
    """
    from services.precheck.monthly_limit_validator import _allowed_work_shifts

    field = "n_exact" if kind == "fixed" else "n_max"

    nurses = (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )
    n_capable = [
        nu for nu in nurses
        if "N" in (_allowed_work_shifts(nu) or {"D", "E", "N"})
    ]
    if not n_capable:
        raise HTTPException(
            status_code=400, detail="해당 병동에 야간 가능 근무자가 없습니다."
        )

    limits = [
        {
            "nurse_id": str(nu.nurse_id),
            "group_id": group_id,
            "year": year,
            "month": month,
            field: value,
        }
        for nu in n_capable
    ]
    return upsert_nurse_monthly_limits_service(db, current_user, year, month, limits)


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
