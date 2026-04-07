"""
간호사 배정/상태 변경 관리 서비스
- nurse_assignment 테이블 CRUD
- flush_pending_transfers: 병동이동 레이지 체크
"""

from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from fastapi import HTTPException
from datetime import date, datetime, timedelta
from typing import Optional
from db.models import NurseAssignment, Nurse as NurseModel, ShiftTransferLog, Group
from schemas.roster_schema import (
    NurseAssignmentCreate,
    NurseAssignmentUpdate,
    NurseAssignmentResponse,
)
import logging

logger = logging.getLogger(__name__)


def _get_group_name(db: Session, group_id: str | None) -> str | None:
    if not group_id:
        return None
    g = db.query(Group.group_name).filter(Group.group_id == group_id).first()
    return g.group_name if g else None


def _get_head_nurse_ids(db: Session, group_id: str | None) -> list[str]:
    """그룹의 수간호사(is_head_nurse=True) nurse_id 목록 반환."""
    if not group_id:
        return []
    rows = (
        db.query(NurseModel.nurse_id)
        .filter(
            NurseModel.group_id == group_id,
            NurseModel.is_head_nurse == True,
            NurseModel.active == 1,
        )
        .all()
    )
    return [str(r.nurse_id) for r in rows]


def _collect_assignment_recipients(
    db: Session, nurse_id: str, source_group_id: str, target_group_id: str | None
) -> list[str]:
    """대상 간호사 + source/target 관리자 수신자 목록 (중복 제거)."""
    recipients = {str(nurse_id)}
    for gid in (source_group_id, target_group_id):
        for nid in _get_head_nurse_ids(db, gid):
            recipients.add(nid)
    return list(recipients)


