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
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    if year is None or month is None:
        return {"error": "year/month required"}

    preview_only = params.get("preview_only", True)

    # 병동 전체 일괄(나이트 개수) — 기존 서비스(night_bulk_apply_service)를 그대로 재사용.
    # 대상자 선정·검증·조합에러가 그쪽 SSOT 이므로 에이전트가 루프를 돌면 규칙이 갈라진다.
    if str(params.get("scope") or "").strip() in ("전체", "ward", "all"):
        kind = params.get("bulk_kind") or ("고정" if params.get("n_exact") is not None else "최대")
        value = params.get("n_exact") if params.get("n_exact") is not None else params.get("n_max")
        if value is None:
            return {
                "error": "일괄 적용할 나이트 개수를 지정해 주세요.",
                "hint": "예: n_exact=4(정확히 4번) 또는 n_max=4(최대 4번)",
            }
        return nurse_monthly_limit_tools.bulk_night_limit(
            db, group_id, year, month,
            kind=kind, value=value,
            acting_user_id=params.get("acting_user_id"),
            preview_only=preview_only,
        )

    nurse_ids = params.get("nurse_ids") or []
    if not nurse_ids:
        return {"error": "nurse_id required (nurse_ids list 첫 entry 사용)"}
    nurse_id = nurse_ids[0]

    # 해제(설정 안 함) — unset 이 오면 지정 시프트(없으면 전체)의 한도를 NULL 로 기록.
    # 값 설정(updates)보다 우선 판정 — '월 한도 해제/없애줘/무제한' 의도.
    unset = params.get("unset")
    if unset is not None:
        shifts = unset if isinstance(unset, list) else None
        return nurse_monthly_limit_tools.unset_monthly_limit(
            db, nurse_id, group_id, year, month,
            shifts=shifts, preview_only=preview_only,
        )

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
            "hint": "해제(설정 안 함)하려면 unset=['all'] 또는 unset=['n'] 등을 사용하세요.",
        }

    return nurse_monthly_limit_tools.upsert_monthly_limit(
        db, nurse_id, group_id, year, month, updates,
        preview_only=preview_only,
    )
