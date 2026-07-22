"""publish-schedule skill — 근무표 확정/발행 (issued 전환 + 스냅샷).

발행은 **위험연산**(간호사에게 공개, 이전 발행본 대체)이라 반드시 preview→승인 후 apply.
서비스로직(roster_service.publish_schedule_service) 직접 호출.

정책:
  - HN/ADM only (mutation).
  - preview_only=true(기본) → 미리보기 → 사용자 동의 후 false 로 발행.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import schedule_tools
from services.roster_service import publish_schedule_service


@register("publish-schedule")
def publish_schedule(db: Session, params: dict) -> Any:
    """근무표 발행.

    params: group_id/office_id/year/month(ctx 주입), acting_user_id, preview_only, issue_comment
    """
    group_id = params.get("group_id")
    office_id = params.get("office_id")
    year = params.get("year")
    month = params.get("month")
    if not group_id:
        return {"error": "group_id required (RBAC scope)"}
    if not (year and month):
        return {"needs_clarification": True, "question": "몇 월 근무표를 발행할까요? (예: 8월)", "options": []}

    meta = schedule_tools.resolve_target_schedule(db, group_id, int(year), int(month))
    if not meta:
        return {"error": f"{year}년 {month}월 발행할 근무표가 없습니다. 먼저 생성하거나 조회하세요."}
    sid, ver = meta["schedule_id"], meta.get("version")

    if params.get("preview_only", True):
        return {
            "preview": True, "operation": "publish_schedule",
            "summary": {"year": int(year), "month": int(month), "schedule_id": sid, "version": ver},
            "message": (f"{year}년 {month}월 근무표(v{ver})를 **확정·발행**합니다. "
                        "발행하면 간호사에게 공개되고 같은 달 이전 발행본은 대체됩니다. 진행할까요?"),
        }

    acting = params.get("acting_user_id") or params.get("nurse_id")
    res = publish_schedule_service(db, sid, acting, office_id, group_id,
                                   issue_comment=params.get("issue_comment", ""))
    if res.get("error"):
        return res
    return {
        "ok": True, "operation": "publish_schedule", **res,
        "message": (f"{year}년 {month}월 근무표를 발행했습니다 "
                    f"(버전 {res['version']}, 발행 스냅샷 저장 완료)."),
    }
