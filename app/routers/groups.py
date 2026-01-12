from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from sqlalchemy import text
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


# @router.get("")
# async def list_groups(
#     current_user: UserSchema = Depends(get_current_user_from_cookie),
#     db: Session = Depends(get_db)
# ):
#     """관리자 전용 병동(그룹) 리스트 조회."""
#     if not current_user or not current_user.is_master_admin:
#         raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")

#     rows = (
#         db.query(GroupModel).filter(GroupModel.office_id == current_user.office_id)
#         .all()
#     )
#     print('office_id', current_user.office_id)
#     return [
#         {
#             "group_id": g.group_id,
#             "group_name": g.group_name,
#             "office_id": g.office_id,
#         }
#         for g in rows
#     ]


@router.get("")
async def list_groups(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """관리자 전용 병동(그룹) 리스트 조회."""
    if not current_user or not current_user.is_master_admin:
        raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")

    # ORM 대신 raw SQL + CAST 사용으로 인코딩 문제 우회
    result = db.execute(
        text("""
            SELECT 
                group_id, 
                office_id, 
                CAST(group_name AS NVARCHAR(100)) AS group_name 
            FROM groups 
            WHERE office_id = :oid
            ORDER BY group_name  -- 필요시 정렬 기준 변경 가능
        """),
        {"oid": current_user.office_id}
    ).fetchall()

    # 디버깅용 로그 (필요시 주석 처리)
    if result:
        first_name = result[0][2]  # group_name 위치
        print("DEBUG - 첫 번째 group_name (CAST 후):", repr(first_name))
        print("DEBUG - 행 개수:", len(result))

    # Row 객체 → dict로 변환
    data = [
        {
            "group_id": str(row[0]),     # group_id
            "group_name": str(row[2]),   # CAST된 group_name
            "office_id": str(row[1]),    # office_id
        }
        for row in result
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


