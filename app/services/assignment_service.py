"""
간호사 배정/상태 변경 관리 서비스
- nurse_assignment 테이블 CRUD
- flush_pending_transfers: 병동이동 레이지 체크
"""

from sqlalchemy.orm import Session
from sqlalchemy import or_
from fastapi import HTTPException
from datetime import date, timedelta
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
            or_(
                NurseAssignment.source_group_id == group_id,
                NurseAssignment.target_group_id == group_id,
            )
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

    # end_date 또는 expected_end_date 중 확정된 값이 해당 월 이전이면 제외
    from sqlalchemy import case, func as sa_func
    _effective_end = case(
        (NurseAssignment.end_date.isnot(None), NurseAssignment.end_date),
        else_=NurseAssignment.expected_end_date,
    )
    return (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.status == "active",
            NurseAssignment.start_date <= month_end,
            or_(
                _effective_end.is_(None),
                _effective_end >= month_start,
            ),
            or_(
                NurseAssignment.source_group_id == group_id,
                NurseAssignment.target_group_id == group_id,
            ),
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
            or_(
                NurseAssignment.source_group_id == group_id,
                NurseAssignment.target_group_id == group_id,
            ),
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


def transfer_shifts_on_publish(
    db: Session,
    schedule_id: str,
    source_group_id: str,
    office_id: str,
    year: int,
    month: int,
) -> int:
    """마감(발행) 시 파견/병동이동 간호사의 shift를 target group에 전달.

    - start/end 월에만 해당 (중간 월은 target이 독립 생성)
    - source shift_id → default_shift → target shift_id 변환
    - target group의 해당 월 schedule에 ScheduleEntry 생성
    Returns: 전달된 entry 수
    """
    import uuid
    from calendar import monthrange
    from db.models import Schedule, ScheduleEntry, Shift

    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])

    # 1. 해당 월에 걸리는 파견/병동이동 assignment 조회
    assignments = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.status == "active",
            NurseAssignment.reason.in_(["파견", "병동이동"]),
            NurseAssignment.source_group_id == source_group_id,
            NurseAssignment.target_group_id.isnot(None),
            NurseAssignment.start_date <= month_end,
            or_(
                NurseAssignment.end_date.is_(None),
                NurseAssignment.end_date >= month_start,
            ),
        )
        .all()
    )
    if not assignments:
        return 0

    # start/end 월만 필터 (중간 월 제외)
    eligible = []
    for a in assignments:
        _is_start_month = (a.start_date.year == year and a.start_date.month == month)
        _a_end = a.end_date or a.expected_end_date
        _is_end_month = (
            _a_end is not None
            and _a_end.year == year
            and _a_end.month == month
        )
        if _is_start_month or _is_end_month:
            eligible.append(a)
    if not eligible:
        return 0

    # 2. source group shift → default_shift 매핑
    source_shifts = db.query(Shift).filter(Shift.group_id == source_group_id).all()
    source_to_default = {s.shift_id: s.default_shift for s in source_shifts if s.default_shift}

    # 3. target group별 default_shift → target shift_id 매핑
    target_group_ids = {a.target_group_id for a in eligible}
    target_default_maps: dict[str, dict[str, str]] = {}  # {target_gid: {default: shift_id}}
    for tgid in target_group_ids:
        target_shifts = db.query(Shift).filter(Shift.group_id == tgid).all()
        dmap: dict[str, str] = {}
        for s in target_shifts:
            if s.default_shift and s.default_shift not in dmap:
                dmap[s.default_shift] = s.shift_id
        target_default_maps[tgid] = dmap

    # 4. source schedule의 ScheduleEntry 조회 (해당 간호사들)
    nurse_ids = {a.nurse_id for a in eligible}
    source_entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == schedule_id,
            ScheduleEntry.nurse_id.in_(nurse_ids),
        )
        .all()
    )
    entries_by_nurse: dict[str, dict[date, str]] = {}
    for e in source_entries:
        # work_date가 datetime일 수 있으므로 date로 정규화
        _wd = e.work_date.date() if hasattr(e.work_date, 'date') else e.work_date
        entries_by_nurse.setdefault(e.nurse_id, {})[_wd] = e.shift_id

    # 5. target schedule 찾기/생성 + shift 변환 + entry 생성
    count = 0
    for a in eligible:
        tgid = a.target_group_id
        dmap = target_default_maps.get(tgid, {})
        nurse_entries = entries_by_nurse.get(a.nurse_id, {})

        # 전달 기간 계산 (assignment 기간과 해당 월의 교집합)
        a_end = a.end_date or a.expected_end_date or month_end
        transfer_start = max(a.start_date, month_start)
        transfer_end = min(a_end, month_end)

        # target group의 해당 월 issued/latest schedule 찾기
        target_schedule = (
            db.query(Schedule)
            .filter(
                Schedule.group_id == tgid,
                Schedule.year == year,
                Schedule.month == month,
                Schedule.dropped == False,
            )
            .order_by(Schedule.version.desc())
            .first()
        )
        if not target_schedule:
            logger.warning(
                "전달 대상 schedule 없음: target_group=%s, %d년 %d월", tgid, year, month
            )
            continue

        # target shift_id → id 매핑
        target_shifts_db = db.query(Shift).filter(Shift.group_id == tgid).all()
        target_shift_id_to_int = {s.shift_id: s.id for s in target_shifts_db}

        # 기존 해당 간호사의 target entry 삭제 (재전달 지원)
        db.query(ScheduleEntry).filter(
            ScheduleEntry.schedule_id == target_schedule.schedule_id,
            ScheduleEntry.nurse_id == a.nurse_id,
            ScheduleEntry.work_date >= transfer_start,
            ScheduleEntry.work_date <= transfer_end,
        ).delete(synchronize_session=False)

        # shift 변환 + entry 생성
        d = transfer_start
        while d <= transfer_end:
            src_shift = nurse_entries.get(d)
            if src_shift and src_shift != '-':
                default_code = source_to_default.get(src_shift, src_shift.upper())
                target_shift = dmap.get(default_code, default_code)
                entry = ScheduleEntry(
                    entry_id=str(uuid.uuid4().hex)[:16],
                    schedule_id=target_schedule.schedule_id,
                    nurse_id=a.nurse_id,
                    work_date=d,
                    shift_id=target_shift,
                    id=target_shift_id_to_int.get(target_shift),
                )
                db.add(entry)
                count += 1
            d += timedelta(days=1)

        logger.info(
            "shift 전달: nurse_id=%s, %s→%s, %s~%s, %d건",
            a.nurse_id, source_group_id, tgid,
            transfer_start, transfer_end, count,
        )

    if count > 0:
        db.flush()
        logger.info("shift 전달 완료: 총 %d건", count)

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
