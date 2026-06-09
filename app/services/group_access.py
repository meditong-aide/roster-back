"""현재 사용자가 관리 가능한 group_id 목록을 결정한다.

권한 분기:
- ADM(is_master_admin): 같은 office_id 의 모든 group
- HN(hn_auth=='HN'): group.hn_id JSON 리스트에 본인 nurse_id 포함된 group + home group
- 그 외(수간호사·일반 간호사, HN 권한 없음): 토큰 group_id 1개

home group 은 JWT 의 group_id 가 switch 로 바뀌어 있을 수 있으므로
nurses 테이블의 실제 소속 group_id 를 우선한다 (routers/groups.py 의
my-admin-groups 패턴과 동일).
"""

from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from db.models import Group as GroupModel
from db.models import Nurse as NurseModel
from db.models import NurseAssignment as NurseAssignmentModel
from schemas.auth_schema import User as UserSchema


def resolve_home_group_id(db: Session, current_user: UserSchema) -> Optional[str]:
    """nurse_id 기준 실제 소속 group_id (DB nurses 테이블). 토큰 group_id 는 무시한다.

    이렇게 해야 일반 간호사의 group_id 가 바뀌어도 토큰 재발행 없이 즉시 반영된다.
    nurse 레코드가 없으면(ADM 등) 토큰 group_id 로 폴백한다.
    """
    if current_user is None:
        return None
    nurse = (
        db.query(NurseModel)
        .filter(NurseModel.nurse_id == current_user.nurse_id)
        .first()
    )
    return (nurse.group_id if nurse and nurse.group_id else current_user.group_id) or None


def _caller_nurse(db: Session, current_user: UserSchema) -> Optional[NurseModel]:
    """호출자의 nurses 행(account_id 기준). 없으면 None(ADM 등)."""
    if current_user is None or not getattr(current_user, "account_id", None):
        return None
    return (
        db.query(NurseModel)
        .filter(NurseModel.account_id == current_user.account_id)
        .first()
    )


def caller_is_head_nurse(db: Session, current_user: UserSchema) -> bool:
    """수간호사 여부 — 토큰 대신 nurses(DB)에서 판정. ADM 은 nurse 행이 없어 False.

    토큰 is_head_nurse 는 승급/강등 시 재발행 전까지 stale 하므로 DB 를 진실로 삼는다.
    """
    n = _caller_nurse(db, current_user)
    return bool(n.is_head_nurse) if n else False


def caller_is_hn(db: Session, current_user: UserSchema) -> bool:
    """그룹관리자(HN) 여부 — 토큰 대신 nurses(DB).hn_auth 로 판정."""
    n = _caller_nurse(db, current_user)
    return bool(n) and str(n.hn_auth or "").upper() == "HN"


def caller_is_manager(db: Session, current_user: UserSchema) -> bool:
    """관리 권한 보유 여부: ADM(토큰 EmpAuthGbn) 또는 수간호사/그룹관리자(DB).

    ADM 판정만 토큰을 쓴다(권위가 외부 mWorks 이고 인앱 그룹전환으로 stale 되지 않음).
    수간호사/HN 은 DB 기준 — 토큰 재발행 없이 즉시 반영.
    """
    if current_user is not None and bool(current_user.is_master_admin):
        return True
    n = _caller_nurse(db, current_user)
    if n is None:
        return False
    return bool(n.is_head_nurse) or str(n.hn_auth or "").upper() == "HN"


def resolve_managed_group_ids(db: Session, current_user: UserSchema) -> List[str]:
    """관리 가능한 group_id 리스트 (office 내부, 중복 제거).

    권한이 없거나 office_id/group_id 가 비어 있으면 빈 리스트를 반환한다.
    호출 측에서 비어 있을 때 어떻게 응답할지 결정한다.
    """

    if current_user is None or not current_user.office_id:
        return []

    is_admin = bool(current_user.is_master_admin)
    is_hn = caller_is_hn(db, current_user)  # 토큰 대신 DB hn_auth

    rows = (
        db.query(GroupModel)
        .filter(GroupModel.office_id == current_user.office_id)
        .all()
    )

    if is_admin:
        return [str(g.group_id) for g in rows]

    if is_hn:
        home_group_id = resolve_home_group_id(db, current_user)

        managed: List[str] = []
        seen: set = set()

        if home_group_id:
            for g in rows:
                if g.group_id == home_group_id:
                    managed.append(str(g.group_id))
                    seen.add(g.group_id)
                    break

        for g in rows:
            if g.group_id in seen:
                continue
            hn_ids = g.hn_id or []
            if current_user.nurse_id in hn_ids:
                managed.append(str(g.group_id))
                seen.add(g.group_id)

        return managed

    home = resolve_home_group_id(db, current_user)
    return [str(home)] if home else []


