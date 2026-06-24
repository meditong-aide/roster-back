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

# nurse_assignment.kind enum (DDL Phase 1.4) ↔ 한글 reason 매핑.
# 매칭 안 되는 reason 은 DB DEFAULT 'transfer' 로 떨어지므로, 신규 reason 추가 시 여기도 갱신할 것.
REASON_TO_KIND: dict[str, str] = {
    "병동이동": "transfer",
    "파견": "dispatch",
    "프리셉티": "preceptee",
    "휴직": "leave",
    "복직": "return",
    "퇴사": "resign",
    "속성변경": "permanent_change",
}

# 속성 이벤트(무엇인가) — 존재 이벤트(어디 있나: 파견/병동이동)와 달리 기간 겹침 허용.
# overlap·transfer 검증기에서 제외하고, 발효 시 Nurse 속성을 직접 갱신한다.
ATTRIBUTE_CHANGE_KINDS: frozenset[str] = frozenset({"permanent_change"})


def kind_for_reason(reason: Optional[str]) -> str:
    """한글 reason 을 kind enum 으로 변환. 부분일치(LIKE 백필과 동일 의미)로 변형 표기도 흡수."""
    if not reason:
        return "transfer"
    for key, kind in REASON_TO_KIND.items():
        if key in reason:
            return kind
    return "transfer"


