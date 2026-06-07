"""
간호사 배정/상태 변경 관리 서비스
- nurse_assignment 테이블 CRUD
- flush_pending_transfers: 병동이동 레이지 체크
"""

from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func
from fastapi import HTTPException
from datetime import date, datetime, timedelta
from typing import Optional, Tuple, List
from db.models import NurseAssignment, Nurse as NurseModel, ShiftTransferLog, Group
from schemas.roster_schema import (
    NurseAssignmentCreate,
    NurseAssignmentUpdate,
    NurseAssignmentResponse,
    AssignmentStatusCounts,
)
from schemas.auth_schema import User as UserSchema
import logging

logger = logging.getLogger(__name__)

# FixedWantedEntry 재배치 시 open-ended assignment(end_date=None)의 월 범위 캡
_REALLOCATE_OPEN_WINDOW_CAP_MONTHS = 12

# 파견/병동이동 이관 사유 (nurse_service._INBOUND_REASONS와 동일 정책)
_INBOUND_REASONS: Tuple[str, ...] = ("파견", "병동이동")


def _assert_caller_owns_source(
    current_user: Optional[UserSchema],
    source_group_id: str,
    db: Optional[Session] = None,
) -> None:
    """파견/병동이동/휴직 등 assignment 조작 권한 검증.

    통과 조건 (OR):
    - current_user is None → system/admin 경로로 간주
    - is_master_admin
    - caller.group_id == source_group_id (현재 view 가 source)
    - caller.original_group_id == source_group_id (원본 소속이 source — view 전환 중)
    - source_group_id ∈ resolve_managed_group_ids(caller)
      (HN multi-group: home + group.hn_id JSON 에 본인이 등록된 모든 그룹)
    """
    if current_user is None:
        return
    if getattr(current_user, "is_master_admin", False):
        return
    caller_gid = getattr(current_user, "group_id", None)
    if caller_gid == source_group_id:
        return
    caller_original = getattr(current_user, "original_group_id", None)
    if caller_original and caller_original == source_group_id:
        return
    if db is not None:
        from services.group_access import resolve_managed_group_ids
        managed = {str(g) for g in resolve_managed_group_ids(db, current_user)}
        if str(source_group_id) in managed:
            return
    raise HTTPException(
        status_code=403,
        detail=(
            f"권한 없음: source 병동({source_group_id})의 수간호사 또는 그룹 관리자만 "
            f"배정을 생성/수정할 수 있습니다. (caller={caller_gid})"
        ),
    )


# source(nurses.*) 필드명 → target(nurse_assignment.target_*) 필드명 매핑
# Target 병동 수간호사가 inbound 간호사 프로필을 수정할 때 사용.
_SOURCE_TO_TARGET_FIELD_MAP: dict[str, str] = {
    "grade": "target_grade",
    "fixed_shift": "target_fixed_shift",
    "is_night_nurse": "target_shift_types",
    "team_id": "target_team_id",
    "weekly_off_enabled": "target_weekly_off_enabled",
    "weekly_off_type": "target_weekly_off_type",
    "weekly_off_weekday": "target_weekly_off_weekday",
    "wanted_max_requests": "target_wanted_max_requests",
}

# 동일 nurse + 동일 target_group_id 로 재파견 시 직전 row 에서 자동 승계할 필드.
# 사용자가 신규 create 요청에서 명시하지 않은(model_fields_set 누락) 키만 채운다.
_ASSIGNMENT_INHERITABLE_FIELDS: Tuple[str, ...] = (
    "target_weekly_off_type",
    "target_weekly_off_enabled",
    "target_weekly_off_weekday",
    "target_shift_types",
    "target_team_id",
    "target_grade",
    "target_fixed_shift",
    "target_wanted_max_requests",
    "note",
)


