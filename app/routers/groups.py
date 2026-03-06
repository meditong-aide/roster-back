from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from typing import List
import uuid

from db.client2 import get_db
from db.models import Group as GroupModel, Office as OfficeModel, Nurse as NurseModel
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


# 그룹 명 수정
@router.patch("/{group_id}")
async def update_group_name(
    group_id: str,
    payload: dict,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    병동(그룹) 이름 수정
    - 마스터 관리자만 가능
    - 요청 예시: PATCH /groups/abc123def456
                {"group_name": "간호4병동 (구 3병동)"}
    """
    if not current_user or not current_user.is_master_admin:
        raise HTTPException(
            status_code=403,
            detail="마스터 관리자만 접근 가능합니다."
        )

    if not group_id:
        raise HTTPException(400, detail="group_id가 필요합니다.")

    # 1. 해당 그룹 존재 여부 + 소속 office 확인
    group = (
        db.query(GroupModel)
        .filter(
            GroupModel.group_id == group_id,
            GroupModel.office_id == current_user.office_id
        )
        .first()
    )

    if not group:
        raise HTTPException(
            status_code=404,
            detail="해당 group_id를 찾을 수 없거나 소속이 다릅니다."
        )

    # 2. 새로운 group_name 추출
    new_name = payload.get("group_name")
    if new_name is None:
        raise HTTPException(
            status_code=400,
            detail="수정할 group_name 필드가 필요합니다."
        )

    cleaned_name = str(new_name).strip()
    if not cleaned_name:
        raise HTTPException(
            status_code=400,
            detail="group_name은 공백일 수 없습니다."
        )

    # 3. 이름 변경
    group.group_name = cleaned_name

    db.commit()
    db.refresh(group)  # 필요 시 최신 상태 반영 (필수는 아님)

    # 4. 응답
    updated_data = {
        "group_id": str(group.group_id),
        "group_name": str(group.group_name),
        "office_id": str(group.office_id),
        "message": "그룹 이름이 수정되었습니다."
    }

    return JSONResponse(
        content=jsonable_encoder(updated_data),
        media_type="application/json; charset=utf-8",
        status_code=status.HTTP_200_OK
    )


# ── 그룹 관리자(HN) 관련 엔드포인트 ──────────────────────────────


@router.get("/by-office")
async def list_groups_by_office(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """ADM 또는 HN 사용자가 자신의 office_id에 해당하는 모든 그룹 조회 (hn_id 포함)."""
    if not current_user:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    if not current_user.is_master_admin and str(current_user.hn_auth or '').upper() != 'HN':
        raise HTTPException(status_code=403, detail="관리자 또는 그룹 관리자만 접근 가능합니다.")

    rows = (
        db.query(GroupModel)
        .filter(GroupModel.office_id == current_user.office_id)
        .all()
    )

    data = [
        {
            "group_id": str(g.group_id),
            "group_name": str(g.group_name),
            "office_id": str(g.office_id),
            "hn_id": g.hn_id or [],
        }
        for g in rows
    ]

    return JSONResponse(
        content=jsonable_encoder(data),
        media_type="application/json; charset=utf-8"
    )


@router.put("/hn-admin")
async def update_hn_admin(
    payload: dict,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    그룹 관리자(HN) 지정/해제.
    - 마스터 관리자(ADM)만 호출 가능
    - payload: {"nurse_id": "...", "group_ids": ["g1", "g2"]}
    - nurse_id를 group_ids 각 그룹의 hn_id 리스트에 추가,
      포함되지 않은 같은 office 그룹에서는 제거
    """
    if not current_user or not current_user.is_master_admin:
        raise HTTPException(status_code=403, detail="마스터 관리자만 접근 가능합니다.")

    target_nurse_id = (payload or {}).get("nurse_id")
    target_group_ids = set((payload or {}).get("group_ids", []))

    if not target_nurse_id:
        raise HTTPException(status_code=400, detail="nurse_id는 필수입니다.")

    # 같은 office의 모든 그룹 조회
    all_groups = (
        db.query(GroupModel)
        .filter(GroupModel.office_id == current_user.office_id)
        .all()
    )

    for group in all_groups:
        current_hn_ids = list(group.hn_id or [])

        if group.group_id in target_group_ids:
            if target_nurse_id not in current_hn_ids:
                current_hn_ids.append(target_nurse_id)
        else:
            if target_nurse_id in current_hn_ids:
                current_hn_ids.remove(target_nurse_id)

        group.hn_id = current_hn_ids
        flag_modified(group, "hn_id")

    # 간호사의 hn_auth + is_head_nurse 업데이트
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == target_nurse_id).first()
    if nurse:
        if len(target_group_ids) > 0:
            nurse.hn_auth = 'HN'
            nurse.is_head_nurse = True
        else:
            # 그룹 관리자 해제 시 hn_auth를 null로 복원 + 모든 그룹 hn_id에서 제거
            nurse.hn_auth = None
            nurse.is_head_nurse = False

    db.commit()

    return {
        "message": "그룹 관리자가 성공적으로 업데이트되었습니다.",
        "nurse_id": target_nurse_id,
        "group_ids": list(target_group_ids),
    }


@router.get("/my-admin-groups")
async def get_my_admin_groups(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db)
):
    """
    현재 로그인한 사용자가 관리자로 지정된 그룹 목록 조회.
    hn_id JSON 배열에 자신의 nurse_id가 포함된 그룹 + 자신의 원래 그룹을 반환.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    # DB에서 간호사의 실제 소속 group_id 조회 (JWT group_id는 switch로 변경될 수 있으므로)
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == current_user.nurse_id).first()
    home_group_id = nurse.group_id if nurse else current_user.group_id

    all_groups = (
        db.query(GroupModel)
        .filter(GroupModel.office_id == current_user.office_id)
        .all()
    )

    seen_ids = set()
    my_groups = []

    # 자신의 원래 소속 그룹 우선 포함 (DB 기준)
    for g in all_groups:
        if g.group_id == home_group_id:
            my_groups.append({
                "group_id": str(g.group_id),
                "group_name": str(g.group_name),
                "office_id": str(g.office_id),
            })
            seen_ids.add(g.group_id)
            break

    # hn_id에 자신이 포함된 그룹 추가
    for g in all_groups:
        if g.group_id in seen_ids:
            continue
        hn_ids = g.hn_id or []
        if current_user.nurse_id in hn_ids:
            my_groups.append({
                "group_id": str(g.group_id),
                "group_name": str(g.group_name),
                "office_id": str(g.office_id),
            })

    return JSONResponse(
        content=jsonable_encoder(my_groups),
        media_type="application/json; charset=utf-8"
    )