def _assert_caller_owns_source(
    current_user: Optional[UserSchema],
    source_group_id: str,
    db: Optional[Session] = None,
    target_group_id: Optional[str] = None,
) -> None:
    """파견/병동이동/휴직 등 assignment 조작 권한 검증.

    통과 조건 (OR):
    - current_user is None → system/admin 경로로 간주
    - is_master_admin
    - caller 가 source_group_id 소유 (group_id / original_group_id / managed)
    - **target_group_id 가 주어지면**(취소 등) caller 가 target_group_id 소유여도 통과.
      A→B 파견을 전입 받은 B(target)에서도 취소→재등록할 수 있게. 생성·수정 호출은
      target_group_id 를 넘기지 않으므로 기존대로 **source 전용**으로 유지된다.
    """
    if current_user is None:
        return
    if getattr(current_user, "is_master_admin", False):
        return
    _allowed = {str(source_group_id)}
    if target_group_id is not None:
        _allowed.add(str(target_group_id))
    caller_gid = getattr(current_user, "group_id", None)
    if str(caller_gid) in _allowed:
        return
    caller_original = getattr(current_user, "original_group_id", None)
    if caller_original and str(caller_original) in _allowed:
        return
    if db is not None:
        from services.group_access import resolve_managed_group_ids
        managed = {str(g) for g in resolve_managed_group_ids(db, current_user)}
        if _allowed & managed:
            return
    raise HTTPException(
        status_code=403,
        detail=(
            f"권한 없음: 해당 배정의 source({source_group_id})"
            f"{'/target(' + str(target_group_id) + ')' if target_group_id else ''} "
            f"병동 수간호사·그룹 관리자만 조작할 수 있습니다. (caller={caller_gid})"
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
    notify: bool = True,
) -> NurseAssignmentResponse:
    """배정/상태 변경 등록.

    notify=False: 개별 알림(S06) 생략 — 벌크 호출(병동재분배)에서 끝에 요약 1건으로 묶기 위함.
    """
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

    # [팀 절연] 파견(임시)은 팀과 엮지 않는다. team SSOT 는 nurse_team_period 이며 파견은
    #   팀 로테이션 비참여(team None) — 생성기에서 team=None 으로 D/E 커버리지 풀에서 빠진다.
    #   승계/명시 어느 경로로 team 이 들어와도 여기서 강제로 비운다.
    if req.reason == "파견":
        req.target_team_id = None

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
        kind=kind_for_reason(req.reason),
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

    # [팀 SSOT] 병동이동의 target 팀을 nurse_team_period(target_group)에 일원화 기록.
    #   수동 병동이동(프로필 패널 _dispatch_assignment_payload 등)은 외부 set_team_period 가
    #   없어 이 기록이 없으면 period 공백→화면/생성기에 팀 미반영(원 버그와 동종). 팩토리 안에서
    #   보장한다. 파견은 team None(절연)이라 해당 없음. ward_redistribute 의 외부 호출과는
    #   valid_from 동일 → 멱등(same-row upsert).
    if req.reason == "병동이동" and req.target_team_id is not None:
        from services.team_period import set_team_period
        set_team_period(
            db, nurse_id=req.nurse_id, group_id=req.target_group_id,
            valid_from=req.start_date, team_id=req.target_team_id,
            source="transfer", note=req.note,
        )

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

    # 알림 발송 (S06) — notify=False(벌크/재분배)면 개별 발송 생략(끝에 요약 1건).
    if notify:
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
    """동일 간호사의 active 존재 배정 중 기간 겹침이 있으면 409.

    속성 이벤트(ATTRIBUTE_CHANGE_KINDS: team/grade 변경)는 '어디 있나'가 아니라 '무엇인가'라
    겹침 검사 대상이 아니다 — 팀이 바뀐 채로도 파견될 수 있으므로 제외한다.
    """
    from sqlalchemy import case
    _eff_end = case(
        (NurseAssignment.end_date.isnot(None), NurseAssignment.end_date),
        else_=NurseAssignment.expected_end_date,
    )
    _filters = [
        NurseAssignment.nurse_id == nurse_id,
        NurseAssignment.status == "active",
        NurseAssignment.kind.notin_(ATTRIBUTE_CHANGE_KINDS),
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

    # [팀 절연] 최종 reason 이 파견이면 team 검증 입력을 비워 곧 버릴 팀을 검사하지 않게 한다.
    #   (영속 클리어는 함수 끝단 가드가 담당 — setattr 루프는 None 을 건너뛰므로.)
    if _new_reason == "파견":
        req.target_team_id = None

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

    # [팀 절연] 파견(임시)은 팀과 엮지 않는다(create_assignment 와 동일 정책).
    #   reason 변경/팀 setattr 어느 경로로 들어와도 파견 row 는 team None 으로 수렴.
    if row.reason == "파견":
        row.target_team_id = None

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

    # 취소는 source(기존 병동) + target(전입 받은 병동) 양쪽 허용 — target 그룹이 잘못
    # 들어온 전입자 파견을 직접 취소→재등록 가능하게. (생성·수정은 source 전용 유지)
    _assert_caller_owns_source(
        current_user, row.source_group_id, db=db, target_group_id=row.target_group_id
    )

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


def group_members_in_month(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    nurse_pool: Optional[dict] = None,
) -> dict:
    """선택 월 '소속' 명단 + 상태 플래그 (근무자관리 월 셀렉터용, Phase 4).

    가시성=소속(근무일 수 아님). assignment 등록 즉시 적용(cancelled 제외).
    - 홈 재직자(nurses.group_id==group, active=1) 기준.
    - 영구 전출(병동이동, start<=월초)로 완전히 떠난 사람 → 제외(그 달 미표시).
    - 전출 transition(병동이동, start 월중) → status='outbound', marker='←'.
    - 파견 나감(source==group) → status='dispatch_out', badge='파견 중'.
    - 휴직/퇴사(source==group) → status='leave'/'resigned' (지속 상태 배지).
    - 인바운드(target==group, source!=group), as-of team/grade=target_*:
      · 병동이동 = 일회성 전이 → start ∈ 그 달(이동月)에만 status='inbound'/marker='→',
        지난 달 발효(정착)면 status='active'(마커 없음). flush 전이어도 명단 포함.
      · 파견 = 지속 상태 → 기간 내내 status='inbound'/badge='파견'(마커 없음).
    마커(←/→)=일회성 전이(병동이동) / 배지(파견/휴직)=지속 상태. 라는 원칙.
    각 멤버: as_of_team(resolve_team), as_of_grade(캐시/override — grade는 경량, period 없음).

    Returns: {"members":[...], "headcount":{"regular","moving","leave"}}
    참조: docs/TEMPORAL_NURSE_MODEL_DESIGN.md §3 정책 매트릭스.

    nurse_pool: {nurse_id(str) → NurseModel}. 호출자(get_nurses_in_group_service)가 이미
      로드한 nurse 객체를 재사용해 중복 쿼리(home·inbound 조회)를 제거하기 위한 optional 입력.
      None(standalone /members 등)이면 기존과 100% 동일하게 DB 에서 조회한다.
      배지/membership/as_of_team/headcount 계산은 NurseModel 의 '출처'만 바뀔 뿐 로직 무변경.
    """
    from calendar import monthrange
    from sqlalchemy import or_
    from db.models import NurseTeamPeriod
    from services.team_period import _coerce_team_int
    from services.cp_sat.allowed_shift_types import is_n_only_profile

    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])
    assignments = get_active_assignments_for_month(db, group_id, year, month)

    inbound: dict[str, NurseAssignment] = {}
    outbound: dict[str, NurseAssignment] = {}
    leave: dict[str, NurseAssignment] = {}
    for a in assignments:
        nid = str(a.nurse_id)
        if a.reason in ("파견", "병동이동"):
            if a.target_group_id == group_id and a.source_group_id != group_id:
                inbound[nid] = a
            elif a.source_group_id == group_id and a.target_group_id != group_id:
                outbound[nid] = a
        elif a.reason in ("휴직", "퇴사") and a.source_group_id == group_id:
            leave[nid] = a

    if nurse_pool is not None:
        # 호출자가 이미 로드한 nurse 재사용 → home 조회 쿼리 제거.
        #   SQL(group_id==group_id AND active==1) 과 동치인 in-memory 필터.
        home = [
            n
            for n in nurse_pool.values()
            if str(getattr(n, "group_id", "") or "").strip() == str(group_id or "").strip()
            and getattr(n, "active", None) == 1
        ]
    else:
        home = (
            db.query(NurseModel)
            .filter(NurseModel.group_id == group_id, NurseModel.active == 1)
            .all()
        )

    # [Perf] resolve_team N+1 제거: nurse_team_period 를 그룹 단위로 1번에 배치 로드.
    #   month_start 를 덮는 구간만, valid_from desc 첫 행 = get_team_period_on 과 동일.
    #   폴백(구간 없음)은 이미 로드한 nurse 객체의 team_id 를 메모리에서 읽는다(NurseModel 재조회 X).
    _periods = (
        db.query(NurseTeamPeriod)
        .filter(
            NurseTeamPeriod.group_id == group_id,
            NurseTeamPeriod.valid_from <= month_start,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > month_start,
            ),
        )
        .order_by(NurseTeamPeriod.valid_from.desc())
        .all()
    )
    _period_team_by_nurse: dict[str, Optional[int]] = {}
    for _p in _periods:
        _pid = str(_p.nurse_id)
        if _pid not in _period_team_by_nurse:  # valid_from desc 첫 등장 = 구간 우선
            _period_team_by_nurse[_pid] = _coerce_team_int(_p.team_id)

    def _resolved_team(nid: str, n_obj) -> Optional[int]:
        """resolve_team 메모리판: 구간 우선(있으면 team_id=None 도 그대로), 없으면 ward-aware 폴백."""
        if nid in _period_team_by_nurse:
            return _period_team_by_nurse[nid]
        if n_obj is not None and str(getattr(n_obj, "group_id", "") or "").strip() == str(group_id or "").strip():
            return _coerce_team_int(getattr(n_obj, "team_id", None))
        return None

    # [Perf] grade·allowed_shifts(전담) 도 월 as-of 로 배치 해석(team 과 동일 원칙).
    #   home(소속) 간호사만 — inbound 는 파견지 override(target_*) 사용. gap→캐시 폴백(무회귀).
    from datetime import timedelta as _timedelta
    from services.nurse_period_resolver import (
        fetch_periods as _fetch_periods, resolve_asof as _resolve_asof,
    )
    from db.models import (
        NurseGradePeriod as _NGP, NurseAllowedShiftPeriod as _NASP,
        NurseWeekendOffPeriod as _NWOP,
    )

    _home_ids = [str(n.nurse_id) for n in home]
    _asof_next = month_start + _timedelta(days=1)
    _grade_periods = _fetch_periods(db, _NGP, _home_ids, month_start, _asof_next, group_id=group_id)
    _allowed_periods = _fetch_periods(db, _NASP, _home_ids, month_start, _asof_next)
    _weekend_periods = _fetch_periods(db, _NWOP, _home_ids, month_start, _asof_next)

    def _asof_grade(nid: str, n_obj):
        v = _resolve_asof(_grade_periods.get(nid), month_start, "grade", default=None)
        return v if v is not None else getattr(n_obj, "grade", None)

    def _asof_night(nid: str, n_obj):
        v = _resolve_asof(_allowed_periods.get(nid), month_start, "allowed_shifts", default=None)
        return v if v is not None else getattr(n_obj, "is_night_nurse", None)

    def _asof_fixed(nid: str, n_obj):
        # fixed_shift 는 allowed satellite 의 형제 컬럼(같은 row)
        v = _resolve_asof(_allowed_periods.get(nid), month_start, "fixed_shift", default=None)
        return v if v is not None else getattr(n_obj, "fixed_shift", None)

    def _asof_weekend(nid: str, n_obj):
        v = _resolve_asof(_weekend_periods.get(nid), month_start, "weekend_off", default=None)
        return v if v is not None else (1 if getattr(n_obj, "is_weekend_off", False) else 0)

    def _row(n, status, marker, badge, team, grade, night_profile=...,
             weekend_off=..., fixed_shift=...):
        # N전담(허용 shift=N뿐) 판정 → 'N전담' 배지 + is_night_dedicated.
        # ★inbound 은 파견지 유효 근무형태(target_shift_types)로 판정해야 한다(base nurses.is_night_nurse 가
        #   아니라). 안 그러면 근무형태=DEN 인데 배지만 N전담으로 남는 불일치(/nurses·엔진은 오버레이 사용).
        #   night_profile 미지정(home) 은 base 사용. (생성기는 N전담을 팀 D/E 커버리지에서 제외.)
        _np = getattr(n, "is_night_nurse", None) if night_profile is ... else night_profile
        _wo = (1 if getattr(n, "is_weekend_off", False) else 0) if weekend_off is ... else weekend_off
        _fx = getattr(n, "fixed_shift", None) if fixed_shift is ... else fixed_shift
        night_dedicated = is_n_only_profile(_np)
        if night_dedicated and badge is None:
            badge = "N전담"
        return {
            "nurse_id": str(n.nurse_id),
            "name": getattr(n, "name", None),
            "membership_status": status,   # active|outbound|dispatch_out|leave|resigned|inbound
            "marker": marker,              # '←'(전출) | '→'(전입) | None
            "badge": badge,                # '파견 중' | '휴직' | '퇴사' | '파견' | 'N전담' | None
            # N전담(허용=N뿐)은 팀 배정 풀에서 제외 → 근무자관리에 팀 미배정(None) 표시.
            #   commit 6f066ff 의도였으나 team SSOT→period 리팩터 때 누락됨. as-of 기준으로 복원.
            "as_of_team": None if night_dedicated else team,
            "as_of_grade": grade,
            "is_night_dedicated": night_dedicated,
            "as_of_is_night_nurse": _np,   # 월 as-of 전담 프로필(raw, /nurses base 덮어쓰기용)
            "as_of_weekend_off": _wo,
            "as_of_fixed_shift": _fx,
        }

    members: list[dict] = []
    seen: set[str] = set()
    for n in home:
        nid = str(n.nurse_id)
        seen.add(nid)
        team = _resolved_team(nid, n)
        grade = _asof_grade(nid, n)      # 월 as-of (gap→nurses.grade)
        _np = _asof_night(nid, n)        # 월 as-of 전담 프로필 (gap→nurses.is_night_nurse)
        _wo = _asof_weekend(nid, n)      # 월 as-of 주말휴무 (gap→nurses.is_weekend_off)
        _fx = _asof_fixed(nid, n)        # 월 as-of 고정근무 (gap→nurses.fixed_shift)
        _kw = dict(night_profile=_np, weekend_off=_wo, fixed_shift=_fx)
        if nid in outbound:
            a = outbound[nid]
            if a.reason == "병동이동":
                if a.start_date <= month_start:
                    continue  # 완전 전출 → 그 달 미표시
                members.append(_row(n, "outbound", "←", None, team, grade, **_kw))
            else:  # 파견 나감 — home 유지
                members.append(_row(n, "dispatch_out", None, "파견 중", team, grade, **_kw))
        elif nid in leave:
            a = leave[nid]
            if a.reason == "퇴사":
                members.append(_row(n, "resigned", None, "퇴사", team, grade, **_kw))
            else:
                members.append(_row(n, "leave", None, "휴직", team, grade, **_kw))
        else:
            members.append(_row(n, "active", None, None, team, grade, **_kw))

    # [Perf] inbound 도 nurse_id IN 배치 1쿼리 (간호사별 NurseModel 재조회 제거)
    _inbound_ids = [nid for nid in inbound if nid not in seen]
    _inbound_nurses: dict[str, NurseModel] = {}
    if _inbound_ids and nurse_pool is not None:
        # 호출자 풀에 이미 있는 inbound 은 재사용. 풀에 없는 id 만 폴백 쿼리.
        for nid in _inbound_ids:
            _pooled = nurse_pool.get(nid)
            if _pooled is not None:
                _inbound_nurses[nid] = _pooled
        _inbound_ids = [nid for nid in _inbound_ids if nid not in _inbound_nurses]
    if _inbound_ids:
        for _n in db.query(NurseModel).filter(NurseModel.nurse_id.in_(_inbound_ids)):
            _inbound_nurses[str(_n.nurse_id)] = _n
    for nid, a in inbound.items():
        if nid in seen:
            continue
        n = _inbound_nurses.get(nid)
        if n is None:
            continue
        # team SSOT = nurse_team_period. inbound 도 period 만 본다(target_team_id 미사용 — 팀 이행 완료).
        # period 가 그 달을 덮으면 그 팀, 없으면 None(미배정). 팀설정 변경이 곧바로 반영된다.
        team = _resolved_team(nid, n)
        grade = a.target_grade if a.target_grade is not None else getattr(n, "grade", None)
        # 파견지 유효 근무형태(target_shift_types) — N전담 배지 판정용. []=전체(비N전담), 엔진/`/nurses`와 동일.
        _np = a.target_shift_types or []
        # 고정근무도 파견지 override(target_fixed_shift) 우선, 없으면 base. 주말휴무는 override 없어 base.
        _fx = a.target_fixed_shift if a.target_fixed_shift is not None else getattr(n, "fixed_shift", None)
        _kw = dict(night_profile=_np, fixed_shift=_fx)
        if a.reason == "파견":
            # 파견(일시·복귀 예정) = 지속 상태 → 기간 내내 '파견' 배지(전이 아님, 마커 없음).
            members.append(_row(n, "inbound", None, "파견", team, grade, **_kw))
        elif a.start_date is not None and month_start <= a.start_date <= month_end:
            # 병동이동 = 일회성 전이 → 이동月(start ∈ 그 달)에만 전입(→).
            members.append(_row(n, "inbound", "→", None, team, grade, **_kw))
        else:
            # 이미 이동 완료(지난 달 발효) → 정착 일반 멤버(마커 없음).
            #   캐시(nurses.group_id) flush 전이어도 그 병동 소속이므로 명단엔 포함한다.
            members.append(_row(n, "active", None, None, team, grade, **_kw))

    headcount = {
        "regular": sum(1 for m in members if m["membership_status"] == "active"),
        "moving": sum(1 for m in members if m["membership_status"] in ("outbound", "dispatch_out", "inbound")),
        "leave": sum(1 for m in members if m["membership_status"] in ("leave", "resigned")),
    }
    return {"members": members, "headcount": headcount}


