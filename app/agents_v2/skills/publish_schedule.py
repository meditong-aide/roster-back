"""publish-schedule skill — 근무표 마감(발행) 안내 전용. 에이전트는 마감하지 않는다.

정본 마감(dev `app/routers/roster.py` publish_roster)은 DB 수간호사·ADM 판정, 같은 달 마감 잠금,
근무표 행 잠금, N 연번 앵커, 연차 장부, 재마감 변경 확인, 간호사 알림까지 한 번에 한다.
에이전트가 부르던 복제본(services/roster_service.py 의 발행 복제 함수)은 이 중 하나도 따르지 않아(N81·M2)
병합 직후 쓰기를 끈다(int-0b). 정본 마감 서비스가 추출되면(dev-8a) 그것을 부르게 된다(int-7).

지금 동작: 대상(연·월·버전)을 확인해 알려 주고, 근무표 만들기 화면에서 사람이 마감하도록 안내한다.
승인 대기(preview)를 만들지 않는다 — '응' 으로 실행될 쓰기가 없기 때문이다.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import schedule_tools


@register("publish-schedule")
def publish_schedule(db: Session, params: dict) -> Any:
    """근무표 마감 안내. params: group_id/office_id/year/month(ctx 주입)."""
    group_id = params.get("group_id")
    year = params.get("year")
    month = params.get("month")
    if not group_id:
        return {"error": "group_id required (RBAC scope)"}
    if not (year and month):
        return {"needs_clarification": True, "question": "몇 월 근무표를 마감할까요? (예: 8월)", "options": []}

    meta = schedule_tools.resolve_target_schedule(db, group_id, int(year), int(month))
    if not meta:
        return {"error": f"{year}년 {month}월 근무표가 없습니다. 먼저 생성하거나 조회하세요."}
    ver = meta.get("version")
    already = bool(meta.get("is_published"))
    head = (f"{year}년 {month}월 근무표는 이미 마감돼 있습니다(v{ver}). 다시 마감하려면"
            if already else f"{year}년 {month}월 근무표(v{ver}) 마감은")
    return {
        "operation": "publish_schedule",
        "publish_disabled": True,
        "summary": {"year": int(year), "month": int(month), "version": ver, "already_issued": already},
        "navigate_target": "roster_create",
        "message": (
            f"{head} 근무표 만들기 화면에서 수간호사가 직접 해 주세요. "
            "에이전트는 마감하지 않습니다 — 화면 마감이 같은 달 잠금·변경 확인·간호사 알림을 함께 처리합니다."
        ),
    }
