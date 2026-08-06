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
    *,
    as_of: bool = True,
) -> dict | None:
    """단일 간호사의 월 한도 조회. 적용 한도 없으면 None.

    as_of=True(기본): 대상 (year, month) '이하 가장 최근' 행을 이월 적용한다 —
    생성 로더(fetch_effective_monthly_limits_by_nurse)·조회 서비스(_list_by_year_month)와
    동일한 as-of 의미. 월별 재설정을 안 해도 마지막 설정이 유지돼, 에이전트 조회가
    실제 생성에 쓰이는 값과 일치한다(과거엔 exact-month .first() 라 '이월된 값'을 못 보고
    '설정 없음'으로 잘못 답했다). 반환 dict 는 대상월로 표시하되 applied_from_* 에 실제
    출처 월을, carried_over 에 이월 여부를 담는다.

    as_of=False: 정확히 그 (year, month) 행만(이력·감사용).
    """
    if not as_of:
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
        return _to_dict(row) if row else None

    row = _resolve_asof_row(db, nurse_id, group_id, year, month)
    if row is None:
        return None
    return _to_dict(row, target_year=year, target_month=month)


def list_monthly_limits(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    *,
    nurse_ids: list[str] | None = None,
    as_of: bool = True,
) -> list[dict]:
    """병동 전체 (또는 일부) 의 월 한도 목록.

    as_of=True(기본): (nurse, group)별 대상월 이하 가장 최근 행 1건씩 이월 적용
    (get_monthly_limit 과 동일 의미). as_of=False: 정확히 그 달 행만.
    """
    if not as_of:
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
        return [{**_to_dict(r), "nurse_name": name} for r, name in q.all()]

    target_ym = year * 12 + month
    q = (
        db.query(NurseMonthlyLimit, Nurse.name)
        .outerjoin(Nurse, Nurse.nurse_id == NurseMonthlyLimit.nurse_id)
        .filter(
            NurseMonthlyLimit.group_id == group_id,
            (NurseMonthlyLimit.year * 12 + NurseMonthlyLimit.month) <= target_ym,
        )
    )
    if nurse_ids:
        q = q.filter(NurseMonthlyLimit.nurse_id.in_(nurse_ids))
    q = q.order_by(NurseMonthlyLimit.year.desc(), NurseMonthlyLimit.month.desc())
    seen: set[str] = set()
    out: list[dict] = []
    for r, name in q.all():
        nid = str(r.nurse_id)
        if nid in seen:
            continue  # 이미 더 최근(대상월에 가까운) 행을 담음
        seen.add(nid)
        out.append(
            {**_to_dict(r, target_year=year, target_month=month), "nurse_name": name}
        )
    return out


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

    # 가능 시프트 게이트 (PUT /monthly-limits 와 동일): 근무유형(allowed_shifts)에 없는
    # 시프트에 양수 한도(min/max/exact)는 저장 차단 — non-N 간호사에 N 한도를 넣어
    # 생성 시 infeasible 되는 것을 원천 차단(에이전트/온톨로지 우회 방지).
    from services.precheck.monthly_limit_validator import _check_work_shifts
    _nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    if _nurse is not None:
        _base = {f: getattr(row, f, None) for f in LIMIT_FIELDS} if row is not None else {}
        _ws_issues = _check_work_shifts(
            {**_base, **updates},
            nurse_id=nurse_id,
            nurse_name=getattr(_nurse, "name", None),
            nurse=_nurse,
        )
        if _ws_issues:
            return {
                "error": _ws_issues[0]["human_message_ko"],
                "reason_code": _ws_issues[0]["reason_code"],
                "issues": _ws_issues,
                "blocked": True,
            }

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


def unset_monthly_limit(
    db: Session,
    nurse_id: str,
    group_id: str,
    year: int,
    month: int,
    *,
    shifts: list[str] | None = None,
    preview_only: bool = False,
) -> dict:
    """월 한도 '해제'(설정 안 함) — 지정 시프트(없으면 전체)의 min/max/exact 를 NULL 로.

    행을 **삭제하지 않고** 해당 필드를 NULL 로 기록한다. 전체 해제 시 all-null '묘비' 행이
    되어 as-of 조회가 이 달에서 멈추고 과거 non-null 한도를 재상속하지 않는다(canonical
    upsert 서비스의 tombstone 규칙과 동일). shifts 미지정/["all"] = 12필드 전부 NULL.
    shifts=["n"] = n_min/n_max/n_exact 만 NULL(나머지 기존값 유지).
    """
    codes = _resolve_unset_shifts(shifts)
    if codes is None:
        return {
            "error": f"unknown shift codes: {shifts}",
            "allowed": list(_SHIFT_CODES) + ["all"],
        }
    updates = {f: None for f in LIMIT_FIELDS if f.split("_")[0] in codes}
    res = upsert_monthly_limit(
        db, nurse_id, group_id, year, month, updates, preview_only=preview_only
    )
    if "error" not in res:
        res["unset"] = True
        res["unset_shifts"] = sorted(codes)
    return res


# ── private ──────────────────────────────────────────────

_SHIFT_CODES = ("d", "e", "n", "o")


def _resolve_unset_shifts(shifts: list[str] | None) -> set[str] | None:
    """해제 대상 시프트 코드 집합. None/['all']/[] → 전체. 미지 코드 있으면 None(에러)."""
    if not shifts or any(str(s).lower() == "all" for s in shifts):
        return set(_SHIFT_CODES)
    codes = {str(s).lower() for s in shifts}
    if not codes <= set(_SHIFT_CODES):
        return None
    return codes


def _resolve_asof_row(
    db: Session, nurse_id: str, group_id: str, year: int, month: int
) -> NurseMonthlyLimit | None:
    """대상 (year, month) 이하 '가장 최근' 한도 행. 생성 로더와 동일 as-of 쿼리."""
    target_ym = year * 12 + month
    return (
        db.query(NurseMonthlyLimit)
        .filter(
            NurseMonthlyLimit.nurse_id == nurse_id,
            NurseMonthlyLimit.group_id == group_id,
            (NurseMonthlyLimit.year * 12 + NurseMonthlyLimit.month) <= target_ym,
        )
        .order_by(NurseMonthlyLimit.year.desc(), NurseMonthlyLimit.month.desc())
        .first()
    )


def _to_dict(
    row: NurseMonthlyLimit,
    *,
    target_year: int | None = None,
    target_month: int | None = None,
) -> dict:
    # target_* 지정 시(as-of 조회) '표시 월'은 대상월로 스탬프하고, 값의 실제 출처
    # (applied_from_*)는 행의 연/월로 둔다. 이월이면 carried_over=True.
    disp_year = target_year if target_year is not None else row.year
    disp_month = target_month if target_month is not None else row.month
    d = {
        "id": row.id,
        "nurse_id": row.nurse_id,
        "group_id": row.group_id,
        "year": disp_year,
        "month": disp_month,
        "applied_from_year": row.year,
        "applied_from_month": row.month,
        "carried_over": (row.year != disp_year or row.month != disp_month),
    }
    for f in LIMIT_FIELDS:
        d[f] = getattr(row, f)
    return d