def create_assignment(
    req: NurseAssignmentCreate,
    db: Session,
) -> NurseAssignmentResponse:
    """배정/상태 변경 등록"""
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == req.nurse_id).first()
    if not nurse:
        raise HTTPException(status_code=404, detail="간호사를 찾을 수 없습니다.")

    # 퇴사자 검증: resignation_date가 존재하고 start_date 이전이면 거부
    _resign = getattr(nurse, "resignation_date", None)
    if _resign:
        _resign_d = _resign.date() if hasattr(_resign, "date") else _resign
        if _resign_d <= req.start_date:
            raise HTTPException(
                status_code=400,
                detail=f"퇴사 처리된 간호사입니다. (퇴사일: {_resign_d})",
            )

    # 기간 중복 검증: 동일 간호사의 active assignment와 일자 겹침 불허
    from sqlalchemy import case
    _eff_end = case(
        (NurseAssignment.end_date.isnot(None), NurseAssignment.end_date),
        else_=NurseAssignment.expected_end_date,
    )
    _overlap = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id == req.nurse_id,
            NurseAssignment.status == "active",
            NurseAssignment.start_date <= req.expected_end_date,
            or_(
                _eff_end.is_(None),
                _eff_end >= req.start_date,
            ),
        )
        .first()
    )
    if _overlap:
        raise HTTPException(
            status_code=409,
            detail=f"기간이 겹치는 배정이 존재합니다. (id={_overlap.id}, reason={_overlap.reason}, "
                   f"{_overlap.start_date}~{_overlap.end_date or _overlap.expected_end_date})",
        )

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

    # 알림 발송 (S06)
    try:
        from utils.utils import send_assignment_created_push
        _recipients = _collect_assignment_recipients(
            db, req.nurse_id, req.source_group_id, req.target_group_id
        )
        send_assignment_created_push(
            nurse_name=nurse.name,
            reason=req.reason,
            start_date=str(req.start_date),
            end_date=str(req.expected_end_date),
            source_group_name=_get_group_name(db, req.source_group_id) or req.source_group_id,
            target_group_name=_get_group_name(db, req.target_group_id),
            recipients=_recipients,
            office_code=req.office_id,
            sender_emp_seq_no=req.nurse_id,
            sender_member_id=req.nurse_id,
        )
    except Exception as e:
        logger.error("배정 생성 알림 발송 실패: %s", e, exc_info=True)

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

    # 알림 발송 (S07)
    try:
        from utils.utils import send_assignment_cancelled_push
        _recipients = _collect_assignment_recipients(
            db, row.nurse_id, row.source_group_id, row.target_group_id
        )
        send_assignment_cancelled_push(
            nurse_name=nurse.name if nurse else str(row.nurse_id),
            reason=row.reason,
            source_group_name=_get_group_name(db, row.source_group_id) or row.source_group_id,
            target_group_name=_get_group_name(db, row.target_group_id),
            recipients=_recipients,
            office_code=row.office_id,
            sender_emp_seq_no=row.nurse_id,
            sender_member_id=row.nurse_id,
        )
    except Exception as e:
        logger.error("배정 취소 알림 발송 실패: %s", e, exc_info=True)

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
    """특정 월 기준 배정 레코드 조회 (active + completed 포함, flush 후에도 유지)"""
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
            NurseAssignment.status.in_(["active", "completed"]),
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
            nurse.team_id = None
            logger.info(
                "병동이동 적용: nurse_id=%s, %s → %s (team 초기화)",
                row.nurse_id, row.source_group_id, row.target_group_id,
            )
        row.status = "completed"
        row.end_date = today
        count += 1

        # 알림 발송 (S08)
        try:
            from utils.utils import send_transfer_completed_push
            _tgt_name = _get_group_name(db, row.target_group_id) or str(row.target_group_id)
            _recipients = {str(row.nurse_id)}
            for nid in _get_head_nurse_ids(db, row.target_group_id):
                _recipients.add(nid)
            send_transfer_completed_push(
                nurse_name=nurse.name if nurse else str(row.nurse_id),
                target_group_name=_tgt_name,
                recipients=list(_recipients),
                office_code=row.office_id,
                sender_emp_seq_no=row.nurse_id,
                sender_member_id=row.nurse_id,
            )
        except Exception as e:
            logger.error("병동이동 완료 알림 실패: %s", e, exc_info=True)

    if count > 0:
        db.commit()
        logger.info("병동이동 레이지 체크 완료: %d건 처리", count)

    return count


def flush_all_pending_transfers(db: Session) -> int:
    """전체 병동이동 레이지 체크 (스케줄러용, group_id 필터 없음).

    start_date <= 오늘인 모든 active 병동이동 레코드를 처리한다.
    """
    today = date.today()
    rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.reason == "병동이동",
            NurseAssignment.status == "active",
            NurseAssignment.start_date <= today,
            NurseAssignment.target_group_id.isnot(None),
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
            nurse.team_id = None
            logger.info(
                "[Scheduler] 병동이동 적용: nurse_id=%s, %s → %s",
                row.nurse_id, row.source_group_id, row.target_group_id,
            )
        row.status = "completed"
        row.end_date = today
        count += 1

        # 알림 발송 (S08)
        try:
            from utils.utils import send_transfer_completed_push
            _tgt_name = _get_group_name(db, row.target_group_id) or str(row.target_group_id)
            _recipients = {str(row.nurse_id)}
            for nid in _get_head_nurse_ids(db, row.target_group_id):
                _recipients.add(nid)
            send_transfer_completed_push(
                nurse_name=nurse.name if nurse else str(row.nurse_id),
                target_group_name=_tgt_name,
                recipients=list(_recipients),
                office_code=row.office_id,
                sender_emp_seq_no=row.nurse_id,
                sender_member_id=row.nurse_id,
            )
        except Exception as e:
            logger.error("[Scheduler] 병동이동 완료 알림 실패: %s", e, exc_info=True)

    if count > 0:
        db.commit()
        logger.info("[Scheduler] 병동이동 자동 flush 완료: %d건", count)

    return count