def _has_active_inbound_assignment(
    db: Session,
    nurse_id: str,
    group_id: str,
    window: Optional[Tuple[int, int]] = None,
) -> bool:
    """본인이 해당 group 으로 파견/병동이동(active) 중인지. window=(year,month) 가 주어지면
    조회월 ±6일 버퍼와 파견기간이 겹치는지까지 확인한다(/issued_roster 의 기존 로직과 동일)."""
    from sqlalchemy import or_ as _or_, case as _case

    q = db.query(NurseAssignmentModel).filter(
        NurseAssignmentModel.nurse_id == nurse_id,
        NurseAssignmentModel.target_group_id == group_id,
        NurseAssignmentModel.reason.in_(["파견", "병동이동"]),
        NurseAssignmentModel.status == "active",
    )
    if window is not None:
        from datetime import date as _date, timedelta as _td
        from calendar import monthrange as _mr

        year, month = window
        _lookback = 6
        _m_start = _date(year, month, 1)
        _m_end = _date(year, month, _mr(year, month)[1])
        _view_start = _m_start - _td(days=_lookback)
        _view_end = _m_end + _td(days=_lookback)
        _eff_end = _case(
            (NurseAssignmentModel.end_date.isnot(None), NurseAssignmentModel.end_date),
            else_=NurseAssignmentModel.expected_end_date,
        )
        q = q.filter(
            NurseAssignmentModel.start_date <= _view_end,
            _or_(_eff_end.is_(None), _eff_end >= _view_start),
        )
    return q.first() is not None


def resolve_effective_group(
    db: Session,
    current_user: UserSchema,
    requested_group_id: Optional[str] = None,
    *,
    require_group: bool = True,
    allow_assignment_target: bool = False,
    assignment_window: Optional[Tuple[int, int]] = None,
) -> Optional[str]:
    """그룹 스코프 단일 해석점. 토큰 group_id 에 의존하지 않는다(nurse_id→DB + groups.hn_id).

    - ADM: requested 가 있으면 office 내 그룹인지 검증 후 반환. 없으면 require_group=True
      면 400, False 면 None(office-wide — 호출 측이 전체 조회로 해석).
    - HN/일반: requested(없으면 DB home) 가 관리 가능 그룹(HN=hn_id+home / 일반=home)에
      속하면 반환.
    - allow_assignment_target=True 면, 관리 그룹이 아니어도 본인이 해당 그룹으로
      파견/병동이동(active) 중이면 허용(/issued_roster 병동전환 Select 용).

    Raises:
        HTTPException(401) 인증 없음 / (400) 그룹 결정 불가 / (403) 권한 없음.
    """
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    managed = resolve_managed_group_ids(db, current_user)

    # ADM: office 내 임의 그룹 지정 가능. 미지정 시 office-wide(None) 또는 400.
    if bool(current_user.is_master_admin):
        if requested_group_id:
            if str(requested_group_id) not in managed:
                raise HTTPException(
                    status_code=403, detail="해당 그룹에 대한 접근 권한이 없습니다."
                )
            return str(requested_group_id)
        if require_group:
            raise HTTPException(status_code=400, detail="group_id가 필요합니다.")
        return None

    # HN/일반: requested 우선, 없으면 DB home.
    target = requested_group_id or resolve_home_group_id(db, current_user)
    if not target:
        raise HTTPException(status_code=400, detail="group_id를 결정할 수 없습니다.")

    if str(target) in managed:
        return str(target)

    if allow_assignment_target and _has_active_inbound_assignment(
        db, current_user.nurse_id, target, assignment_window
    ):
        return str(target)

    raise HTTPException(status_code=403, detail="해당 그룹에 대한 접근 권한이 없습니다.")
