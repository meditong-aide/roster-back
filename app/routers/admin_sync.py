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


@router.post("/prod-to-dev")
async def trigger_prod_to_dev_sync(
    req: ProdToDevSyncRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """수동 prod → dev 동기화. master admin 전용."""
    if not getattr(current_user, "is_master_admin", False):
        raise HTTPException(status_code=403, detail="master admin only")
    return sync_prod_to_dev(db, tables=req.tables)