def transfer_shifts_on_publish(
    db: Session,
    schedule_id: str,
    source_group_id: str,
    office_id: str,
    year: int,
    month: int,
) -> int:
    """마감(발행) 시 파견/병동이동 전달 이력을 기록한다.

    - start/end 월에만 해당 (중간 월은 target이 독립 생성)
    - target_schedule_id는 null (target에서 근무표 생성 시 별도 기록)
    - 실제 entry 복사는 target 근무표 생성 시 수행
    Returns: 기록된 log 수
    """
    from calendar import monthrange

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
                and_(
                    NurseAssignment.end_date.is_(None),
                    or_(
                        NurseAssignment.expected_end_date.is_(None),
                        NurseAssignment.expected_end_date >= month_start,
                    ),
                ),
                NurseAssignment.end_date >= month_start,
            ),
        )
        .all()
    )
    if not assignments:
        return 0

    # source 생성 월만 전달 대상 (full month / 중간 월은 target 독립)
    from calendar import monthrange as _mr
    from services.day_windows import is_source_generated_month
    _dim = _mr(year, month)[1]
    eligible = [
        a for a in assignments
        if is_source_generated_month(
            a.start_date, a.end_date or a.expected_end_date, month_start, _dim
        )
    ]
    if not eligible:
        return 0

    # 2. source schedule의 entry 수 집계 (log용)
    from db.models import ScheduleEntry
    nurse_ids = {a.nurse_id for a in eligible}
    source_entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == schedule_id,
            ScheduleEntry.nurse_id.in_(nurse_ids),
        )
        .all()
    )
    entry_count_by_nurse: dict[str, int] = {}
    for e in source_entries:
        _wd = e.work_date.date() if hasattr(e.work_date, 'date') else e.work_date
        entry_count_by_nurse[e.nurse_id] = entry_count_by_nurse.get(e.nurse_id, 0) + 1

    # 3. 기존 미소비 pending log 정리 (재마감 대응 — source group 전체 범위)
    db.query(ShiftTransferLog).filter(
        ShiftTransferLog.source_group_id == source_group_id,
        ShiftTransferLog.year == year,
        ShiftTransferLog.month == month,
        ShiftTransferLog.target_schedule_id.is_(None),
    ).delete(synchronize_session=False)

    # 4. 전달 이력 기록 (source 마감 → target_schedule_id는 null)
    count = 0
    for a in eligible:
        a_end = a.end_date or a.expected_end_date or month_end
        transfer_start = max(a.start_date, month_start)
        transfer_end = min(a_end, month_end)

        db.add(ShiftTransferLog(
            schedule_id=schedule_id,
            target_schedule_id=None,
            assignment_id=a.id,
            nurse_id=a.nurse_id,
            source_group_id=source_group_id,
            target_group_id=a.target_group_id,
            transfer_start=transfer_start,
            transfer_end=transfer_end,
            entry_count=entry_count_by_nurse.get(a.nurse_id, 0),
            year=year,
            month=month,
        ))
        count += 1

        logger.info(
            "전달 이력 기록: nurse_id=%s, %s→%s, %s~%s",
            a.nurse_id, source_group_id, a.target_group_id,
            transfer_start, transfer_end,
        )

    if count > 0:
        db.flush()
        logger.info("전달 이력 기록 완료: %d건", count)

    return count