def _inherit_target_fields_from_prior(
    db: Session,
    req: NurseAssignmentCreate,
) -> None:
    """동일 nurse + 동일 target_group_id 의 직전 active 파견/병동이동 row 에서
    create 요청에 명시되지 않은 target_* / note 필드를 자동 승계.

    "명시" 기준: Pydantic v2 `model_fields_set` 에 키가 포함되었는지.
    명시적 null 전송은 그대로 유지한다.
    cancelled / completed row 는 사용자가 의도적으로 종료/취소한 이력이므로
    승계 대상에서 제외한다.
    """
    if req.reason not in _INBOUND_REASONS or not req.target_group_id:
        return

    prior = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id == req.nurse_id,
            NurseAssignment.target_group_id == req.target_group_id,
            NurseAssignment.reason.in_(_INBOUND_REASONS),
            NurseAssignment.status == "active",
        )
        .order_by(
            NurseAssignment.created_at.desc(),
            NurseAssignment.id.desc(),
        )
        .first()
    )
    if not prior:
        return

    _provided = req.model_fields_set
    for _f in _ASSIGNMENT_INHERITABLE_FIELDS:
        if _f in _provided:
            continue
        _v = getattr(prior, _f, None)
        if _v is None:
            continue
        setattr(req, _f, _v)


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


# ── FixedWantedEntry 재배치 헬퍼 ───────────────────────────────────────
# 정책 B: assignment 기간 내(in-period) 엔트리 소유자는 target_group_id,
# 기간 외(out-of-period) 소유자는 source_group_id.
# 재배치 충돌 시 Policy 1(desired_owner 기존 엔트리 보존)로 해결한다.


def _effective_end_date(row: NurseAssignment) -> Optional[date]:
    """assignment의 실제 종료 경계(end_date 우선, 없으면 expected_end_date).

    병동이동은 영구 이동이라 flush가 set한 end_date(today)는 상태 마커일 뿐
    실제 종료가 아니다. 따라서 expected_end_date 만 인정한다.
    """
    if row.reason == "병동이동":
        return row.expected_end_date
    return row.end_date or row.expected_end_date


def _collect_affected_months(
    start_date: date,
    end_date: Optional[date],
    cap_months: int = _REALLOCATE_OPEN_WINDOW_CAP_MONTHS,
) -> List[Tuple[int, int]]:
    """[start_date, end_date] 범위의 (year, month) 목록. open-ended는 cap_months로 제한."""
    if end_date is None:
        y, m = start_date.year, start_date.month + cap_months
        while m > 12:
            y += 1
            m -= 12
        end = date(y, m, 1)
    else:
        end = end_date
    cur_y, cur_m = start_date.year, start_date.month
    months: List[Tuple[int, int]] = []
    while (cur_y, cur_m) <= (end.year, end.month):
        months.append((cur_y, cur_m))
        cur_m += 1
        if cur_m > 12:
            cur_y += 1
            cur_m = 1
    return months


def _desired_owner_for_date(
    d: date,
    source_group_id: str,
    target_group_id: Optional[str],
    window: Optional[Tuple[date, Optional[date]]],
) -> str:
    """window 기간 내면 target, 바깥(또는 window=None)이면 source."""
    if not target_group_id or window is None:
        return source_group_id
    start, end = window
    if d < start:
        return source_group_id
    if end is not None and d > end:
        return source_group_id
    return target_group_id


def _reallocate_month_fixed_wanted(
    db: Session,
    nurse_id: str,
    source_group_id: str,
    target_group_id: Optional[str],
    year: int,
    month: int,
    window: Optional[Tuple[date, Optional[date]]],
) -> int:
    """해당 월 FixedWantedEntry.group_id를 desired_owner 기준으로 재배치."""
    from db.models import FixedWantedEntry

    candidate_gids = [source_group_id]
    if target_group_id:
        candidate_gids.append(target_group_id)
    entries = (
        db.query(FixedWantedEntry)
        .filter(
            FixedWantedEntry.nurse_id == nurse_id,
            FixedWantedEntry.year == year,
            FixedWantedEntry.month == month,
            FixedWantedEntry.group_id.in_(candidate_gids),
        )
        .all()
    )
    if not entries:
        return 0
    by_date: dict[date, dict[str, list]] = {}
    for e in entries:
        by_date.setdefault(e.shift_date, {}).setdefault(e.group_id, []).append(e)
    changed = 0
    for d, owners in by_date.items():
        want = _desired_owner_for_date(d, source_group_id, target_group_id, window)
        has_want = bool(owners.get(want))
        for gid, rows in owners.items():
            if gid == want:
                continue
            if has_want:
                for r in rows:
                    db.delete(r)
                    changed += 1
            else:
                for r in rows:
                    r.group_id = want
                    changed += 1
    return changed


