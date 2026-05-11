"""현재 사용자가 관리 가능한 group_id 목록을 결정한다.

권한 분기:
- ADM(is_master_admin): 같은 office_id 의 모든 group
- HN(hn_auth=='HN'): group.hn_id JSON 리스트에 본인 nurse_id 포함된 group + home group
- 그 외(수간호사·일반 간호사, HN 권한 없음): 토큰 group_id 1개

home group 은 JWT 의 group_id 가 switch 로 바뀌어 있을 수 있으므로
nurses 테이블의 실제 소속 group_id 를 우선한다 (routers/groups.py 의
my-admin-groups 패턴과 동일).
"""

from typing import List

from sqlalchemy.orm import Session

from db.models import Group as GroupModel
from db.models import Nurse as NurseModel
from schemas.auth_schema import User as UserSchema


def resolve_managed_group_ids(db: Session, current_user: UserSchema) -> List[str]:
    """관리 가능한 group_id 리스트 (office 내부, 중복 제거).

    권한이 없거나 office_id/group_id 가 비어 있으면 빈 리스트를 반환한다.
    호출 측에서 비어 있을 때 어떻게 응답할지 결정한다.
    """

    if current_user is None or not current_user.office_id:
        return []

    is_admin = bool(current_user.is_master_admin)
    is_hn = str(current_user.hn_auth or "").upper() == "HN"

    rows = (
        db.query(GroupModel)
        .filter(GroupModel.office_id == current_user.office_id)
        .all()
    )

    if is_admin:
        return [str(g.group_id) for g in rows]

    if is_hn:
        nurse = (
            db.query(NurseModel)
            .filter(NurseModel.nurse_id == current_user.nurse_id)
            .first()
        )
        home_group_id = (nurse.group_id if nurse else current_user.group_id) or None

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

    return [str(current_user.group_id)] if current_user.group_id else []
