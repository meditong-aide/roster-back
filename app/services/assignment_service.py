"""
간호사 배정/상태 변경 관리 서비스
- nurse_assignment 테이블 CRUD
- flush_pending_transfers: 병동이동 레이지 체크
"""

from sqlalchemy.orm import Session
from fastapi import HTTPException
from datetime import date
from typing import Optional
from db.models import NurseAssignment, Nurse as NurseModel
from schemas.roster_schema import (
    NurseAssignmentCreate,
    NurseAssignmentUpdate,
    NurseAssignmentResponse,
)
import logging

logger = logging.getLogger(__name__)


def create_assignment(
    req: NurseAssignmentCreate,
    db: Session,
) -> NurseAssignmentResponse:
    """배정/상태 변경 등록"""
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == req.nurse_id).first()
    if not nurse:
        raise HTTPException(status_code=404, detail="간호사를 찾을 수 없습니다.")

    row = NurseAssignment(
        nurse_id=req.nurse_id,
        source_group_id=req.source_group_id,
        target_group_id=req.target_group_id,
        office_id=req.office_id,
        start_date=req.start_date,
        expected_end_date=req.expected_end_date,
        reason=req.reason,
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "배정 등록: nurse_id=%s, reason=%s, start=%s",
        req.nurse_id, req.reason, req.start_date,
    )
    return _to_response(row, nurse.name)


def update_assignment(
    assignment_id: int,
    req: NurseAssignmentUpdate,
    db: Session,
) -> NurseAssignmentResponse:
    """배정/상태 변경 수정"""
    row = db.query(NurseAssignment).filter(NurseAssignment.id == assignment_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="배정 이력을 찾을 수 없습니다.")

    if req.expected_end_date is not None:
        row.expected_end_date = req.expected_end_date
    if req.end_date is not None:
        row.end_date = req.end_date
    if req.status is not None:
        row.status = req.status

    db.commit()
    db.refresh(row)
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == row.nurse_id).first()
    return _to_response(row, nurse.name if nurse else None)


def cancel_assignment(
    assignment_id: int,
    db: Session,
) -> NurseAssignmentResponse:
    """배정 취소 (status → cancelled)"""
    row = db.query(NurseAssignment).filter(NurseAssignment.id == assignment_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="배정 이력을 찾을 수 없습니다.")

    row.status = "cancelled"
    db.commit()
    db.refresh(row)
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == row.nurse_id).first()
    return _to_response(row, nurse.name if nurse else None)


def get_assignments(
    db: Session,
    office_id: str,
    group_id: Optional[str] = None,
    nurse_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[NurseAssignmentResponse]:
    """배정 이력 조회 (필터 조건별)"""
    query = db.query(NurseAssignment).filter(NurseAssignment.office_id == office_id)

    if group_id:
        query = query.filter(
            (NurseAssignment.source_group_id == group_id)
            | (NurseAssignment.target_group_id == group_id)
        )
    if nurse_id:
        query = query.filter(NurseAssignment.nurse_id == nurse_id)
    if status:
        query = query.filter(NurseAssignment.status == status)

    rows = query.order_by(NurseAssignment.start_date.desc()).all()

    nurse_ids = {r.nurse_id for r in rows}
    nurse_map = {}
    if nurse_ids:
        nurses = db.query(NurseModel).filter(NurseModel.nurse_id.in_(nurse_ids)).all()
        nurse_map = {n.nurse_id: n.name for n in nurses}

    return [_to_response(r, nurse_map.get(r.nurse_id)) for r in rows]


def get_active_assignments_for_month(
    db: Session,
    group_id: str,
    year: int,
    month: int,
) -> list[NurseAssignment]:
    """특정 월 기준 active 배정 레코드 조회 (솔버 입력용)"""
    from calendar import monthrange
    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])

    return (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.status == "active",
            NurseAssignment.start_date <= month_end,
            (NurseAssignment.end_date.is_(None))
            | (NurseAssignment.end_date >= month_start),
            (NurseAssignment.source_group_id == group_id)
            | (NurseAssignment.target_group_id == group_id),
        )
        .all()
    )


def flush_pending_transfers(db: Session, group_id: str) -> int:
    """병동이동 레이지 체크: start_date <= 오늘인 active 병동이동 레코드 처리
    - nurses.group_id를 target_group_id로 업데이트
    - status를 completed로 변경
    Returns: 처리된 건수
    """
    today = date.today()
    rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.reason == "병동이동",
            NurseAssignment.status == "active",
            NurseAssignment.start_date <= today,
            NurseAssignment.target_group_id.isnot(None),
            (NurseAssignment.source_group_id == group_id)
            | (NurseAssignment.target_group_id == group_id),
        )
        .all()
    )

    if not rows:
        return 0

    count = 0
    for row in rows:
        nurse = (
            db.query(NurseModel)
            .filter(NurseModel.nurse_id == row.nurse_id)
            .first()
        )
        if nurse and nurse.group_id != row.target_group_id:
            nurse.group_id = row.target_group_id
            logger.info(
                "병동이동 적용: nurse_id=%s, %s → %s",
                row.nurse_id, row.source_group_id, row.target_group_id,
            )
        row.status = "completed"
        row.end_date = today
        count += 1

    if count > 0:
        db.commit()
        logger.info("병동이동 레이지 체크 완료: %d건 처리", count)

    return count


def _to_response(
    row: NurseAssignment,
    nurse_name: Optional[str] = None,
) -> NurseAssignmentResponse:
    """ORM → Response 변환"""
    return NurseAssignmentResponse(
        id=row.id,
        nurse_id=row.nurse_id,
        nurse_name=nurse_name,
        source_group_id=row.source_group_id,
        target_group_id=row.target_group_id,
        office_id=row.office_id,
        start_date=row.start_date,
        expected_end_date=row.expected_end_date,
        end_date=row.end_date,
        reason=row.reason,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
