"""근무표 생성 진입부의 그룹 스코프 해석 — 토큰 group_id 대신 req.group_id(없으면 DB home).

generate_roster_service 는 게이트 통과 후 resolve_effective_group 로 대상 그룹을 정하고
current_user.group_id 에 되쓴다(이후 ~15곳의 current_user.group_id 참조가 검증된 그룹을 가리킴).
솔버까지 가지 않고 ① 권한 거부(403) ② wanted 미작성(그룹 해석 성공 증거) 에서 조기 종료하므로
무거운 솔버 없이 해석/검증만 검증한다. 참조: app/services/roster_create_service.py.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from db.models import Group, Nurse, Office
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import RosterRequest
from services.roster_create_service import generate_roster_service


def _user(*, group_id, is_head_nurse=True, hn_auth=None):
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id="o1", group_id=group_id,
        is_head_nurse=is_head_nurse, is_master_admin=False, name="수간",
        EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True,
        hn_auth=hn_auth, original_group_id=group_id, gw_useYN="Y", qpis_useYN="Y",
    )


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add(Group(group_id="OTHER", group_name="타병동", office_id="o1"))
    # 호출자: DB home = A 인 수간호사
    db.add(Nurse(nurse_id="HN", account_id="acc_HN", group_id="A", office_id="o1",
                 name="수간", active=1, is_head_nurse=True, allowed_shifts=[]))
    db.flush()
    return db


def test_resolves_db_home_ignoring_stale_token(seeded):
    """token group_id 가 stale 해도 req.group_id 미지정이면 DB home(A) 으로 해석된다."""
    db = seeded
    user = _user(group_id="STALE")  # 토큰은 낡은 값
    req = RosterRequest(year=2026, month=8)  # group_id 미지정

    with pytest.raises(Exception) as ei:
        generate_roster_service(req, user, db)

    # wanted 미작성에서 멈춤 = 그룹 해석/권한은 통과했다는 증거
    assert "wanted" in str(ei.value)
    # 토큰 STALE 이 아니라 DB home A 로 해석되어 되써졌다
    assert user.group_id == "A"


def test_rejects_unmanaged_group(seeded):
    """관리하지 않는 그룹을 req.group_id 로 지정하면 403."""
    db = seeded
    user = _user(group_id="A")
    req = RosterRequest(year=2026, month=8, group_id="OTHER")

    with pytest.raises(HTTPException) as ei:
        generate_roster_service(req, user, db)
    assert ei.value.status_code == 403


def test_hn_can_target_managed_non_home_group(seeded):
    """그룹관리자(HN)는 home(A) 이 아니어도 hn_id 에 등록된 B 를 대상으로 생성할 수 있다."""
    db = seeded
    # HN 권한은 토큰이 아니라 DB(nurses.hn_auth)에서 본다 — DB 행에도 부여해야 한다.
    db.query(Nurse).filter(Nurse.nurse_id == "HN").update({"hn_auth": "HN"})
    db.query(Group).filter(Group.group_id == "B").update({"hn_id": ["HN"]})
    db.flush()
    user = _user(group_id="A", hn_auth="HN")
    req = RosterRequest(year=2026, month=8, group_id="B")

    with pytest.raises(Exception) as ei:
        generate_roster_service(req, user, db)
    assert "wanted" in str(ei.value)      # B 로 해석 성공 → wanted 단계에서 멈춤
    assert user.group_id == "B"            # 선택한 관리 그룹으로 되써짐