def preview_assignment_impact(
    db: Session,
    *,
    nurse_id: str,
    reason: str,
    start_date: date,
    target_group_id: Optional[str] = None,
    expected_end_date: Optional[date] = None,
    exclude_id: Optional[int] = None,
) -> dict:
    """배정(파견/병동이동/휴직/등) 생성 전 영향 분석 (dry-run).

    실제 DB 변경 없이 영향만 계산해 반환. UI 확정 직전 운영자가 영향을 확인하는 용도.

    참조: docs/NURSE_GROUP_CHANGE_MODEL.md §2.6, NURSE_ASSIGNMENT_CRON_DESIGN.md §5.

    Returns:
        dict with:
          - nurse_id / nurse_name / source_group_id / target_group_id / reason / 기간
          - conflicts: 기간 겹침 active 배정 리스트 (raise 안 함)
          - nml_affected: 발효 월 이후의 NML 중 group_id가 옛 그룹인 것 (자동 update 대상)
          - wanted_affected: 발효 월 이후 wanted 카운트 (정책: 안 건드림, generate가 자연 무시)
          - schedules_to_check: 발효 월 이후 이미 생성된 schedule (재생성 검토)
          - notifications: 알림 대상 (self / source_hn / target_hn)
    """
    from sqlalchemy import case
    from db.models import NurseMonthlyLimit, WantedRequest, Schedule

    nurse = db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
    if not nurse:
        raise HTTPException(status_code=404, detail="간호사를 찾을 수 없습니다.")
    source_group_id = nurse.group_id

    # 1. 기간 겹침 (raise 안 함, 목록 반환) — _raise_if_overlap 로직 재사용
    _eff_end = case(
        (NurseAssignment.end_date.isnot(None), NurseAssignment.end_date),
        else_=NurseAssignment.expected_end_date,
    )
    _filters = [
        NurseAssignment.nurse_id == nurse_id,
        NurseAssignment.status == "active",
        or_(_eff_end.is_(None), _eff_end >= start_date),
    ]
    if expected_end_date is not None:
        _filters.append(NurseAssignment.start_date <= expected_end_date)
    if exclude_id is not None:
        _filters.append(NurseAssignment.id != exclude_id)
    overlaps = db.query(NurseAssignment).filter(*_filters).all()
    conflicts = [
        {
            "id": o.id,
            "reason": o.reason,
            "start_date": o.start_date.isoformat(),
            "end_date": (o.end_date or o.expected_end_date).isoformat()
                        if (o.end_date or o.expected_end_date) else None,
            "target_group_id": o.target_group_id,
            "status": o.status,
        }
        for o in overlaps
    ]

    start_y, start_m = start_date.year, start_date.month
    start_ym = f"{start_y:04d}-{start_m:02d}"

    # 2. NML 영향 — 병동이동 시 발효 월 이후 NML group_id 자동 update 대상
    nml_affected = []
    if reason == "병동이동" and target_group_id and target_group_id != source_group_id:
        rows = (
            db.query(NurseMonthlyLimit)
            .filter(
                NurseMonthlyLimit.nurse_id == nurse_id,
                NurseMonthlyLimit.group_id == source_group_id,
                or_(
                    NurseMonthlyLimit.year > start_y,
                    and_(
                        NurseMonthlyLimit.year == start_y,
                        NurseMonthlyLimit.month >= start_m,
                    ),
                ),
            )
            .all()
        )
        nml_affected = [
            {
                "year": r.year,
                "month": r.month,
                "current_group_id": r.group_id,
                "will_become": target_group_id,
            }
            for r in rows
        ]

    # 3. wanted 영향 — 정책: 안 건드림. 단 발효 월 이후 wanted 수를 노출 (운영자 인지용)
    wanted_count = 0
    wanted_range = None
    try:
        if reason == "병동이동" and target_group_id:
            wq = (
                db.query(WantedRequest)
                .filter(
                    WantedRequest.nurse_id == nurse_id,
                    WantedRequest.group_id == source_group_id,
                    WantedRequest.month >= start_ym,
                )
                .all()
            )
            wanted_count = len(wq)
            if wq:
                months = sorted(w.month for w in wq)
                wanted_range = {"earliest": months[0], "latest": months[-1]}
    except Exception as e:
        logger.warning("preview wanted 조회 실패: %s", e)
    wanted_affected = {
        "count": wanted_count,
        "range": wanted_range,
        "policy_note": "이동 시 안 건드림 — generate가 새 group_id로 필터하므로 옛 그룹 wanted는 자연 무시",
    }

    # 4. 발효 월 이후 이미 생성된 schedule (재생성 검토 대상)
    schedules_to_check = []
    try:
        group_ids = [source_group_id]
        if target_group_id and target_group_id != source_group_id:
            group_ids.append(target_group_id)
        sched_rows = (
            db.query(Schedule)
            .filter(
                Schedule.group_id.in_(group_ids),
                or_(
                    Schedule.year > start_y,
                    and_(Schedule.year == start_y, Schedule.month >= start_m),
                ),
                Schedule.dropped == False,  # noqa: E712
            )
            .all()
        )
        schedules_to_check = [
            {
                "schedule_id": s.schedule_id,
                "year": s.year,
                "month": s.month,
                "group_id": s.group_id,
            }
            for s in sched_rows
        ]
    except Exception as e:
        logger.warning("preview schedule 조회 실패: %s", e)

    # 5. 알림 대상
    notifications = ["self"]
    if source_group_id:
        notifications.append(f"head_nurse:{source_group_id}")
    if target_group_id and target_group_id != source_group_id:
        notifications.append(f"head_nurse:{target_group_id}")

    return {
        "nurse_id": nurse_id,
        "nurse_name": nurse.name,
        "source_group_id": source_group_id,
        "target_group_id": target_group_id,
        "reason": reason,
        "start_date": start_date.isoformat(),
        "expected_end_date": expected_end_date.isoformat() if expected_end_date else None,
        "conflicts": conflicts,
        "nml_affected": nml_affected,
        "wanted_affected": wanted_affected,
        "schedules_to_check": schedules_to_check,
        "notifications": notifications,
    }