def _reallocate_fixed_wanted_on_assignment_change(
    db: Session,
    nurse_id: str,
    source_group_id: str,
    target_group_id: Optional[str],
    old_window: Optional[Tuple[date, Optional[date]]],
    new_window: Optional[Tuple[date, Optional[date]]],
) -> int:
    """assignment 변경(create/update/cancel)에 따라 FixedWantedEntry 소유자를 재배치.

    old_window∪new_window 범위의 월들에 대해서만 재배치한다.
    파견/병동이동만 대상(target_group_id 존재 시); 그 외 사유는 no-op.
    """
    if not target_group_id:
        return 0
    months: set[Tuple[int, int]] = set()
    for win in (old_window, new_window):
        if win is None:
            continue
        for ym in _collect_affected_months(win[0], win[1]):
            months.add(ym)
    if not months:
        return 0
    total = 0
    for (year, month) in months:
        total += _reallocate_month_fixed_wanted(
            db, nurse_id, source_group_id, target_group_id, year, month, new_window,
        )
    if total > 0:
        db.flush()
        logger.info(
            "FixedWantedEntry 재배치: nurse_id=%s, %s⇄%s, months=%s, touched=%d",
            nurse_id, source_group_id, target_group_id, sorted(months), total,
        )
    return total


def _validate_assignment_team_grade_or_raise(
    db: Session,
    *,
    nurse_id: str,
    target_group_id: Optional[str],
    old_target_team_id: Optional[int],
    new_target_team_id: Optional[int],
    old_target_grade: Optional[int],
    new_target_grade: Optional[int],
) -> None:
    """inbound assignment 의 target_team_id / target_grade 변경 정합성 검증.

    사이드프로필(PATCH /nurses/{id}.target_*) 와 동일 정책. target_group_id 미설정
    (휴직/퇴사/프리셉티 등) 인 경우 skip.
    """
    if not target_group_id:
        return
    if (
        old_target_team_id == new_target_team_id
        and old_target_grade == new_target_grade
    ):
        return
    from services.precheck.nurse_change_validators import validate_nurse_change

    result = validate_nurse_change(
        db,
        group_id=str(target_group_id),
        swap_nurse_id=str(nurse_id),
        old_team_id=old_target_team_id,
        new_team_id=new_target_team_id,
        old_grade=old_target_grade,
        new_grade=new_target_grade,
        scope="target",
    )
    if not result.get("saveable", True):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TEAM_GRADE_VALIDATION_FAILED",
                "message": "팀/Grade 변경이 인원 정합성을 충족하지 못합니다.",
                "issues": result.get("issues", []),
            },
        )


def create_assignment(
    req: NurseAssignmentCreate,
    db: Session,
    current_user: Optional[UserSchema] = None,
) -> NurseAssignmentResponse:
    """배정/상태 변경 등록"""
    _assert_caller_owns_source(current_user, req.source_group_id, db=db)

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

    # 병동이동 체인 검증 (source가 직전 target 또는 현재 group_id 와 일치)
    if req.reason == "병동이동":
        _assert_transfer_chain_source(db, req.nurse_id, req.source_group_id)

    # 파견/병동이동: target_group_id 필수 + source != target 검증
    _assert_valid_inbound_target(req.reason, req.source_group_id, req.target_group_id)

    # Office 경계 검증 (파견/병동이동: target_group이 동일 office 소속이어야 함)
    if req.reason in _INBOUND_REASONS and req.target_group_id:
        _assert_target_in_same_office(db, req.office_id, req.target_group_id)

    # 기간 중복 검증: 동일 간호사의 active assignment와 일자 겹침 불허
    _raise_if_overlap(db, req.nurse_id, req.start_date, req.expected_end_date)

    # 동일 target_group_id 재파견/재병동이동 시 이전 row 의 target_*/note 자동 승계
    _inherit_target_fields_from_prior(db, req)

    # 신규 inbound: target_group 의 team/grade 정합성 검증 (사이드프로필과 동일 정책)
    if req.reason in _INBOUND_REASONS:
        _validate_assignment_team_grade_or_raise(
            db,
            nurse_id=req.nurse_id,
            target_group_id=req.target_group_id,
            old_target_team_id=None,
            new_target_team_id=req.target_team_id,
            old_target_grade=None,
            new_target_grade=req.target_grade,
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
        note=req.note,
        target_weekly_off_type=req.target_weekly_off_type,
        target_weekly_off_enabled=req.target_weekly_off_enabled,
        target_weekly_off_weekday=req.target_weekly_off_weekday,
        target_shift_types=req.target_shift_types if req.target_shift_types is not None else [],
        target_team_id=req.target_team_id,
        target_grade=req.target_grade,
        target_fixed_shift=req.target_fixed_shift,
        target_wanted_max_requests=req.target_wanted_max_requests,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "배정 등록: nurse_id=%s, reason=%s, start=%s",
        req.nurse_id, req.reason, req.start_date,
    )

    # FixedWantedEntry 재배치 (파견/병동이동만)
    if req.reason in _INBOUND_REASONS and req.target_group_id:
        try:
            _reallocate_fixed_wanted_on_assignment_change(
                db,
                nurse_id=req.nurse_id,
                source_group_id=req.source_group_id,
                target_group_id=req.target_group_id,
                old_window=None,
                new_window=(row.start_date, _effective_end_date(row)),
            )
            db.commit()
        except Exception as e:
            logger.error("FixedWantedEntry 재배치 실패(create): %s", e, exc_info=True)
            db.rollback()

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


