"""프리셉티 write 경로(period 전용) 테스트 — preceptee-self + preceptor-side.

도려내기: assignment 미경유, 종료예정일 필수, 취소=삭제.
"""
from datetime import date

import pytest

from db.models import Office, Group, Nurse, NursePrecepteePeriod
from schemas.auth_schema import User
from services.nurse_service import (
    _dispatch_preceptee_self_period,
    _dispatch_preceptees_payload,
)


def _admin():
    return User(
        nurse_id="adm", account_id="adm", office_id="o1", group_id="A",
        is_master_admin=True, name="adm", EmpSeqNo="", EmpAuthGbn="",
        mb_part="", office_name="", mb_part_name="", gw_useYN="Y", qpis_useYN="Y",
        official_title_name=None,
    )


@pytest.fixture
def ward(db):
    db.add(Office(office_id="o1", office_name="H"))
    db.add(Group(group_id="A", group_name="A", office_id="o1"))
    db.add(Nurse(nurse_id="pe", account_id="pe", group_id="A", office_id="o1", name="mentee", active=1))
    db.add(Nurse(nurse_id="pr", account_id="pr", group_id="A", office_id="o1", name="mentor", active=1))
    db.flush()
    return db


def _period(db, nid):
    return db.query(NursePrecepteePeriod).filter(NursePrecepteePeriod.nurse_id == nid).all()


# ── preceptee-self (프리셉티가 프리셉터 선택) ──
def test_self_create_writes_period_and_cache(ward):
    db = ward
    _dispatch_preceptee_self_period(db, "pe", {
        "operation": "create", "preceptor_id": "pr",
        "start_date": "2026-08-01", "expected_end_date": "2026-08-31",
    }, _admin())
    rows = _period(db, "pe")
    assert len(rows) == 1
    assert rows[0].preceptor_id == "pr"
    assert rows[0].valid_from == date(2026, 8, 1) and rows[0].valid_to == date(2026, 9, 1)  # exclusive


def test_self_end_date_required(ward):
    db = ward
    with pytest.raises(Exception):  # HTTPException 422
        _dispatch_preceptee_self_period(db, "pe", {
            "operation": "create", "preceptor_id": "pr", "start_date": "2026-08-01",
        }, _admin())


def test_self_cancel_deletes(ward):
    db = ward
    _dispatch_preceptee_self_period(db, "pe", {
        "operation": "create", "preceptor_id": "pr",
        "start_date": "2026-08-01", "expected_end_date": "2026-08-31",
    }, _admin())
    _dispatch_preceptee_self_period(db, "pe", {"operation": "cancel"}, _admin())
    assert _period(db, "pe") == []


def test_self_cannot_be_own_preceptor(ward):
    db = ward
    with pytest.raises(Exception):
        _dispatch_preceptee_self_period(db, "pe", {
            "operation": "create", "preceptor_id": "pe",
            "start_date": "2026-08-01", "expected_end_date": "2026-08-31",
        }, _admin())


# ── preceptor-side (프리셉터가 preceptees 지정, target_nurse_id 기반) ──
def test_preceptor_side_create_via_target(ward):
    db = ward
    _dispatch_preceptees_payload(db, "pr", {
        "operation": "create", "target_nurse_id": "pe",
        "start_date": "2026-08-01", "expected_end_date": "2026-08-31",
    }, _admin())
    rows = _period(db, "pe")
    assert len(rows) == 1 and rows[0].preceptor_id == "pr"


def test_preceptor_side_end_required(ward):
    db = ward
    with pytest.raises(Exception):
        _dispatch_preceptees_payload(db, "pr", {
            "operation": "create", "target_nurse_id": "pe", "start_date": "2026-08-01",
        }, _admin())


def test_preceptor_side_cancel_deletes(ward):
    db = ward
    _dispatch_preceptees_payload(db, "pr", {
        "operation": "create", "target_nurse_id": "pe",
        "start_date": "2026-08-01", "expected_end_date": "2026-08-31",
    }, _admin())
    _dispatch_preceptees_payload(db, "pr", {"operation": "cancel", "target_nurse_id": "pe"}, _admin())
    assert _period(db, "pe") == []
