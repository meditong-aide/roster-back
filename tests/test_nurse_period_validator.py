# tests/test_nurse_period_validator.py
"""cross-attribute 저장 검증 — allowed_shifts ↔ 월한도/고정근무 모순 차단.

참조: app/services/nurse_period_validator.py, app/routers/nurse_period.py.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.client2 import get_db
from db.models import (
    Office, Group, Nurse, NurseMonthlyLimit, RosterConfig, NurseAllowedShiftPeriod,
)
from routers.auth import get_current_user_from_cookie
from routers.nurse_period import router as nurse_period_router
from schemas.auth_schema import User as UserSchema
from services.nurse_period_validator import validate_allowed_shift_period


def _codes(issues):
    return {i["reason_code"] for i in issues}


@pytest.fixture
def base(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
                 name="n1", active=1, allowed_shifts=[]))
    db.flush()
    return db


# ── 유닛 ────────────────────────────────────────────────────────────────────
def test_allowed_de_but_n_exact_blocks(base):
    db = base
    db.add(NurseMonthlyLimit(nurse_id="n1", group_id="A", year=2026, month=7, n_exact=2))
    db.flush()
    issues = validate_allowed_shift_period(db, "n1", "A", ["D", "E"], date(2026, 7, 1))
    assert "MONTHLY_LIMIT_NOT_IN_WORK_SHIFTS" in _codes(issues)
    assert issues[0]["evidence"]["month"] == 7      # 달 컨텍스트


def test_night_dedicated_over_max_night_blocks(base):
    db = base
    db.add(RosterConfig(config_id=1, group_id="A", office_id="o1", max_nig_per_month=15))
    db.add(NurseMonthlyLimit(nurse_id="n1", group_id="A", year=2026, month=7, n_exact=20))
    db.flush()
    issues = validate_allowed_shift_period(db, "n1", "A", ["N"], date(2026, 7, 1))
    assert "MONTHLY_LIMIT_NIGHT_DEDICATED_N_OVER_MAX_NIGHT" in _codes(issues)


def test_allowed_vs_fixed_shift_blocks(base):
    db = base
    # fixed_shift 는 allowed satellite 의 형제 컬럼 (allowed_shifts NOT NULL)
    db.add(NurseAllowedShiftPeriod(nurse_id="n1", valid_from=date(2026, 7, 1),
           valid_to=None, allowed_shifts=["N"], fixed_shift="N_A"))
    db.flush()
    issues = validate_allowed_shift_period(db, "n1", "A", ["D", "E"], date(2026, 7, 1))
    assert "ALLOWED_VS_FIXED_SHIFT_CONFLICT" in _codes(issues)


def test_no_restriction_passes(base):
    db = base
    db.add(NurseMonthlyLimit(nurse_id="n1", group_id="A", year=2026, month=7, n_exact=2))
    db.flush()
    assert validate_allowed_shift_period(db, "n1", "A", [], date(2026, 7, 1)) == []  # 제한 없음


def test_consistent_passes(base):
    db = base
    db.add(NurseMonthlyLimit(nurse_id="n1", group_id="A", year=2026, month=7,
                             d_exact=10, e_exact=5))
    db.flush()
    # 허용 [D,E,N] 이고 N 한도 0 → 모순 없음
    assert validate_allowed_shift_period(db, "n1", "A", ["D", "E", "N"], date(2026, 7, 1)) == []


# ── 엔드포인트 422 ──────────────────────────────────────────────────────────
def _user():
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id="o1", group_id="A",
        is_head_nurse=True, is_master_admin=False, name="수간", EmpSeqNo="",
        EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True, hn_auth="HN",
        original_group_id="A", gw_useYN="Y", qpis_useYN="Y",
    )


def _client(db):
    app = FastAPI()
    app.include_router(nurse_period_router)

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_from_cookie] = lambda: _user()
    return TestClient(app)


def test_change_endpoint_blocks_contradiction_422(base):
    db = base
    db.add(NurseMonthlyLimit(nurse_id="n1", group_id="A", year=2026, month=7, n_exact=2))
    db.flush()
    c = _client(db)
    r = c.post("/nurse-period/change", json={
        "attribute": "allowed_shifts", "nurse_id": "n1",
        "valid_from": "2026-07-01", "value": ["D", "E"]})
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert any(i["reason_code"] == "MONTHLY_LIMIT_NOT_IN_WORK_SHIFTS" for i in body["issues"])
    # 저장 안 됨
    from db.models import NurseAllowedShiftPeriod
    assert db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="n1").count() == 0


def test_change_endpoint_allows_consistent_200(base):
    db = base
    db.add(NurseMonthlyLimit(nurse_id="n1", group_id="A", year=2026, month=7, n_exact=2))
    db.flush()
    c = _client(db)
    r = c.post("/nurse-period/change", json={
        "attribute": "allowed_shifts", "nurse_id": "n1",
        "valid_from": "2026-07-01", "value": ["D", "E", "N"]})  # N 허용 → 모순 없음
    assert r.status_code == 200, r.text