def _assert_transfer_chain_source(
    db: Session,
    nurse_id: str,
    source_group_id: str,
) -> None:
    """병동이동 체인 검증.

    후속 병동이동의 source_group_id는 직전 active 병동이동의 target_group_id와 일치해야 한다.
    직전 이동이 없으면 현재 nurses.group_id와 일치해야 한다.
    """
    prev = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id == nurse_id,
            NurseAssignment.reason == "병동이동",
            NurseAssignment.status == "active",
        )
        .order_by(NurseAssignment.start_date.desc())
        .first()
    )
    if prev is not None:
        if source_group_id != prev.target_group_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"병동이동 체인 불일치: 직전 이동 target={prev.target_group_id}, "
                    f"후속 source={source_group_id}"
                ),
            )
        return
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
    if nurse and source_group_id != nurse.group_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"source 불일치: 현재 그룹={nurse.group_id}, "
                f"요청 source={source_group_id}"
            ),
        )


def _assert_valid_inbound_target(
    reason: str,
    source_group_id: str,
    target_group_id: Optional[str],
) -> None:
    """파견/병동이동: target_group_id 필수 + source와 달라야 함."""
    if reason not in _INBOUND_REASONS:
        return
    if not target_group_id:
        raise HTTPException(
            status_code=422,
            detail=f"{reason}은(는) target_group_id 필수입니다.",
        )
    if target_group_id == source_group_id:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{reason}의 target_group_id가 source_group_id({source_group_id})와 "
                f"동일합니다. 다른 병동을 선택해 주세요."
            ),
        )


def _assert_target_in_same_office(
    db: Session,
    caller_office_id: str,
    target_group_id: str,
) -> None:
    """파견/병동이동의 target_group_id가 호출자 office_id와 동일한 오피스인지 검증."""
    target_group = (
        db.query(Group).filter(Group.group_id == target_group_id).first()
    )
    if target_group is None:
        raise HTTPException(
            status_code=400,
            detail=f"target_group_id={target_group_id}는 존재하지 않습니다.",
        )
    if target_group.office_id != caller_office_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"office 경계 위반: target group의 office={target_group.office_id}, "
                f"요청 office={caller_office_id}"
            ),
        )


def _raise_if_overlap(
    db: Session,
    nurse_id: str,
    my_start: date,
    my_end_upper: Optional[date],
    exclude_id: Optional[int] = None,
) -> None:
    """동일 간호사의 active 배정 중 기간 겹침이 있으면 409."""
    from sqlalchemy import case
    _eff_end = case(
        (NurseAssignment.end_date.isnot(None), NurseAssignment.end_date),
        else_=NurseAssignment.expected_end_date,
    )
    _filters = [
        NurseAssignment.nurse_id == nurse_id,
        NurseAssignment.status == "active",
        or_(_eff_end.is_(None), _eff_end >= my_start),
    ]
    if my_end_upper is not None:
        _filters.append(NurseAssignment.start_date <= my_end_upper)
    if exclude_id is not None:
        _filters.append(NurseAssignment.id != exclude_id)
    _overlap = db.query(NurseAssignment).filter(*_filters).first()
    if _overlap:
        raise HTTPException(
            status_code=409,
            detail=f"기간이 겹치는 배정이 존재합니다. (id={_overlap.id}, reason={_overlap.reason}, "
                   f"{_overlap.start_date}~{_overlap.end_date or _overlap.expected_end_date})",
        )


