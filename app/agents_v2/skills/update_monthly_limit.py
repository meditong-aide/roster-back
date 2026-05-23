"""update-monthly-limit skill — 개인별 월 D/E/N/O 한도 설정 (NurseMonthlyLimit upsert)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import nurse_monthly_limit_tools


@register("update-monthly-limit")
def update_monthly_limit(db: Session, params: dict) -> Any:
    """단일 간호사의 월 한도 (d/e/n/o × min/max/exact) upsert.

    Params:
        nurse_ids: [str] — 대상 간호사 (단일).
        group_id, year, month: 필수.
        n_exact / n_min / n_max / d_exact / d_min / d_max / e_* / o_* 중 1개 이상.
        preview_only: bool — 기본 True (sensitive mutation).
    """
    nurse_ids = params.get("nurse_ids") or []
    if not nurse_ids:
        return {"error": "nurse_id required (nurse_ids list 첫 entry 사용)"}
    nurse_id = nurse_ids[0]

    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    if year is None or month is None:
        return {"error": "year/month required"}

    # LIMIT_FIELDS 중 params 에 들어온 것만 추출
    updates = {
        f: params[f]
        for f in nurse_monthly_limit_tools.LIMIT_FIELDS
        if f in params and params[f] is not None
    }
    if not updates:
        return {
            "error": "no limit fields supplied",
            "allowed": list(nurse_monthly_limit_tools.LIMIT_FIELDS),
        }

    preview_only = params.get("preview_only", True)
    return nurse_monthly_limit_tools.upsert_monthly_limit(
        db, nurse_id, group_id, year, month, updates,
        preview_only=preview_only,
    )
