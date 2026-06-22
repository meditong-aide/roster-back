# tests/test_nurse_period_backfill_api.py
"""P2 — POST /nurse-period/backfill 엔드포인트.

현 nurses 캐시값 → period 첫 구간 시드. 멱등성 · 값 정확성 · 권한 검증.
참조: app/routers/nurse_period.py.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.client2 import get_db
from db.models import (
    Office, Group, Nurse,
    NurseAllowedShiftPeriod, NurseWeekendOffPeriod, NurseFixedShiftPeriod, NurseGradePeriod,
)
from routers.auth import get_current_user_from_cookie
from routers.nurse_period import router as nurse_period_router
from schemas.auth_schema import User as UserSchema
from services.nurse_period_resolver import fetch_periods, resolve_asof

VF = date(2026, 7, 1)
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
    # 권한 게이트용 호출자 행(분류 풀과 무관하게 active=0)
    db.add(Nurse(nurse_id="HN", account_id="acc_HN", group_id="A", office_id="o1",
                 name="수간", active=0, is_head_nurse=True, hn_auth="HN", is_night_nurse=[]))
    # 대상 간호사 3명 — 서로 다른 속성값
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, grade=1, is_night_nurse=["N"], is_weekend_off=True,
                 fixed_shift=None))
    db.add(Nurse(nurse_id="n2", account_id="acc_n2", group_id="A", office_id="o1", name="n2",
                 active=1, grade=2, is_night_nurse=[], is_weekend_off=False,
                 fixed_shift="D_A"))
    # active=0 은 제외돼야 함
    db.add(Nurse(nurse_id="nx", account_id="acc_nx", group_id="A", office_id="o1", name="nx",
                 active=0, grade=3, is_night_nurse=[], is_weekend_off=False))
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


def test_backfill_seeds_period_rows(seeded):
    db = seeded
    c = _client(db, _user())
    r = c.post("/nurse-period/backfill", json={"group_id": "A", "valid_from": "2026-07-01"})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["nurse_count"] == 2                     # active=1 인 n1, n2 만 (nx 제외)
    assert body["rows"] == {"allowed_shifts": 2, "weekend_off": 2,
                            "fixed_shift": 2, "grade": 2}

    # 값 정확성: as-of 해석이 현재값과 일치
    by_as = fetch_periods(db, NurseAllowedShiftPeriod, ["n1", "n2"], MS, ME)
    assert resolve_asof(by_as["n1"], date(2026, 7, 10), "allowed_shifts") == ["N"]  # N전담
    assert resolve_asof(by_as["n2"], date(2026, 7, 10), "allowed_shifts") == []      # 제한 없음

    by_wo = fetch_periods(db, NurseWeekendOffPeriod, ["n1", "n2"], MS, ME)
    assert resolve_asof(by_wo["n1"], date(2026, 7, 10), "weekend_off") == 1
    assert resolve_asof(by_wo["n2"], date(2026, 7, 10), "weekend_off") == 0

    by_fx = fetch_periods(db, NurseFixedShiftPeriod, ["n1", "n2"], MS, ME)
    assert resolve_asof(by_fx["n2"], date(2026, 7, 10), "fixed_shift") == "D_A"

    by_gr = fetch_periods(db, NurseGradePeriod, ["n1", "n2"], MS, ME, group_id="A")
    assert resolve_asof(by_gr["n1"], date(2026, 7, 10), "grade") == 1


def test_backfill_idempotent(seeded):
    db = seeded
    c = _client(db, _user())
    body = {"group_id": "A", "valid_from": "2026-07-01"}
    r1 = c.post("/nurse-period/backfill", json=body); assert r1.status_code == 200
    r2 = c.post("/nurse-period/backfill", json=body); assert r2.status_code == 200
    # 재호출해도 row 수 그대로(멱등) — 동일값 upsert no-op
    assert r2.json()["rows"]["allowed_shifts"] == 2
    assert db.query(NurseAllowedShiftPeriod).filter(
        NurseAllowedShiftPeriod.nurse_id.in_(["n1", "n2"])).count() == 2


def test_backfill_subset_attributes(seeded):
    db = seeded
    c = _client(db, _user())
    r = c.post("/nurse-period/backfill",
               json={"group_id": "A", "valid_from": "2026-07-01", "attributes": ["grade"]})
    assert r.status_code == 200
    assert set(r.json()["rows"].keys()) == {"grade"}
    assert db.query(NurseAllowedShiftPeriod).count() == 0   # 다른 속성 안 건드림


def test_backfill_unknown_attribute_400(seeded):
    c = _client(seeded, _user())
    r = c.post("/nurse-period/backfill", json={"group_id": "A", "attributes": ["bogus"]})
    assert r.status_code == 400


def test_backfill_foreign_group_403(seeded):
    c = _client(seeded, _user(group_id="A"))
    r = c.post("/nurse-period/backfill", json={"group_id": "OTHER"})
    assert r.status_code == 403
