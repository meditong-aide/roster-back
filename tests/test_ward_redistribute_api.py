"""병동 간 재분배 엔드포인트 + 권한 — POST /teams/redistribute/preview · /apply.

참조: app/routers/teams.py, app/services/ward_redistribute_service.py.
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


def _user(*, group_id="A", is_head_nurse=True, is_master_admin=False):
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id="o1", group_id=group_id,
        is_head_nurse=is_head_nurse, is_master_admin=is_master_admin, name="수간",
        EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True,
        hn_auth="HN" if is_head_nurse else None, original_group_id=group_id,
        gw_useYN="Y", qpis_useYN="Y",
    )


def _mk_nurse(db, nid, gid, grade):
    db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id=gid, office_id="o1",
                 name=nid, active=1, team_id=1, grade=grade, is_night_nurse=[]))


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1팀"))
    db.add(Team(office_id="o1", group_id="B", team_id=1, team_name="1팀"))
    _mk_nurse(db, "a_g1", "A", 1)
    for i in range(4):
        _mk_nurse(db, f"a{i}", "A", 2)
    _mk_nurse(db, "b_g1", "B", 1)
    for i in range(4):
        _mk_nurse(db, f"b{i}", "B", 2)
    db.flush()
    return db


def _client(db, user):
    app = FastAPI()
    app.include_router(teams_router)

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_from_cookie] = lambda: user
    return TestClient(app)


def test_redistribute_preview_ok(seeded):
    db = seeded
    c = _client(db, _user(is_master_admin=True))  # admin → office 전체 관리
    r = c.post("/teams/redistribute/preview",
               json={"group_ids": ["A", "B"], "year": 2026, "month": 7})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["num_wards"] == 2 and b["num_pool"] == 10
    assert set(b["wards"].keys()) == {"A", "B"}
    assert db.query(NurseAssignment).count() == 0  # read-only


def test_preview_422_when_ward_missing_g1(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    _mk_nurse(db, "a_g1", "A", 1)
    for i in range(4):
        _mk_nurse(db, f"a{i}", "A", 2)
    for i in range(5):
        _mk_nurse(db, f"b{i}", "B", 2)   # B: G1 없음
    db.flush()
    c = _client(db, _user(is_master_admin=True))
    r = c.post("/teams/redistribute/preview",
               json={"group_ids": ["A", "B"], "year": 2026, "month": 7})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert any(w["group_id"] == "B" for w in detail["needs_g1_setup"])


def test_redistribute_apply_creates_events(seeded):
    db = seeded
    c = _client(db, _user(is_master_admin=True))
    r = c.post("/teams/redistribute/apply", json={
        "group_ids": ["A", "B"], "year": 2026, "month": 7,
        "assignments": [
            {"nurse_id": "a0", "to_group_id": "B", "team_id": 1},
            {"nurse_id": "a1", "to_group_id": "A", "team_id": 1},  # 동일 → skip
        ],
    })
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["transfers"] == 1 and res["effective_date"] == "2026-07-01"


def test_unmanaged_group_forbidden(seeded):
    db = seeded
    # home=A 수간호사(비admin) → A만 관리. B 포함 재분배 → 403
    c = _client(db, _user(group_id="A", is_master_admin=False))
    r = c.post("/teams/redistribute/preview",
               json={"group_ids": ["A", "B"], "year": 2026, "month": 7})
    assert r.status_code == 403


def test_non_manager_forbidden(seeded):
    db = seeded
    c = _client(db, _user(is_head_nurse=False, is_master_admin=False))
    r = c.post("/teams/redistribute/preview",
               json={"group_ids": ["A", "B"], "year": 2026, "month": 7})
    assert r.status_code == 403