def copy_transferred_entries(
    db: Session,
    target_schedule_id: str,
    target_group_id: str,
    year: int,
    month: int,
) -> int:
    """Target 근무표 생성 시 source에서 shift를 복사하고 log에 히스토리 추가.

    - target_schedule_id가 null인 log에서 source schedule 조회
    - source entry → default_shift → target shift 변환 후 복사
    - 새 log row insert (히스토리 누적)
    Returns: 복사된 entry 수
    """
    import uuid
    from calendar import monthrange
    from db.models import Schedule, ScheduleEntry, Shift

    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])

    # 1. 미전달 log 조회 (해당 target group, 해당 월, target_schedule_id가 null)
    pending_logs = (
        db.query(ShiftTransferLog)
        .filter(
            ShiftTransferLog.target_group_id == target_group_id,
            ShiftTransferLog.year == year,
            ShiftTransferLog.month == month,
            ShiftTransferLog.target_schedule_id.is_(None),
        )
        .all()
    )
    if not pending_logs:
        return 0

    # 2. source → default_shift 매핑 (source group별)
    source_group_ids = {log.source_group_id for log in pending_logs}
    source_default_maps: dict[str, dict[str, str]] = {}
    source_shift_id_to_int: dict[str, dict[str, int]] = {}
    for sgid in source_group_ids:
        shifts = db.query(Shift).filter(Shift.group_id == sgid).all()
        source_default_maps[sgid] = {
            s.shift_id: s.default_shift for s in shifts if s.default_shift
        }
        source_shift_id_to_int[sgid] = {s.shift_id: s.id for s in shifts}

    # 3. target default_shift → target shift_id 매핑
    target_shifts = db.query(Shift).filter(Shift.group_id == target_group_id).all()
    target_dmap: dict[str, str] = {}
    for s in target_shifts:
        if s.default_shift and s.default_shift not in target_dmap:
            target_dmap[s.default_shift] = s.shift_id
    target_shift_id_to_int = {s.shift_id: s.id for s in target_shifts}

    # 4. 각 log에 대해 entry 복사 + 새 log row 생성
    total = 0
    for log in pending_logs:
        src_map = source_default_maps.get(log.source_group_id, {})

        # source schedule에서 해당 간호사 entry 조회
        source_entries = (
            db.query(ScheduleEntry)
            .filter(
                ScheduleEntry.schedule_id == log.schedule_id,
                ScheduleEntry.nurse_id == log.nurse_id,
                ScheduleEntry.work_date >= log.transfer_start,
                ScheduleEntry.work_date <= log.transfer_end,
            )
            .all()
        )

        # 기존 target entry 삭제 (재생성 대응)
        db.query(ScheduleEntry).filter(
            ScheduleEntry.schedule_id == target_schedule_id,
            ScheduleEntry.nurse_id == log.nurse_id,
            ScheduleEntry.work_date >= log.transfer_start,
            ScheduleEntry.work_date <= log.transfer_end,
        ).delete(synchronize_session=False)

        # shift 변환 + entry 생성
        src_int_map = source_shift_id_to_int.get(log.source_group_id, {})
        a_count = 0
        for e in source_entries:
            src_shift = e.shift_id
            if not src_shift or src_shift == '-':
                continue
            default_code = src_map.get(src_shift, src_shift.upper())
            target_shift = target_dmap.get(default_code, default_code)
            # 매핑 성공 → target id, 매핑 불가 → source id fallback
            shift_int_id = target_shift_id_to_int.get(target_shift)
            if shift_int_id is None:
                shift_int_id = src_int_map.get(src_shift)
            db.add(ScheduleEntry(
                entry_id=str(uuid.uuid4().hex)[:16],
                schedule_id=target_schedule_id,
                nurse_id=log.nurse_id,
                work_date=e.work_date,
                shift_id=target_shift,
                id=shift_int_id,
            ))
            a_count += 1

        total += a_count

        # 히스토리 log 추가 (새 row)
        db.add(ShiftTransferLog(
            schedule_id=log.schedule_id,
            target_schedule_id=target_schedule_id,
            assignment_id=log.assignment_id,
            nurse_id=log.nurse_id,
            source_group_id=log.source_group_id,
            target_group_id=target_group_id,
            transfer_start=log.transfer_start,
            transfer_end=log.transfer_end,
            entry_count=a_count,
            year=year,
            month=month,
        ))

        logger.info(
            "shift 복사: nurse_id=%s, %s→%s (target_schedule=%s), %d건",
            log.nurse_id, log.source_group_id, target_group_id,
            target_schedule_id, a_count,
        )

    if total > 0:
        db.flush()
        logger.info("shift 복사 완료: 총 %d건", total)

    return total


