"""P2 회귀: 엑셀 업로드의 allowed_shifts 가 캐시 직접쓰기가 아니라 period(SSOT) 경유.

버그: upload2_confirm 이 existing.allowed_shifts=... 로 캐시만 수정 → 생성기(period as-of)가
무시. 수정: _excel_upsert_allowed 가 upsert_period 로 기록 + 캐시 단방향 투영.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from db.models import Office, Group, Nurse, NurseAllowedShiftPeriod
from services.excel_service import _excel_upsert_allowed
from services.nurse_period_resolver import fetch_periods, resolve_asof


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, allowed_shifts=["D", "E", "N"], grade=1))
    db.flush()
    return db


def test_excel_update_writes_period_and_projects_cache(seeded):
    db = seeded
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "n1").first()

    _excel_upsert_allowed(db, nurse, ["D"])
    db.flush()

    today = date.today()
    rows = fetch_periods(db, NurseAllowedShiftPeriod, ["n1"], today, today + timedelta(days=1)).get("n1")
    # period(SSOT)에 기록됨
    assert resolve_asof(rows, today, "allowed_shifts") == ["D"]
    # 캐시도 단방향 투영(today 발효라 즉시)
    assert list(nurse.allowed_shifts) == ["D"]
