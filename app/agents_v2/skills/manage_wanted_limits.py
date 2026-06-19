"""manage-wanted-limits skill — 원티드 한도 초과자 조회 / 초과분 정리.

operation:
  list_over_limit   — wanted_max_requests 초과 제출한 간호사 목록 (read-only)
  delete_excess_off — 특정 간호사의 초과분 OFF 요청 삭제 (preview/apply)

정책:
  - HN/ADM only (mutation 은 middleware._MUTATION_SKILLS).
  - delete_excess_off 는 preview→apply.
  - 사용자에게 internal nurse_id 노출 금지. 이름으로 말하기.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from services.wanted_service import (
    delete_excess_off_requests,
    get_over_limit_nurses,
)


def _summarize_over_limit_message(items: list[dict], year: int, month: int) -> str:
    if not items:
        return f"{year}년 {month}월에 원티드 한도를 넘긴 간호사는 없어요."
    names = [str(i.get("nurse_name") or i.get("name") or i.get("nurse_id")) for i in items]
    head = ", ".join(names[:3])
    extra = f" 외 {len(items) - 3}명" if len(items) > 3 else ""
    return (
        f"{year}년 {month}월 원티드 한도를 넘긴 간호사가 {len(items)}명 있어요: {head}{extra}."
    )


@register("manage-wanted-limits")
def manage_wanted_limits(db: Session, params: dict) -> Any:
    """원티드 한도 관리.

    params:
      operation: list_over_limit | delete_excess_off (필수)
      group_id (필수, RBAC)
      year, month (필수)
      nurse_id (delete_excess_off 필수)
      nurse_name (delete_excess_off — UI 용 라벨, 없으면 nurse_id)
      preview_only (delete_excess_off): 기본 True
    """
    op = (params.get("operation") or "").lower()
    group_id = params.get("group_id")
    year = params.get("year")
    month = params.get("month")

    if not group_id:
        return {"error": "group_id required (RBAC scope)"}
    if year is None or month is None:
        return {"error": "year and month required"}
    if op not in ("list_over_limit", "delete_excess_off"):
        return {
            "needs_clarification": True,
            "question": "원티드 한도를 어떻게 처리할까요?",
            "options": [
                "한도 초과자 목록 보기 (list_over_limit)",
                "특정 간호사 초과분 정리 (delete_excess_off)",
            ],
        }

    if op == "list_over_limit":
        items = get_over_limit_nurses(db, int(year), int(month), group_id)
        return {
            "operation": "list_over_limit",
            "year": int(year),
            "month": int(month),
            "count": len(items),
            "items": items,
            "message": _summarize_over_limit_message(items, int(year), int(month)),
        }

    # op == "delete_excess_off"
    nurse_id = params.get("nurse_id")
    if not nurse_id:
        return {
            "needs_clarification": True,
            "question": "어떤 간호사의 초과분을 정리할까요? nurse_id 를 알려주세요.",
            "options": [],
        }
    label = params.get("nurse_name") or nurse_id
    preview_only = params.get("preview_only", True)

    if preview_only:
        # 실제 삭제 없이 현재 한도 초과 여부만 안내.
        items = get_over_limit_nurses(db, int(year), int(month), group_id)
        target = next(
            (i for i in items if i.get("nurse_id") == nurse_id), None
        )
        if not target:
            return {
                "preview": True,
                "operation": "delete_excess_off",
                "applied": False,
                "message": (
                    f"{label} 간호사는 {year}년 {month}월 원티드 한도를 초과하지 않았어요. 정리할 것이 없습니다."
                ),
                "_internal": {"nurse_id": nurse_id},
            }
        excess = target.get("excess_count") or target.get("excess") or 0
        return {
            "preview": True,
            "operation": "delete_excess_off",
            "year": int(year),
            "month": int(month),
            "target": {"nurse_name": label, "excess_count": excess},
            "message": (
                f"{label} 간호사의 {year}년 {month}월 원티드 초과분"
                + (f" ({excess}건)" if excess else "")
                + " 을 정리합니다. 진행할까요?"
            ),
            "_internal": {"nurse_id": nurse_id},
        }

    result = delete_excess_off_requests(db, nurse_id, int(year), int(month))
    deleted = result.get("deleted_count") or result.get("count") or 0
    return {
        "preview": False,
        "applied": True,
        "operation": "delete_excess_off",
        "year": int(year),
        "month": int(month),
        "deleted_count": deleted,
        "message": (
            f"{label} 간호사의 {year}년 {month}월 원티드 초과분 {deleted}건을 정리했어요."
            if deleted
            else f"{label} 간호사의 정리할 초과분이 없었어요."
        ),
        "_internal": {"nurse_id": nurse_id, "raw": result},
    }
