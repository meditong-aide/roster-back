from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.client2 import get_db
from db.models import Group
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from schemas.grade_schema import GradeConfigResponse, GradeConfigUpsert
from services.grade_service import (
    get_grade_config_service,
    upsert_grade_config_service,
)
from services.group_access import assert_caller_can_access_group

router = APIRouter(
    prefix="/grade",
    tags=["grade"],
)


def _resolve_group_and_office(
    db: Session, current_user: UserSchema = Depends(get_current_user_from_cookie), group_id: Optional[str] = None
) -> tuple[str, str]:
    """유효한 group_id와 office_id를 반환합니다.

    정책:
    - query group_id 우선. 없으면 current_user.group_id fallback.
    - ADM/HN multi-group 의 경우 managed groups 안에 있어야 통과 (403 그 외).
    - office 경계 검증 유지.
    """
    target_group_id = group_id or getattr(current_user, "group_id", None)
    if not target_group_id:
        raise HTTPException(status_code=400, detail="group_id가 필요합니다.")

    assert_caller_can_access_group(db, current_user, target_group_id)

    group = db.query(Group).filter(Group.group_id == target_group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="그룹을 찾을 수 없습니다.")

    if getattr(current_user, "office_id", None) and group.office_id != current_user.office_id:
        raise HTTPException(status_code=403, detail="다른 오피스의 그룹입니다.")

    return group.group_id, group.office_id


@router.get("/config", response_model=GradeConfigResponse)
def get_grade_config(
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """그룹의 Grade 설정을 조회합니다."""

    print('[DEBUG] [grade.py - get_grade_config] group_id', group_id)
    print('[DEBUG] [grade.py - get_grade_config] current_user', current_user)
    try:
        target_group_id, _ = _resolve_group_and_office(db, current_user, group_id)
        return get_grade_config_service(db, target_group_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/config", response_model=GradeConfigResponse)
def upsert_grade_config(
    req: GradeConfigUpsert,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """그룹의 Grade 설정을 저장하거나 갱신합니다."""
    print('[DEBUG] [grade.py - upsert_grade_config] req', req)
    print('[DEBUG] [grade.py - upsert_grade_config] group_id', group_id)
    print('[DEBUG] [grade.py - upsert_grade_config] current_user', current_user)
    try:
        target_group_id, office_id = _resolve_group_and_office(db, current_user, group_id)
        return upsert_grade_config_service(db, office_id, target_group_id, req, current_user.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

