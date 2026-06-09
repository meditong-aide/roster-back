"""팀 분류 엔드포인트 + 권한 — POST /teams/classify/preview · /apply.

참조: app/routers/teams.py, app/services/team_classify_service.py.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.client2 import get_db
from db.models import (
    FixedWantedEntry, Group, Nurse, NurseAssignment, Office, Shift, Team,
)
from routers.auth import get_current_user_from_cookie
from routers.teams import router as teams_router
from schemas.auth_schema import User as UserSchema


def _user(*, group_id="A", office_id="o1", is_head_nurse=True, is_master_admin=False):
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id=office_id, group_id=group_id,
        is_head_nurse=is_head_nurse, is_master_admin=is_master_admin, name="수간",
        EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True,
        hn_auth="HN" if is_head_nurse else None, original_group_id=group_id,
        gw_useYN="Y", qpis_useYN="Y",
    )


def _mk_nurse(db, nid, team_id, grade):
    db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id="A", office_id="o1",
                 name=nid, active=1, team_id=team_id, grade=grade, is_night_nurse=[]))


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1팀"))
    db.add(Team(office_id="o1", group_id="A", team_id=2, team_name="2팀"))
    db.add(Shift(shift_id="V", id=1, office_id="o1", group_id="A", name="연차",
                 color="#fff", type="휴가"))
    _mk_nurse(db, "g1a", 1, 1)
    _mk_nurse(db, "g1b", 2, 1)
    for nid, tid, g in [("n1", 1, 2), ("n2", 1, 2), ("n3", 1, 3),
                        ("n4", 2, 2), ("n5", 2, 2), ("n6", 2, 3)]:
        _mk_nurse(db, nid, tid, g)
    for nid, day in [("n1", 5), ("n2", 5), ("n4", 20), ("n5", 20)]:
        db.add(FixedWantedEntry(group_id="A", year=2026, month=8, nurse_id=nid,
                                shift_date=date(2026, 8, day), shift_id="O",
                                source_type="original"))
    db.flush()
    return db


def _client(db, user):
    # 권한 게이트가 토큰 대신 nurses(DB)를 보므로 호출자 nurse 행을 시드한다.
    # (분류 풀에 섞이지 않도록 active=0; 권한 판정은 active 무관)
    if user.account_id and not db.query(Nurse).filter(
        Nurse.account_id == user.account_id
    ).first():
        db.add(Nurse(
            nurse_id=user.nurse_id, account_id=user.account_id,
            group_id=user.group_id, office_id=user.office_id, name=user.name,
            active=0, is_head_nurse=bool(user.is_head_nurse),
            hn_auth=user.hn_auth, is_night_nurse=[],
        ))
        db.flush()

    app = FastAPI()
    app.include_router(teams_router)

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_from_cookie] = lambda: user
    return TestClient(app)


def test_preview_endpoint_readonly(seeded):
    db = seeded
    c = _client(db, _user())
    r = c.post("/teams/classify/preview", json={"year": 2026, "month": 8})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_month"] == "2026-08"
    assert body["num_teams"] == 2 and body["num_pool"] == 8
    assert db.query(NurseAssignment).count() == 0  # read-only


def test_apply_endpoint_creates_events(seeded):
    db = seeded
    c = _client(db, _user())
    pv = c.post("/teams/classify/preview", json={"year": 2026, "month": 8}).json()
    flat = [{"nurse_id": nid, "team_id": int(t)}
            for t, members in pv["teams"].items() for nid in members]
    r = c.post("/teams/classify/apply",
               json={"year": 2026, "month": 8, "assignments": flat})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["effective_date"] == "2026-08-01"
    assert res["created"] == pv["num_changed"]
    assert db.query(NurseAssignment).count() == res["created"]


def test_apply_forbidden_for_non_manager(seeded):
    db = seeded
    # 수간호사도 admin도 아니고 hn_auth 도 없음 → 관리자 아님 → 403
    c = _client(db, _user(is_head_nurse=False, is_master_admin=False))
    r = c.post("/teams/classify/apply",
               json={"year": 2026, "month": 8,
                     "assignments": [{"nurse_id": "n1", "team_id": 2}]})
    assert r.status_code == 403


def test_forbidden_for_unmanaged_group(seeded):
    db = seeded
    # home=A 수간호사가 관리하지 않는 다른 그룹 지정 → 403
    c = _client(db, _user(group_id="A"))
    r = c.post("/teams/classify/preview",
               json={"year": 2026, "month": 8, "group_id": "OTHER"})
    assert r.status_code == 403


def test_group_manager_can_classify_via_hn_id(seeded):
    """home 이 A가 아니어도, Group.hn_id 에 등록된 그룹관리자는 그 그룹을 분류할 수 있다."""
    db = seeded
    db.query(Group).filter(Group.group_id == "A").update({"hn_id": ["HN"]})
    db.flush()
    # 토큰 group_id 는 존재하지 않는 HOME — 관리권은 hn_id 로 A 에 부여됨
    c = _client(db, _user(group_id="HOME"))
    r = c.post("/teams/classify/preview", json={"year": 2026, "month": 8})
    assert r.status_code == 200, r.text
    assert r.json()["num_pool"] == 8  # A 병동 분류 수행됨


def test_admin_multiple_groups_requires_group_id(seeded):
    db = seeded
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.flush()
    # admin 은 office 내 전체(A,B) 관리 → 미지정이면 모호 → 400
    c = _client(db, _user(is_master_admin=True))
    r = c.post("/teams/classify/preview", json={"year": 2026, "month": 8})
    assert r.status_code == 400
