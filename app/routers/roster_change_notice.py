"""재마감 변경 확인 API(성남 ②) — 변경 알림 조회·내 미확인·상세·[확인].

알림은 재마감(`POST /roster/publish`) 안에서 만들어진다(`services.roster_change_notice_service`).
화면은 마감본 응답을 바꾸지 않고 이 API 를 따로 불러 바뀐 칸 표시를 얹는다.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from db.client2 import get_db
from routers.auth import require_current_user
from schemas.auth_schema import User as UserSchema
from services.roster_change_notice_service import (
    ack_notice, month_notices, notice_detail, pending_for_me,
)

router = APIRouter(prefix="/roster/change-notices", tags=["roster-change-notice"])


@router.get("")
def list_month_change_notices(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """그 달 **확인 중인** 변경 알림과 바뀐 칸(근무표 화면 칸 표시용).

    같은 병동(그 달 파견 간 병동 포함)이면 누구나. 전원이 확인했거나 마감 철회 중이면 `notices` 가 비고,
    그러면 칸 표시가 풀린다. 응답: `{group_id, year, month, is_manager, notices:[{…요약, cells:[…]}]}`.
    """
    return month_notices(db, current_user, year, month, group_id)


@router.get("/pending/me")
def list_my_pending_change_notices(
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """내가 아직 [확인]하지 않은 변경 알림(앱 홈 배너용). 응답: `{notices:[{…요약, group_name}]}`."""
    return pending_for_me(db, current_user)


@router.get("/{notice_id}")
def get_change_notice(
    notice_id: int = Path(..., ge=1),
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """변경 알림 1건 — 바뀐 칸 목록 + 내 확인 여부. 관리자에게는 대상자별 확인 시각(`targets`)도.

    일반 간호사는 동료별 확인 여부 대신 "N명 중 M명"(`confirm`) 숫자만 본다(10-02 결정).
    """
    return notice_detail(db, current_user, notice_id)


@router.post("/{notice_id}/ack")
def ack_change_notice(
    notice_id: int = Path(..., ge=1),
    via: Literal["app", "web"] = Query("app", description="확인한 화면"),
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    """[확인했습니다] — 내 확인 기록. 다시 눌러도 같은 결과. 마지막 한 명이면 알림이 닫힌다.

    확인 전에 근무표가 다시 마감돼 대체된 알림이면 409(`detail.superseded_by` = 새 알림 번호).
    """
    return ack_notice(db, current_user, notice_id, via)