def reconcile_nurse_attrs(
    db: Session,
    *,
    as_of: Optional[date] = None,
    sample_limit: int = 0,
) -> dict:
    """야간 reconcile — Nurses 테이블 캐시와 시점-effective 값의 일관성 검증.

    read-only. 불일치 발견 시 로그·반환만, 자동 동기화 없음.
    (자동 동기화는 Phase 1.4 DDL 적용 + kind 기반 정밀화 후에 도입.)

    Args:
        as_of: 비교 기준 일자 (기본 오늘)
        sample_limit: 0이면 전체 검사, N>0이면 N명 샘플

    Returns:
        {
          "as_of": "YYYY-MM-DD",
          "total_checked": N,
          "mismatches": [{nurse_id, name, attr, nurse_value, effective_value, assignment_id}],
          "mismatch_count": M,
        }

    참조: docs/NURSE_ASSIGNMENT_CRON_DESIGN.md §1.2 reconcile_nurse_attrs.
    """
    from services.nurse_effective import (
        get_active_assignments_batch,
        _ATTR_TO_TARGET_COL,
        _is_unset_override,
    )

    if as_of is None:
        as_of = date.today()

    q = db.query(NurseModel).filter(NurseModel.active == 1)
    if sample_limit > 0:
        q = q.limit(sample_limit)
    nurses = q.all()

    ids = [n.nurse_id for n in nurses]
    cache = get_active_assignments_batch(db, ids, as_of)

    mismatches: list[dict] = []
    for n in nurses:
        asg = cache.get(n.nurse_id)
        if asg is None:
            continue  # 활성 assignment 없으면 폴백 = Nurses 값 그대로, 불일치 정의 X
        for attr, target_col in _ATTR_TO_TARGET_COL.items():
            target_val = getattr(asg, target_col, None)
            if _is_unset_override(target_val):
                continue  # target 미설정(None/[]/"")이면 폴백, Nurses와 비교 의미 없음
            nurse_val = getattr(n, attr, None)
            # 정규화 — TINYINT 0/1 ↔ bool 비교
            if isinstance(nurse_val, bool) and isinstance(target_val, int):
                cmp_val = bool(target_val)
            elif isinstance(target_val, bool) and isinstance(nurse_val, int):
                cmp_val = bool(nurse_val)
                target_val = bool(target_val)
                nurse_val = cmp_val
            else:
                cmp_val = nurse_val
            if cmp_val != target_val:
                mismatches.append({
                    "nurse_id": n.nurse_id,
                    "name": n.name,
                    "attr": attr,
                    "nurse_value": repr(nurse_val),
                    "effective_value": repr(target_val),
                    "assignment_id": asg.id,
                    "assignment_reason": asg.reason,
                })

    result = {
        "as_of": as_of.isoformat(),
        "total_checked": len(nurses),
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
    }
    if mismatches:
        logger.warning(
            "[reconcile_nurse_attrs] 불일치 %d건 발견 (총 %d명 검사). 상세: %s",
            len(mismatches), len(nurses), mismatches[:5],
        )
    return result


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


