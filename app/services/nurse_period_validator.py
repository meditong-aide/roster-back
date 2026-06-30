# app/services/nurse_period_validator.py
"""allowed_shifts(시점) 저장 시 cross-attribute 모순 검증.

split 모델에서 allowed_shifts 를 저장할 때, 그 구간이 덮는 달의 nurse_monthly_limits
및 fixed_shift(period)와의 결정론적 모순을 저장 시점에 차단한다(생성 전 hard gate).

기존 monthly_limit_validator 의 check_allowed_vs_limits 를 재사용(SSOT) — 역방향.
설계: docs/NURSE_ATTRIBUTE_PERIOD_DESIGN.md, monthly_limit_validator.py.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import or_

from db.models import (
    Nurse, RosterConfig, NurseMonthlyLimit,
    NurseAllowedShiftPeriod,
)
from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes
from services.precheck.monthly_limit_validator import check_allowed_vs_limits, _make_issue

_WORK_MAIN = {"D", "E", "N", "M"}


def _ym(d: date) -> int:
    return d.year * 12 + (d.month - 1)


def _effective_end(db, nurse_id: str, valid_from: date) -> Optional[date]:
    """새 allowed 구간의 실제 끝 = valid_from 이후 가장 가까운 기존 구간 시작(없으면 None=계속)."""
    nxt = (
        db.query(NurseAllowedShiftPeriod)
        .filter(NurseAllowedShiftPeriod.nurse_id == nurse_id,
                NurseAllowedShiftPeriod.valid_from > valid_from)
        .order_by(NurseAllowedShiftPeriod.valid_from.asc())
        .first()
    )
    return nxt.valid_from if nxt else None


def _limit_row_to_dict(r: NurseMonthlyLimit) -> Dict[str, Any]:
    return {
        f"{p}_{k}": getattr(r, f"{p}_{k}", None)
        for p in ("d", "e", "n", "o") for k in ("min", "max", "exact")
    }


def validate_allowed_shift_period(
    db, nurse_id: str, group_id: Optional[str], allowed_value: Any,
    valid_from: date, *, use_mid: bool = False,
) -> List[Dict[str, Any]]:
    """allowed_shifts 변경([valid_from, 다음구간))이 그 달 월한도/fixed_shift와 모순이면 issue 목록.

    빈 set(제한 없음)이면 모순 불가 → []. blocking issue 가 하나라도 있으면 호출측이 422.
    """
    allowed_set = normalize_allowed_shift_codes(allowed_value, use_mid=use_mid)
    if not allowed_set:
        return []  # 제한 없음 = 모순 없음

    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    nurse_name = getattr(nurse, "name", None)
    gid = group_id or getattr(nurse, "group_id", None)

    cfg = (
        db.query(RosterConfig)
        .filter(RosterConfig.group_id == gid)
        .order_by(RosterConfig.created_at.desc())
        .first()
    ) if gid else None
    max_night = int(getattr(cfg, "max_nig_per_month", 0) or 0) if cfg else 0

    end = _effective_end(db, nurse_id, valid_from)
    lo, hi = _ym(valid_from), (_ym(end) if end else None)   # hi exclusive

    issues: List[Dict[str, Any]] = []

    # ── 1) 월 한도 모순 (그 구간이 덮는 달들) ──
    q = db.query(NurseMonthlyLimit).filter(NurseMonthlyLimit.nurse_id == nurse_id)
    if gid:
        q = q.filter(NurseMonthlyLimit.group_id == gid)
    for r in q.all():
        rym = r.year * 12 + (r.month - 1)
        if rym < lo or (hi is not None and rym >= hi):
            continue
        month_issues = check_allowed_vs_limits(
            allowed_set, _limit_row_to_dict(r),
            nurse_id=nurse_id, nurse_name=nurse_name, max_night=max_night,
        )
        for iss in month_issues:                      # 달 컨텍스트 부착
            iss["evidence"]["year"] = r.year
            iss["evidence"]["month"] = r.month
        issues.extend(month_issues)

    # ── 2) fixed_shift 교차: 고정 근무형이 허용 밖이면 모순 ──
    # fixed_shift 는 같은 satellite(nurse_allowed_shift_period)의 형제 컬럼.
    fx_rows = (
        db.query(NurseAllowedShiftPeriod)
        .filter(NurseAllowedShiftPeriod.nurse_id == nurse_id,
                NurseAllowedShiftPeriod.valid_from < (end or date(9999, 12, 31)),
                or_(NurseAllowedShiftPeriod.valid_to.is_(None),
                    NurseAllowedShiftPeriod.valid_to > valid_from))
        .all()
    )
    for fx in fx_rows:
        code = str(getattr(fx, "fixed_shift", "") or "").strip().upper()
        if not code:
            continue
        fixed_main = code[0]
        if fixed_main in _WORK_MAIN and fixed_main not in allowed_set:
            issues.append(_make_issue(
                "ALLOWED_VS_FIXED_SHIFT_CONFLICT",
                nurse_id=nurse_id, nurse_name=nurse_name,
                evidence={"fixed_shift": code, "fixed_main": fixed_main,
                          "allowed": sorted(allowed_set)},
                human_message_ko=(
                    f"{nurse_name or nurse_id} 간호사의 고정 근무형 '{code}'({fixed_main})이 "
                    f"가능 시프트 {sorted(allowed_set)} 밖입니다. 동시에 적용할 수 없습니다."
                ),
                fix_suggestions_ko=[
                    f"고정 근무형을 해제하거나 가능 시프트에 {fixed_main}를 추가하세요.",
                ],
            ))

    return issues
