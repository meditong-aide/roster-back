"""운영 DB(eun_roster) → 개발 DB(eun_roster_dev) 동기화 수동 트리거.

매일 07:00 KST 자동 스케줄과 별개로 즉시 실행용.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.prod_to_dev_sync_service import sync_prod_to_dev

router = APIRouter(prefix="/admin/sync", tags=["admin-sync"])


class ProdToDevSyncRequest(BaseModel):
    tables: Optional[List[str]] = None
    include_group_ids: Optional[List[str]] = None
    exclude_group_ids: Optional[List[str]] = None
    include_office_ids: Optional[List[str]] = None


@router.post("/prod-to-dev")
async def trigger_prod_to_dev_sync(
    req: ProdToDevSyncRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """수동 prod → dev 동기화. master admin 전용.

    - include_group_ids: group_id 컬럼이 있는 테이블에서 해당 group_id 행만
      prod 에서 가져오고 dev 의 해당 group_id 행만 wipe.
      group_id 컬럼이 없는 테이블은 건드리지 않음 (dev 보존).
      다른 group 의 dev 데이터는 그대로 유지.
    - exclude_group_ids: 해당 group_id 행을 prod 에서 가져오지 않고
      dev wipe 대상에서도 제외 (dev 기존 행 보존).
      group_id 컬럼이 없는 테이블은 영향 없음 (full wipe+copy).
    """
    if not getattr(current_user, "is_master_admin", False):
        raise HTTPException(status_code=403, detail="master admin only")
    return sync_prod_to_dev(
        db,
        tables=req.tables,
        include_group_ids=req.include_group_ids,
        exclude_group_ids=req.exclude_group_ids,
        include_office_ids=req.include_office_ids,
    )