def update_assignment(
    assignment_id: int,
    req: NurseAssignmentUpdate,
    db: Session,
    current_user: Optional[UserSchema] = None,
) -> NurseAssignmentResponse:
    """배정/상태 변경 수정"""
    row = db.query(NurseAssignment).filter(NurseAssignment.id == assignment_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="배정 이력을 찾을 수 없습니다.")

    _assert_caller_owns_source(current_user, row.source_group_id, db=db)

    # 재배치 판정을 위해 기존(window) 스냅샷 저장
    _old_window = (row.start_date, _effective_end_date(row))
    _old_reason = row.reason
    _old_status = row.status
    _old_target_gid = row.target_group_id

    # 신규 값(변경 없으면 기존 값) 산정
    _new_start = req.start_date if req.start_date is not None else row.start_date
    _new_reason = req.reason if req.reason is not None else row.reason
    _new_target_gid = (
        req.target_group_id if req.target_group_id is not None else row.target_group_id
    )

    # 파견/병동이동: 최종 target_group_id 필수 + source != target 검증
    # (reason 또는 target_group_id 변경 시 재검증, 또는 기존 reason이 파견/병동이동인데 invalid state인 경우 차단)
    if _new_reason in _INBOUND_REASONS and (
        req.reason is not None or req.target_group_id is not None
    ):
        _assert_valid_inbound_target(_new_reason, row.source_group_id, _new_target_gid)

    # target_group_id 교체: office 경계 재검증 (파견/병동이동만 의미)
    if (
        req.target_group_id is not None
        and req.target_group_id != _old_target_gid
        and _new_reason in _INBOUND_REASONS
    ):
        _assert_target_in_same_office(db, row.office_id, req.target_group_id)

    # 병동이동 체인: reason/start_date 변경 시 체인 검증 재실행
    if _new_reason == "병동이동" and (
        req.reason is not None or req.start_date is not None
    ):
        _assert_transfer_chain_source(db, row.nurse_id, row.source_group_id)

    # 기간/상태/사유 변경 시 동일 간호사 active 배정과의 겹침 차단
    _period_changed = any(
        v is not None
        for v in (req.start_date, req.expected_end_date, req.end_date, req.status)
    )
    _new_status = req.status if req.status is not None else row.status
    if _period_changed and _new_status == "active":
        _new_end = req.end_date if req.end_date is not None else row.end_date
        _new_exp = req.expected_end_date if req.expected_end_date is not None else row.expected_end_date
        _eff_upper = _new_end if _new_end is not None else _new_exp
        _raise_if_overlap(db, row.nurse_id, _new_start, _eff_upper, exclude_id=row.id)

    # team/grade 변경 정합성 검증 (사이드프로필과 동일 정책)
    if _new_reason in _INBOUND_REASONS and _new_status == "active":
        _new_team_for_check = (
            req.target_team_id if req.target_team_id is not None else row.target_team_id
        )
        _new_grade_for_check = (
            req.target_grade if req.target_grade is not None else row.target_grade
        )
        _validate_assignment_team_grade_or_raise(
            db,
            nurse_id=row.nurse_id,
            target_group_id=_new_target_gid,
            old_target_team_id=row.target_team_id,
            new_target_team_id=_new_team_for_check,
            old_target_grade=row.target_grade,
            new_target_grade=_new_grade_for_check,
        )

    if req.start_date is not None:
        row.start_date = req.start_date
    if req.expected_end_date is not None:
        row.expected_end_date = req.expected_end_date
    if req.end_date is not None:
        row.end_date = req.end_date
    if req.status is not None:
        row.status = req.status
    if req.reason is not None:
        row.reason = req.reason
    if req.target_group_id is not None:
        row.target_group_id = req.target_group_id
    if req.note is not None:
        row.note = req.note

    for _f in (
        "target_weekly_off_type",
        "target_weekly_off_enabled",
        "target_weekly_off_weekday",
        "target_shift_types",
        "target_team_id",
        "target_grade",
        "target_fixed_shift",
        "target_wanted_max_requests",
    ):
        _v = getattr(req, _f, None)
        if _v is not None:
            setattr(row, _f, _v)

    db.commit()
    db.refresh(row)

    # FixedWantedEntry 재배치 (파견/병동이동 & 상태 변화 기준)
    if _old_reason in _INBOUND_REASONS and row.target_group_id:
        _was_active = _old_status == "active"
        _now_active = row.status == "active"
        _old = _old_window if _was_active else None
        _new = (row.start_date, _effective_end_date(row)) if _now_active else None
        if _old is not None or _new is not None:
            try:
                _reallocate_fixed_wanted_on_assignment_change(
                    db,
                    nurse_id=row.nurse_id,
                    source_group_id=row.source_group_id,
                    target_group_id=row.target_group_id,
                    old_window=_old,
                    new_window=_new,
                )
                db.commit()
            except Exception as e:
                logger.error("FixedWantedEntry 재배치 실패(update): %s", e, exc_info=True)
                db.rollback()

    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == row.nurse_id).first()
    return _to_response(row, nurse.name if nurse else None)


