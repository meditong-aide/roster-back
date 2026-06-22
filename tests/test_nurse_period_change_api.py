# tests/test_nurse_period_change_api.py
"""P7 — POST /nurse-period/change 엔드포인트 (속성 시점 변경, INSERT는 API로).

교육 D→DE 타임라인을 API 두 번으로 구성 + close-before-open + 단방향 투영 검증.
참조: app/routers/nurse_period.py.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.client2 import get_db
from db.models import Office, Group, Nurse, NurseAllowedShiftPeriod
from routers.auth import get_current_user_from_cookie
from routers.nurse_period import router as nurse_period_router
from schemas.auth_schema import User as UserSchema
from services.nurse_period_resolver import fetch_periods, resolve_asof

MS, ME = date(2026, 7, 1), date(2026, 8, 1)


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
    db.add(Nurse(nurse_id="edu", account_id="acc_edu", group_id="A", office_id="o1",
                 name="교육", active=1, is_night_nurse=[]))
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


def test_change_builds_education_timeline(seeded):
    db = seeded
    c = _client(db, _user())

    # 1) 7/1 부터 D만
    r1 = c.post("/nurse-period/change", json={
        "attribute": "allowed_shifts", "nurse_id": "edu",
        "valid_from": "2026-07-01", "value": ["D"]})
    assert r1.status_code == 200, r1.text

    # 2) 7/22 부터 D,E (close-before-open 으로 첫 구간 닫힘)
    r2 = c.post("/nurse-period/change", json={
        "attribute": "allowed_shifts", "nurse_id": "edu",
        "valid_from": "2026-07-22", "value": ["D", "E"]})
    assert r2.status_code == 200, r2.text

    # 구간 2개, 겹침 없음
    rows = db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="edu") \
             .order_by(NurseAllowedShiftPeriod.valid_from).all()
    assert len(rows) == 2
    assert rows[0].valid_from == date(2026, 7, 1) and rows[0].valid_to == date(2026, 7, 22)
    assert rows[1].valid_from == date(2026, 7, 22) and rows[1].valid_to is None

    # as-of 해석: 7/21=D만, 7/22=D·E
    by = fetch_periods(db, NurseAllowedShiftPeriod, ["edu"], MS, ME)
    assert resolve_asof(by["edu"], date(2026, 7, 21), "allowed_shifts") == ["D"]
    assert resolve_asof(by["edu"], date(2026, 7, 22), "allowed_shifts") == ["D", "E"]


def test_change_future_does_not_touch_cache(seeded):
    # valid_from(미래) > today → 캐시(is_night_nurse) 투영 안 함
    db = seeded
    c = _client(db, _user())
    r = c.post("/nurse-period/change", json={
        "attribute": "allowed_shifts", "nurse_id": "edu",
        "valid_from": "2026-07-01", "value": ["N"]})
    assert r.status_code == 200
    assert db.query(Nurse).filter_by(nurse_id="edu").first().is_night_nurse == []  # 캐시 그대로


def test_change_past_projects_cache(seeded):
    # valid_from(과거) <= today → 캐시 투영됨
    db = seeded
    c = _client(db, _user())
    r = c.post("/nurse-period/change", json={
        "attribute": "allowed_shifts", "nurse_id": "edu",
        "valid_from": "2020-01-01", "value": ["N"]})
    assert r.status_code == 200
    assert r.json()["today_value"] == ["N"]
    assert db.query(Nurse).filter_by(nurse_id="edu").first().is_night_nurse == ["N"]


def test_change_unknown_attribute_400(seeded):
    c = _client(seeded, _user())
    r = c.post("/nurse-period/change", json={
        "attribute": "bogus", "nurse_id": "edu", "valid_from": "2026-07-01", "value": []})
    assert r.status_code == 400


def test_change_missing_nurse_404(seeded):
    c = _client(seeded, _user())
    r = c.post("/nurse-period/change", json={
        "attribute": "allowed_shifts", "nurse_id": "ghost",
        "valid_from": "2026-07-01", "value": ["D"]})
    assert r.status_code == 404