def get_transfer_logs(
    db: Session,
    year: int,
    month: int,
    group_id: Optional[str] = None,
    nurse_id: Optional[str] = None,
) -> list[dict]:
    """전달 이력 조회 (운영자: group_id 기준 / 당사자: nurse_id 기준)"""
    query = db.query(ShiftTransferLog).filter(
        ShiftTransferLog.year == year,
        ShiftTransferLog.month == month,
    )
    if group_id:
        query = query.filter(
            or_(
                ShiftTransferLog.source_group_id == group_id,
                ShiftTransferLog.target_group_id == group_id,
            )
        )
    if nurse_id:
        query = query.filter(ShiftTransferLog.nurse_id == nurse_id)

    rows = query.order_by(ShiftTransferLog.transferred_at.desc()).all()

    # 간호사 이름 매핑
    nurse_ids = {r.nurse_id for r in rows}
    nurse_map = {}
    if nurse_ids:
        nurses = db.query(NurseModel).filter(NurseModel.nurse_id.in_(nurse_ids)).all()
        nurse_map = {n.nurse_id: n.name for n in nurses}

    return [
        {
            "id": r.id,
            "schedule_id": r.schedule_id,
            "target_schedule_id": r.target_schedule_id,
            "assignment_id": r.assignment_id,
            "nurse_id": r.nurse_id,
            "nurse_name": nurse_map.get(r.nurse_id),
            "source_group_id": r.source_group_id,
            "target_group_id": r.target_group_id,
            "transfer_start": r.transfer_start,
            "transfer_end": r.transfer_end,
            "entry_count": r.entry_count,
            "year": r.year,
            "month": r.month,
            "transferred_at": r.transferred_at,
        }
        for r in rows
    ]