def cancel_assignment(
    assignment_id: int,
    db: Session,
    current_user: Optional[UserSchema] = None,
) -> NurseAssignmentResponse:
    """배정 취소 (status → cancelled)"""
    row = db.query(NurseAssignment).filter(NurseAssignment.id == assignment_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="배정 이력을 찾을 수 없습니다.")

    _assert_caller_owns_source(current_user, row.source_group_id, db=db)

    _old_window = (row.start_date, _effective_end_date(row))
    _old_reason = row.reason
    _old_status = row.status

    row.status = "cancelled"
    db.commit()
    db.refresh(row)
    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == row.nurse_id).first()

    # FixedWantedEntry 재배치: 기존 target-period 엔트리 → source로 복귀
    if (
        _old_reason in _INBOUND_REASONS
        and _old_status == "active"
        and row.target_group_id
    ):
        try:
            _reallocate_fixed_wanted_on_assignment_change(
                db,
                nurse_id=row.nurse_id,
                source_group_id=row.source_group_id,
                target_group_id=row.target_group_id,
                old_window=_old_window,
                new_window=None,
            )
            db.commit()
        except Exception as e:
            logger.error("FixedWantedEntry 재배치 실패(cancel): %s", e, exc_info=True)
            db.rollback()

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


def get_assignment_status_counts(
    db: Session,
    office_id: str,
    group_id: Optional[str] = None,
    nurse_id: Optional[str] = None,
) -> AssignmentStatusCounts:
    """status 필터와 무관한 전체 카운트 집계 (리스트 응답 메타용)."""
    query = db.query(NurseAssignment.status, func.count(NurseAssignment.id)).filter(
        NurseAssignment.office_id == office_id
    )
    if group_id:
        query = query.filter(
            or_(
                NurseAssignment.source_group_id == group_id,
                NurseAssignment.target_group_id == group_id,
            )
        )
    if nurse_id:
        query = query.filter(NurseAssignment.nurse_id == nurse_id)
    counts = {"active": 0, "completed": 0, "cancelled": 0, "on_hold": 0}
    for status_val, cnt in query.group_by(NurseAssignment.status).all():
        if status_val in counts:
            counts[status_val] = int(cnt)
    return AssignmentStatusCounts(**counts)


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


