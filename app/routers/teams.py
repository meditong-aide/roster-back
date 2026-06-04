from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from schemas.team_schema import TeamBulkOpsRequest, TeamWithMembers
from services.team_service import list_teams_with_members, apply_team_ops
from services.team_classify_service import (
    preview_team_classification,
    apply_team_classification,
)
from services.group_access import resolve_managed_group_ids


router = APIRouter(prefix="/teams", tags=["teams"])


class TeamClassifyPreviewRequest(BaseModel):
    year: int
    month: int
    group_id: str | None = None  # master_admin 만 타 그룹 지정 가능


class TeamAssignmentItem(BaseModel):
    nurse_id: str
    team_id: int


class TeamClassifyApplyRequest(BaseModel):
    year: int
    month: int
    assignments: list[TeamAssignmentItem]
    group_id: str | None = None
    note: str | None = None


def _is_manager(u: UserSchema) -> bool:
    """팀 분류는 관리 작업 — ADM / 수간호사 / 그룹관리자(hn_auth=='HN')만 허용."""
    return bool(
        u.is_master_admin
        or u.is_head_nurse
        or str(u.hn_auth or "").upper() == "HN"
    )


def _resolve_managed_target(
    db: Session, current_user: UserSchema, group_id: str | None
) -> str:
    """관리 가능한 그룹으로 대상 group_id 해소.

    그룹관리자는 home 외에 Group.hn_id 에 본인이 등록된 그룹도 관리한다
    (resolve_managed_group_ids). 지정한 group_id 가 관리 목록에 없으면 403.
    관리 그룹이 여럿인데 미지정이면 모호하므로 400.
    """
    if not _is_manager(current_user):
        raise HTTPException(status_code=403, detail="팀 분류 권한이 없습니다.")
    managed = resolve_managed_group_ids(db, current_user)
    if not managed:
        raise HTTPException(status_code=403, detail="관리 권한이 있는 그룹이 없습니다.")
    if group_id:
        if group_id not in managed:
            raise HTTPException(status_code=403, detail="해당 그룹 관리 권한이 없습니다.")
        return group_id
    if len(managed) == 1:
        return managed[0]
    raise HTTPException(
        status_code=400, detail="관리 그룹이 여러 개입니다. group_id 를 지정하세요."
    )


@router.get("", response_model=list[TeamWithMembers])
async def get_teams(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    group_id: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        if current_user.is_master_admin:
            target_group_id = group_id
        else:
            target_group_id = current_user.group_id
        return list_teams_with_members(db, current_user.office_id, target_group_id)
    except Exception as e:
        print('[DEBUG] [teams.py - get_teams] office_id', current_user.office_id)
        print('[DEBUG] [teams.py - get_teams] group_id', group_id)
        print('[DEBUG] [teams.py - get_teams] current_user', current_user.__dict__)
        print('[DEBUG] [teams.py - get_teams] error', e)
        raise HTTPException(status_code=500, detail=f"팀 목록 조회 실패: {e}")


@router.put("", response_model=list[TeamWithMembers])
async def put_teams(
    body: TeamBulkOpsRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    group_id: str | None = None,
    db: Session = Depends(get_db),
):
    # if not current_user or (not current_user.is_head_nurse and not current_user.is_master_admin):
    #     raise HTTPException(status_code=403, detail="권한 없음")
    if current_user.is_master_admin:
        target_group_id = group_id
    else:
        target_group_id = current_user.group_id
    try:
        payload = [t.model_dump() for t in body.teams]
        return apply_team_ops(
            db,
            current_user.office_id,
            target_group_id,
            payload,
            delete_team_ids=body.delete_team_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print('[DEBUG] [teams.py - put_teams] office_id', current_user.office_id)
        print('[DEBUG] [teams.py - put_teams] group_id', current_user.group_id)
        print('[DEBUG] [teams.py - put_teams] current_user', current_user.__dict__)
        print('[DEBUG] [teams.py - put_teams] body.teams', body.teams)
        print('[DEBUG] [teams.py - put_teams] error', e)
        raise HTTPException(status_code=500, detail=f"팀 동기화 실패: {e}")


@router.post("/classify/preview")
async def classify_preview(
    body: TeamClassifyPreviewRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """원티드 기반 팀 분류 미리보기 (read-only). 확정 원티드로 제안 팀+변경 diff 반환."""
    target_group_id = _resolve_managed_target(db, current_user, body.group_id)
    try:
        return preview_team_classification(
            db, group_id=target_group_id, year=body.year, month=body.month,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/classify/apply")
async def classify_apply(
    body: TeamClassifyApplyRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """승인된 팀 분류를 permanent_change 이벤트로 발행 (대상월 1일 발효)."""
    target_group_id = _resolve_managed_target(db, current_user, body.group_id)
    try:
        return apply_team_classification(
            db,
            group_id=target_group_id,
            office_id=current_user.office_id,
            year=body.year,
            month=body.month,
            assignments=[a.model_dump() for a in body.assignments],
            note=body.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
