# tests/test_nurse_period_roll_api.py
"""P7 — POST /nurse-period/roll : as_of 기준 period → nurses 캐시 단방향 투영.

미래발효 변경이 발효일에 캐시에 반영되는지(일일 cron 역할) 검증.
참조: app/routers/nurse_period.py.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.client2 import get_db
from db.models import (
    Office, Group, Nurse, NurseWeekendOffPeriod, NurseGradePeriod,
)
from routers.auth import get_current_user_from_cookie
from routers.nurse_period import router as nurse_period_router
from schemas.auth_schema import User as UserSchema


def _user(group_id="A"):
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id="o1", group_id=group_id,
        is_head_nurse=True, is_master_admin=False, name="수간", EmpSeqNo="",
        EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True, hn_auth="HN",
        original_group_id=group_id, gw_useYN="Y", qpis_useYN="Y",
    )


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="HN", account_id="acc_HN", group_id="A", office_id="o1",
                 name="수간", active=0, is_head_nurse=True, hn_auth="HN", is_night_nurse=[]))
    db.add(Nurse(nurse_id="w1", account_id="acc_w1", group_id="A", office_id="o1",
                 name="w1", active=1, is_weekend_off=False, grade=2, is_night_nurse=[]))
    db.flush()
    return db


def _client(db, user):
    app = FastAPI()
    app.include_router(nurse_period_router)

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_from_cookie] = lambda: user
    return TestClient(app)


def test_roll_applies_effective_change_on_date(seeded):
    db = seeded
    # 9/1 부터 weekend_off=1 (미래발효)
    db.add(NurseWeekendOffPeriod(nurse_id="w1", valid_from=date(2026, 9, 1),
           valid_to=None, weekend_off=1))
    db.flush()
    c = _client(db, _user())

    # as_of 8/31 → 아직 발효 전(gap) → 캐시 그대로
    r0 = c.post("/nurse-period/roll", json={"group_id": "A", "as_of": "2026-08-31",
                                            "attributes": ["weekend_off"]})
    assert r0.status_code == 200
    assert r0.json()["updated"]["weekend_off"] == 0
    assert db.query(Nurse).filter_by(nurse_id="w1").first().is_weekend_off in (False, 0)

    # as_of 9/1 → 발효 → 캐시 투영
    r1 = c.post("/nurse-period/roll", json={"group_id": "A", "as_of": "2026-09-01",
                                            "attributes": ["weekend_off"]})
    assert r1.status_code == 200
    assert r1.json()["updated"]["weekend_off"] == 1
    assert db.query(Nurse).filter_by(nurse_id="w1").first().is_weekend_off == 1


def test_roll_idempotent(seeded):
    db = seeded
    db.add(NurseGradePeriod(nurse_id="w1", group_id="A", valid_from=date(2026, 1, 1),
           valid_to=None, grade=5))
    db.flush()
    c = _client(db, _user())
    body = {"group_id": "A", "as_of": "2026-09-01", "attributes": ["grade"]}
    r1 = c.post("/nurse-period/roll", json=body)
    assert r1.json()["updated"]["grade"] == 1          # 2→5 변경
    r2 = c.post("/nurse-period/roll", json=body)
    assert r2.json()["updated"]["grade"] == 0          # 이미 5 → no-op(멱등)


def test_roll_foreign_group_403(seeded):
    c = _client(seeded, _user(group_id="A"))
    r = c.post("/nurse-period/roll", json={"group_id": "OTHER"})
    assert r.status_code == 403