def _apply_target_profile_reset(
    db: Session,
    nurse: NurseModel,
    row: NurseAssignment,
) -> int:
    """병동이동 발효 시 간호사 속성을 target overlay 또는 기본값으로 초기화.

    프리셉터/프리셉티 비대칭도 함께 해제.
    Returns: 해제된 하위 프리셉티 수 (로깅용).
    """
    nurse.grade = row.target_grade
    nurse.fixed_shift = row.target_fixed_shift
    # is_night_nurse는 네이밍과 달리 실제로 근무 가능 shift 타입 JSON list 저장용 컬럼임
    nurse.is_night_nurse = row.target_shift_types if row.target_shift_types is not None else []
    nurse.weekly_off_enabled = (
        row.target_weekly_off_enabled if row.target_weekly_off_enabled is not None else False
    )
    nurse.weekly_off_type = row.target_weekly_off_type
    nurse.weekly_off_weekday = row.target_weekly_off_weekday
    nurse.wanted_max_requests = (
        row.target_wanted_max_requests if row.target_wanted_max_requests is not None else 0
    )
    nurse.is_weekend_off = False

    detached = db.query(NurseModel).filter(NurseModel.preceptor_id == row.nurse_id).update(
        {NurseModel.preceptor_id: None}, synchronize_session=False
    )
    if nurse.preceptor_id is not None:
        nurse.preceptor_id = None
    return int(detached or 0)


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
            detached = _apply_target_profile_reset(db, nurse, row)
            logger.info(
                "[transfer] nurse_id=%s, %s → %s target overlay applied, preceptees detached=%d",
                row.nurse_id, row.source_group_id, row.target_group_id, detached,
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
            detached = _apply_target_profile_reset(db, nurse, row)
            logger.info(
                "[Scheduler][transfer] nurse_id=%s, %s → %s target overlay applied, preceptees detached=%d",
                row.nurse_id, row.source_group_id, row.target_group_id, detached,
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


def flush_expired_preceptees(db: Session) -> int:
    """프리셉티 기간 만료 자동 해제 (스케줄러용).

    expected_end_date < 오늘인 active 프리셉티 assignment를 찾아:
    - nurses.preceptor_id = NULL
    - assignment status = completed, end_date = expected_end_date
    - 알림 발송 (운영자, 해당 간호사, 프리셉터)
    """
    today = date.today()
    rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.reason == "프리셉티",
            NurseAssignment.status == "active",
            NurseAssignment.expected_end_date.isnot(None),
            NurseAssignment.expected_end_date < today,
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
        preceptor_id = nurse.preceptor_id if nurse else None
        preceptor = (
            db.query(NurseModel)
            .filter(NurseModel.nurse_id == preceptor_id)
            .first()
        ) if preceptor_id else None

        # preceptor_id 해제
        if nurse and nurse.preceptor_id:
            logger.info(
                "[Scheduler] 프리셉티 해제: nurse_id=%s, preceptor_id=%s → NULL",
                row.nurse_id, nurse.preceptor_id,
            )
            nurse.preceptor_id = None

        row.status = "completed"
        row.end_date = row.expected_end_date
        count += 1

        # 알림 발송
        try:
            from utils.utils import set_app_push
            nurse_name = nurse.name if nurse else str(row.nurse_id)
            preceptor_name = preceptor.name if preceptor else "?"
            _group_name = _get_group_name(db, row.source_group_id) or str(row.source_group_id)
            push_message = f"{nurse_name} 프리셉티 기간 종료 (프리셉터: {preceptor_name}, {_group_name})"

            _recipients = {str(row.nurse_id)}
            if preceptor_id:
                _recipients.add(str(preceptor_id))
            for nid in _get_head_nurse_ids(db, row.source_group_id):
                _recipients.add(nid)

            set_app_push(
                pushCode="P30", pushSubCode="S13",
                officeCode=row.office_id,
                sendEmpSeqNo=row.nurse_id,
                sendMemberId=row.nurse_id,
                receiveEmpSeqNo=",".join(map(str, _recipients)),
                pushMessage=push_message, orgPushMessage=push_message,
                linkUrl="", linkCode="",
            )
        except Exception as e:
            logger.error("[Scheduler] 프리셉티 해제 알림 실패: %s", e, exc_info=True)

    if count > 0:
        db.commit()
        logger.info("[Scheduler] 프리셉티 자동 해제 완료: %d건", count)

    return count


def flush_orphan_preceptee_assignments(db: Session) -> int:
    """nurses.preceptor_id=NULL 이지만 active 프리셉티 assignment 가 떠있는 비대칭을 정리.

    원인: 외부 경로(직접 INSERT, 프론트 가드로 PATCH 누락 등)로 nurses.preceptor_id 와
    nurse_assignment 가 어긋난 경우. lazy 체크 시점에 active row 를 모두 cancelled 로 정리.
    알림은 발송하지 않는다(자동 정리는 사용자 의도 변경이 아님).
    """
    rows = (
        db.query(NurseAssignment)
        .join(NurseModel, NurseModel.nurse_id == NurseAssignment.nurse_id)
        .filter(
            NurseAssignment.reason == "프리셉티",
            NurseAssignment.status == "active",
            NurseModel.preceptor_id.is_(None),
        )
        .all()
    )
    if not rows:
        return 0
    today = date.today()
    for row in rows:
        row.status = "cancelled"
        if row.end_date is None:
            row.end_date = today
    db.commit()
    logger.info("[Lazy] orphan 프리셉티 assignment 정리: %d건", len(rows))
    return len(rows)


def flush_expired_dispatches(db: Session) -> int:
    """파견 만료 자동 디엑티브 (스케줄러용).

    조건: reason='파견' AND status='active' AND expected_end_date < today
    처리: status='completed', end_date=expected_end_date. target_* 은 이력으로만 남김.
    Returns: 처리된 건수
    """
    today = date.today()
    rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.reason == "파견",
            NurseAssignment.status == "active",
            NurseAssignment.expected_end_date.isnot(None),
            NurseAssignment.expected_end_date < today,
        )
        .all()
    )
    if not rows:
        return 0

    for row in rows:
        row.status = "completed"
        row.end_date = row.expected_end_date

    db.commit()
    logger.info("[Scheduler] 파견 자동 디엑티브 %d건", len(rows))
    return len(rows)