def create_permanent_change(
    db: Session,
    *,
    nurse_id: str,
    group_id: str,
    office_id: str,
    start_date: date,
    new_team_id: Optional[int] = None,
    new_grade: Optional[int] = None,
    new_shift_types: Optional[list] = None,
    release_preceptor: bool = False,
    note: Optional[str] = None,
) -> NurseAssignment:
    """병동 내 영구 속성변경(team/grade/shift_types/프리셉터십 해제) 이벤트 생성.

    존재 이벤트(파견/병동이동)가 아니라 '속성 이벤트'다. 같은 병동 내 변경이므로
    source==target==group_id 이고, overlap·transfer 검증을 거치지 않는다.
    되돌리기를 위해 payload 에 직전 값을 박아둔다.
    new_shift_types: is_night_nurse(허용 shift 목록). N전담 해제 시 [] 전달.
    release_preceptor: 프리셉티의 preceptor_id 를 비움(프리셉터십 공식 종료). 전용
      target 컬럼이 없어 payload 로 운반하고 flush 가 nurse.preceptor_id=None 적용.
    발효(start_date 도래)는 flush_pending_permanent_changes 가 처리한다.
    """
    if (new_team_id is None and new_grade is None and new_shift_types is None
            and not release_preceptor):
        raise HTTPException(
            status_code=400,
            detail="변경할 속성(team/grade/shift_types/프리셉터십 해제)이 없습니다.",
        )
    nurse = (
        db.query(NurseModel).filter(NurseModel.nurse_id == nurse_id).first()
    )
    if not nurse:
        raise HTTPException(status_code=404, detail=f"간호사를 찾을 수 없습니다. (nurse_id={nurse_id})")

    payload = {
        "prev_team_id": nurse.team_id,
        "prev_grade": nurse.grade,
        "prev_shift_types": nurse.is_night_nurse,
    }
    if release_preceptor:
        payload["release_preceptor"] = True
        payload["prev_preceptor_id"] = nurse.preceptor_id

    row = NurseAssignment(
        nurse_id=nurse_id,
        source_group_id=group_id,
        target_group_id=group_id,  # 병동 내 → source==target
        office_id=office_id,
        start_date=start_date,
        reason="속성변경",
        kind="permanent_change",
        status="active",
        target_team_id=new_team_id,
        target_grade=new_grade,
        target_shift_types=new_shift_types,
        payload=payload,
        note=note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # [팀 SSOT] 속성변경의 팀 변경을 nurse_team_period 에 일원화 기록(발효 전이어도 resolve_team/
    #   생성기가 즉시 반영). 외부 호출자(team_classify/ward_redistribute)도 동일 호출을 하지만
    #   valid_from 동일 → 멱등. 어느 경로로 만들든 period 가 보장되도록 팩토리 안에서 기록한다.
    if new_team_id is not None:
        from services.team_period import set_team_period
        set_team_period(
            db, nurse_id=nurse_id, group_id=group_id, valid_from=start_date,
            team_id=new_team_id, source="permanent_change", note=note,
        )

    logger.info(
        "속성변경 등록: nurse_id=%s, team %s→%s, grade %s→%s, 발효=%s",
        nurse_id, nurse.team_id, new_team_id, nurse.grade, new_grade, start_date,
    )
    return row


def flush_pending_permanent_changes(db: Session, as_of: Optional[date] = None) -> int:
    """영구 속성변경 발효 (스케줄러용).

    조건: kind='permanent_change' AND status='active' AND start_date <= today
    처리: Nurse.team_id/grade ← target_* (None 인 속성은 미변경), status='completed', end_date=today.
    Returns: 처리된 건수
    """
    today = as_of or date.today()
    rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.kind == "permanent_change",
            NurseAssignment.status == "active",
            NurseAssignment.start_date <= today,
        )
        .all()
    )
    if not rows:
        return 0

    count = 0
    for row in rows:
        nurse = (
            db.query(NurseModel).filter(NurseModel.nurse_id == row.nurse_id).first()
        )
        if nurse:
            # [팀 절연] team 은 create_permanent_change 시점에 nurse_team_period(SSOT)에
            #   이미 기록됨. nurses.team_id 캐시는 더 이상 쓰지 않는다(컬럼 DROP 예정).
            if row.target_grade is not None:
                nurse.grade = row.target_grade
            # target_shift_types 지정 시 is_night_nurse 갱신 (N전담 해제 = [] 등)
            if row.target_shift_types is not None:
                nurse.is_night_nurse = row.target_shift_types
            # payload.release_preceptor → 프리셉터십 공식 종료 (preceptor_id 비움)
            if (row.payload or {}).get("release_preceptor"):
                nurse.preceptor_id = None
        row.status = "completed"
        row.end_date = today
        count += 1

    db.commit()
    logger.info("[Scheduler] 영구 속성변경 발효 %d건", count)
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
