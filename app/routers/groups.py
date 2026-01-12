from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from typing import List
import uuid

from db.client2 import get_db
from db.models import Group as GroupModel, Office as OfficeModel
from schemas.auth_schema import User as UserSchema
from routers.auth import get_current_user_from_cookie

router = APIRouter(
    prefix="/groups",
    tags=["groups"],
)


@router.get("")
async def list_groups(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """관리자 전용 병동(그룹) 리스트 조회."""
    if not current_user or not current_user.is_master_admin:
        raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")

    rows = (
        db.query(GroupModel).filter(GroupModel.office_id == current_user.office_id)
        .all()
    )
    
    data = [
        {
            "group_id": str(g.group_id),
            "group_name": str(g.group_name),    # 이제 str()만으로도 정상일 가능성 높음
            "office_id": str(g.office_id),
        }
        for g in rows
    ]

    return JSONResponse(
        content=jsonable_encoder(data),
        media_type="application/json; charset=utf-8"
    )


@router.post("")
async def create_group(
    payload: dict,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """새로운 병동(그룹) 생성. 입력: {"group_name": "간호3병동"}"""
    if not current_user or not current_user.is_master_admin:
        raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")

    group_name = (payload or {}).get("group_name")
    if not group_name or not str(group_name).strip():
        raise HTTPException(status_code=400, detail="group_name은 필수입니다.")

    # 그룹 ID 생성: office_id 접두 + 난수 6자리
    office_id = current_user.office_id
    if not office_id:
        raise HTTPException(status_code=400, detail="사용자 office_id가 필요합니다.")
    new_gid = f"{office_id}{uuid.uuid4().hex[:6]}"  # 충돌 희박

    g = GroupModel(group_id=new_gid, office_id=office_id, group_name=str(group_name).strip())
    db.add(g)
    db.commit()
    return {"group_id": new_gid, "office_name": g.group_name}