def flush_expired_leaves(db: Session) -> int:
    """휴직 만료 자동 디엑티브 (스케줄러용).

    조건: reason='휴직' AND status='active' AND expected_end_date < today
    처리: status='completed', end_date=expected_end_date.
    Returns: 처리된 건수
    """
    today = date.today()
    rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.reason == "휴직",
            NurseAssignment.status == "active",
            NurseAssignment.expected_end_date.isnot(None),
            NurseAssignment.expected_end_date < today,
        )
        .all()
    )
    if not rows:
        return 0

    for row in rows:
        row.status = "completed"
        row.end_date = row.expected_end_date

    db.commit()
    logger.info("[Scheduler] 휴직 자동 디엑티브 %d건", len(rows))
    return len(rows)


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
) -> dict[str, list[dict]]:
    """근무표 응답용 파견/병동이동/휴직/퇴사 assignment 메타데이터.

    해당 group이 source인 assignment + 해당 월 퇴사자 synthetic entry.
    Returns: {nurse_id: [{reason, target_group_id, target_group_name, start_date, end_date}]}
    간호사당 복수 assignment 지원.
    """
    from calendar import monthrange

    _days = monthrange(year, month)[1]
    _m_start = date(year, month, 1)
    _m_end = date(year, month, _days)
    _DISPATCH_REASON_ALIASES = frozenset({"파견", "병동이동", "부서이동"})

    assignments = get_active_assignments_for_month(db, group_id, year, month)
    eligible = [
        a for a in assignments
        if a.reason in ("파견", "병동이동", "휴직")
        and a.source_group_id == group_id
    ]

    # 해당 월 퇴사자 (nurses.resignation_date 기반 synthetic entry)
    _resigning_all = (
        db.query(NurseModel)
        .filter(
            NurseModel.group_id == group_id,
            NurseModel.resignation_date.isnot(None),
            NurseModel.resignation_date >= _m_start,
            NurseModel.resignation_date <= _m_end,
        )
        .all()
    )
    _resigning = [
        n for n in _resigning_all
        if (n.resignation_reason or "").strip() not in _DISPATCH_REASON_ALIASES
    ]

    if not eligible and not _resigning:
        return {}

    target_gids = {a.target_group_id for a in eligible if a.target_group_id}
    gname_map: dict[str, str] = {}
    if target_gids:
        groups = db.query(Group).filter(Group.group_id.in_(target_gids)).all()
        gname_map = {g.group_id: g.group_name for g in groups}

    result: dict[str, list[dict]] = {}
    for a in eligible:
        entry = {
            "reason": a.reason,
            "target_group_id": a.target_group_id or "",
            "target_group_name": gname_map.get(a.target_group_id, "") if a.target_group_id else "",
            "start_date": str(a.start_date),
            "end_date": str(a.end_date or a.expected_end_date) if (a.end_date or a.expected_end_date) else None,
        }
        result.setdefault(a.nurse_id, []).append(entry)
    for n in _resigning:
        # 퇴사일 당일부터 월말까지 블랭크 (프론트 배지/기간바 용)
        result.setdefault(n.nurse_id, []).append({
            "reason": "퇴사",
            "target_group_id": "",
            "target_group_name": "",
            "start_date": str(n.resignation_date),
            "end_date": None,
        })
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
        "note": getattr(a, "note", None),
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
        note=getattr(row, "note", None),
        target_weekly_off_type=row.target_weekly_off_type,
        target_weekly_off_enabled=row.target_weekly_off_enabled,
        target_weekly_off_weekday=row.target_weekly_off_weekday,
        target_shift_types=row.target_shift_types,
        target_team_id=row.target_team_id,
        target_grade=row.target_grade,
        target_fixed_shift=row.target_fixed_shift,
        target_wanted_max_requests=row.target_wanted_max_requests,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