def get_transferred_wanted(
    db: Session,
    nurse_id: str,
    year: int,
    month: int,
) -> dict:
    """파견/병동이동 간호사의 원티드를 target group shift로 변환하여 반환.

    매핑 가능: source shift → default_shift → target shift
    매핑 불가: 원본 shift + color 그대로 반환 (fallback)
    """
    from calendar import monthrange
    from db.models import NurseShiftRequest, Shift, Nurse as NurseModel

    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
    if not nurse:
        return {"entries": [], "assignment": None}

    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])

    # 파견/병동이동 assignment 조회 (flush 후 group_id 변경 대응: nurse_id 직접 조회)
    from sqlalchemy import case as _case
    _eff_end = _case(
        (NurseAssignment.end_date.isnot(None), NurseAssignment.end_date),
        else_=NurseAssignment.expected_end_date,
    )
    my_assign = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id == nurse_id,
            NurseAssignment.status.in_(["active", "completed"]),
            NurseAssignment.reason.in_(["파견", "병동이동"]),
            NurseAssignment.target_group_id.isnot(None),
            NurseAssignment.start_date <= month_end,
            or_(
                _eff_end.is_(None),
                _eff_end >= month_start,
            ),
        )
        .all()
    )
    if not my_assign:
        return {"entries": [], "assignment": None}

    a = my_assign[0]
    a_end = a.end_date or a.expected_end_date or month_end
    period_start = max(a.start_date, month_start)
    period_end = min(a_end, month_end)

    # 최신 request_id 조회
    from sqlalchemy import func as sa_func
    from db.models import WantedRequest
    month_str = f"{year}-{month:02d}"
    latest_req = (
        db.query(sa_func.max(WantedRequest.request_id))
        .filter(WantedRequest.nurse_id == nurse_id, WantedRequest.month == month_str)
        .scalar()
    )
    if not latest_req:
        return {"entries": [], "assignment": _assignment_summary(a)}

    # 해당 기간 원티드 조회
    entries = (
        db.query(NurseShiftRequest)
        .filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == latest_req,
            NurseShiftRequest.shift_date >= period_start,
            NurseShiftRequest.shift_date <= period_end,
        )
        .all()
    )
    if not entries:
        return {"entries": [], "assignment": _assignment_summary(a)}

    # source shift 매핑: shift_id → (default_shift, name, color)
    source_shifts = db.query(Shift).filter(Shift.group_id == nurse.group_id).all()
    source_map = {
        s.shift_id: {"default": s.default_shift, "name": s.name, "color": s.color}
        for s in source_shifts
    }

    # target shift 매핑: default_shift → (shift_id, name, color)
    target_shifts = db.query(Shift).filter(Shift.group_id == a.target_group_id).all()
    target_by_default: dict[str, dict] = {}
    for s in target_shifts:
        if s.default_shift and s.default_shift not in target_by_default:
            target_by_default[s.default_shift] = {
                "shift_id": s.shift_id, "name": s.name, "color": s.color,
            }

    # 변환
    result = []
    for e in entries:
        src = source_map.get(e.shift, {})
        default_code = src.get("default")
        target = target_by_default.get(default_code) if default_code else None

        if target:
            result.append({
                "shift_date": e.shift_date,
                "shift": target["shift_id"],
                "shift_name": target["name"],
                "color": target["color"],
                "score": float(e.score) if e.score is not None else 0.0,
                "comment": e.comment,
                "mapped": True,
            })
        else:
            # 매핑 불가 → 원본 shift + color fallback
            result.append({
                "shift_date": e.shift_date,
                "shift": e.shift,
                "shift_name": src.get("name", e.shift),
                "color": src.get("color"),
                "score": float(e.score) if e.score is not None else 0.0,
                "comment": e.comment,
                "mapped": False,
            })

    return {"entries": result, "assignment": _assignment_summary(a)}


def get_roster_assignments(
    db: Session,
    group_id: str,
    year: int,
    month: int,
) -> dict[str, dict]:
    """근무표 응답용 파견/병동이동 assignment 메타데이터.

    해당 group이 source인 assignment만 반환 (source group 근무표에서 미표기 처리용).
    Returns: {nurse_id: {reason, target_group_id, target_group_name, start_date, end_date}}
    """
    from db.models import Group

    assignments = get_active_assignments_for_month(db, group_id, year, month)
    # source group 기준: 파견/병동이동/휴직 간호사
    eligible = [
        a for a in assignments
        if a.reason in ("파견", "병동이동", "휴직")
        and a.source_group_id == group_id
    ]
    if not eligible:
        return {}

    # target group name 조회 (파견/병동이동만)
    target_gids = {a.target_group_id for a in eligible if a.target_group_id}
    gname_map: dict[str, str] = {}
    if target_gids:
        groups = db.query(Group).filter(Group.group_id.in_(target_gids)).all()
        gname_map = {g.group_id: g.group_name for g in groups}

    result: dict[str, dict] = {}
    for a in eligible:
        result[a.nurse_id] = {
            "reason": a.reason,
            "target_group_id": a.target_group_id or "",
            "target_group_name": gname_map.get(a.target_group_id, "") if a.target_group_id else "",
            "start_date": str(a.start_date),
            "end_date": str(a.end_date or a.expected_end_date) if (a.end_date or a.expected_end_date) else None,
        }
    return result


def _assignment_summary(a: NurseAssignment) -> dict:
    """assignment 요약 정보"""
    return {
        "id": a.id,
        "reason": a.reason,
        "source_group_id": a.source_group_id,
        "target_group_id": a.target_group_id,
        "start_date": a.start_date,
        "end_date": a.end_date or a.expected_end_date,
    }


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
