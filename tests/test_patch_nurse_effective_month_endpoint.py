"""PATCH /nurses/{id}?year=&month= 가 서비스까지 valid_from 으로 닿는지 end-to-end 고정.

엔드포인트 쿼리(year/month) → update_nurse_profile_service(effective_*) → period valid_from.
프론트 전송 의심을 백엔드측에서 분리(백엔드가 받으면 정확히 동작함을 증명).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.client2 import get_db
from db.models import Office, Group, Nurse, NurseWeekendOffPeriod
from routers.auth import get_current_user_from_cookie
from routers.nurses import router as nurses_router
from schemas.auth_schema import User as UserSchema
from services.nurse_period_resolver import fetch_periods, resolve_asof


def _admin():
    return UserSchema(
        nurse_id="ADM", account_id="acc_ADM", office_id="o1", group_id="A",
        is_head_nurse=False, is_master_admin=True, name="관리자", EmpSeqNo="",
        EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True, hn_auth=None,
        original_group_id="A", gw_useYN="Y", qpis_useYN="Y",
    )


@pytest.fixture
def client(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, is_weekend_off=False, allowed_shifts=[], grade=1))
    db.flush()
    app = FastAPI()
    app.include_router(nurses_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_from_cookie] = lambda: _admin()
    return TestClient(app), db


def _wk(db, day):
    rows = fetch_periods(db, NurseWeekendOffPeriod, ["n1"], day, day + timedelta(days=1))
    return resolve_asof(rows.get("n1"), day, "weekend_off")


def test_patch_query_year_month_reaches_period(client):
    c, db = client
    # 6월 ON
    r1 = c.patch("/nurses/n1?group_id=A&year=2026&month=6", json={"is_weekend_off": True})
    assert r1.status_code == 200, r1.text
    assert _wk(db, date(2026, 6, 15)) == 1
    # 8월 OFF — 쿼리 month=8 이 valid_from=8/1 로 닿으면 close-before-open
    r2 = c.patch("/nurses/n1?group_id=A&year=2026&month=8", json={"is_weekend_off": False})
    assert r2.status_code == 200, r2.text
    assert _wk(db, date(2026, 6, 15)) == 1   # 6월 유지
    assert _wk(db, date(2026, 7, 15)) == 1   # 7월 유지
    assert _wk(db, date(2026, 8, 15)) == 0   # 8월부터 OFF
