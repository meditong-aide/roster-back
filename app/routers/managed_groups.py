"""근무표 만들기 페이지의 게시판 형태 메타 API.

각 그룹별 해당 year/month 의 schedule 버전 메타만 가볍게 반환하여
초기 아코디언 비활성 상태에서 일괄 노출할 수 있도록 한다.
실제 schedule 본문은 카드 클릭 시 기존 엔드포인트로 조회한다.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from db.client2 import get_db
from db.models import Group as GroupModel
from db.models import Schedule as ScheduleModel
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.group_access import resolve_managed_group_ids


router = APIRouter(
    prefix="/managed-groups",
    tags=["managed-groups"],
)


@router.get("/roster-create/summary")
async def get_roster_create_summary(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """관리 가능한 그룹별 해당 월 schedule 버전 메타 요약.

    응답 필드:
    - latest_schedule_id / latest_version / latest_status: version DESC LIMIT 1
    - has_draft: status='draft' 가 1개 이상 존재
    - has_issued: status='issued' 가 1개 이상 존재
    - default_schedule_id: 첫 오픈 기본값. 마감(issued) 중 최신 우선, 없으면 전체 최신
    - versions: 해당 월 전체 버전 목록 [{schedule_id, version, status}] (version DESC)
    - schedule 미존재 시 latest_*/default_schedule_id 는 null, has_* 는 false, versions 는 []
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    group_ids = resolve_managed_group_ids(db, current_user)
    if not group_ids:
        return JSONResponse(
            content=jsonable_encoder([]),
            media_type="application/json; charset=utf-8",
        )

    groups = (
        db.query(GroupModel)
        .filter(GroupModel.group_id.in_(group_ids))
        .all()
    )
    groups_map = {g.group_id: g for g in groups}

    schedules = (
        db.query(ScheduleModel)
        .filter(
            ScheduleModel.group_id.in_(group_ids),
            ScheduleModel.year == year,
            ScheduleModel.month == month,
            ScheduleModel.dropped == False,  # noqa: E712
        )
        .all()
    )

    by_group: dict = {}
    for s in schedules:
        by_group.setdefault(s.group_id, []).append(s)

    data: List[dict] = []
    for gid in group_ids:
        g = groups_map.get(gid)
        if not g:
            continue

        rows = sorted(
            by_group.get(gid, []),
            key=lambda r: int(r.version or 0),
            reverse=True,
        )
        latest = rows[0] if rows else None
        has_draft = any((r.status or "") == "draft" for r in rows)
        has_issued = any((r.status or "") == "issued" for r in rows)

        # 첫 오픈 기본값: 마감(issued) 중 최신 우선, 없으면 전체 최신
        issued_rows = [r for r in rows if (r.status or "") == "issued"]
        default = issued_rows[0] if issued_rows else latest

        data.append({
            "group_id": str(g.group_id),
            "group_name": str(g.group_name),
            "latest_schedule_id": str(latest.schedule_id) if latest else None,
            "latest_version": int(latest.version) if latest else None,
            "latest_status": latest.status if latest else None,
            "has_draft": has_draft,
            "has_issued": has_issued,
            "default_schedule_id": str(default.schedule_id) if default else None,
            "versions": [
                {
                    "schedule_id": str(r.schedule_id),
                    "version": int(r.version or 0),
                    "status": r.status,
                }
                for r in rows
            ],
        })

    return JSONResponse(
        content=jsonable_encoder(data),
        media_type="application/json; charset=utf-8",
    )
