from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from schemas.team_schema import TeamBulkOpsRequest, TeamWithMembers
from services.team_service import list_teams_with_members, apply_team_ops
from services.group_access import assert_caller_can_access_group


router = APIRouter(prefix="/teams", tags=["teams"])


@router.get("", response_model=list[TeamWithMembers])
async def get_teams(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    group_id: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        target_group_id = group_id or current_user.group_id
        assert_caller_can_access_group(db, current_user, target_group_id)
        return list_teams_with_members(db, current_user.office_id, target_group_id)
    except HTTPException:
        raise
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
    target_group_id = group_id or current_user.group_id
    assert_caller_can_access_group(db, current_user, target_group_id)
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
